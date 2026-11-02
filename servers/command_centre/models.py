"""Command Centre: alerts that must reach a person. The seed of the module.

WHY A TABLE AND NOT A PUSH
--------------------------
The platform already fans an SOS out as push notifications and SMS
(`sos.tasks.dispatch_sos`) and the console bell reads per-user
`rider.Notification` rows. Neither answers the question the owner asked on
2026-10-28: "which SOS could not be routed to a city, and has anybody taken
it?" A push is gone once dismissed; a per-user notification belongs to one
account. An `OperatorAlert` is a platform-level row with an acknowledgement
on it, so the Command Centre can show what is waiting on a person, and the
audit log can show who answered.

FIRST PRODUCER
--------------
`sos.anchoring.alert_unanchored`: an SOS with no trip, no usable GPS fix
and no driver home city. Such an event is already visible to every Safety
Officer regardless of scope; the alert is what makes it IMPOSSIBLE TO MISS
rather than merely possible to find.

SCOPE (revised with the threshold alerts, Command Centre module)
-----------------------------------------------------------------
The first alert kind existed because a row could NOT be placed, and such an
alert is for every `report.read` holder regardless of geography (see
`admin_access.scoping.GLOBAL_BY_DESIGN`). A THRESHOLD alert is the
opposite: it is about one city's numbers, so it carries `city`, and the
list narrows city-anchored alerts to the caller's scope while leaving
city-less alerts visible to everyone. Acknowledging asks the capability
that owns the alert's kind (`ACK_PERMISSION`), at the alert's city when it
has one.

SECOND PRODUCER: ALERT THRESHOLDS
---------------------------------
`AlertThreshold` is versioned config per (city, metric): the warn and
critical lines, the window a rate is measured over, who put the version in
place and through which maker-checker request. `tasks.
evaluate_alert_thresholds` reads the active rows every minute, measures
each city (`overview.measure_city`) and raises, escalates or clears the
matching `OperatorAlert` idempotently per (city, metric). Thresholds are
written only by the seed command (version 1) and the approvals executor
(`command_centre.threshold_change`, always a checker, `platform.settings`).
"""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from servers.admin_audit.models import actor_snapshot

from .metrics import METRICS, RATE_WINDOWS, WINDOW_NOW, metric_for

INFO, WARNING, CRITICAL = 'info', 'warning', 'critical'
SEVERITY_CHOICES = [(INFO, 'Info'), (WARNING, 'Warning'), (CRITICAL, 'Critical')]

#: Alert kinds, and the permission that may acknowledge each. A kind that is
#: not listed cannot be acknowledged over the API -- a deliberate default,
#: because "anyone may dismiss it" is how an alert stops being one.
SOS_UNANCHORED = 'sos_unanchored'
#: Safety module: an SOS nobody acknowledged in time, a critical incident
#: nobody picked up in time, and a detected signal above its incident line.
#: All three are live-safety alerts: `safety.respond` takes them, and they
#: join `SOS_ALERT_KINDS` (so Marketing and Analyst never see them).
SAFETY_SOS_ESCALATED = 'safety_sos_escalated'
SAFETY_INCIDENT_UNASSIGNED = 'safety_incident_unassigned'
SAFETY_SIGNAL = 'safety_signal'
#: A LIVE city with fewer Safety Officers than its country's minimum (or a
#: country with no minimum configured). Critical, raised and cleared by the
#: coverage sweep (`safety.coverage`), carries no personal data. It is not in
#: `ACK_PERMISSION` on purpose: it cannot be acknowledged away, it clears
#: when the gap is fixed. Being absent from `SOS_ALERT_KINDS` it is visible to
#: whoever sees the city's other alerts.
SAFETY_COVERAGE_GAP = 'safety_coverage_gap'
#: Module 9 (owner item): a completed trip that could not be settled because no commission
#: rule governs it. Critical, one per trip, anchored to the trip's city, visible only to
#: `finance.read` holders in scope (and Super Admin); City Ops, Marketing and Analyst never
#: see it. Not in `ACK_PERMISSION` on purpose: it clears itself when the trip settles
#: (`payments.unsettled`).
SETTLEMENT_NO_COMMISSION_RULE = 'settlement.no_commission_rule'
#: Kinds that are money, not operations: `_visible_alerts` shows them by `finance.read`.
#: An operator ended a STARTED stale trip under the platform-funded rule (safety incident or
#: platform/app fault): the rider paid 0 and the platform paid the driver. Warning, one per
#: trip, anchored to the trip's city, Finance (`finance.read` in scope) and Super Admin. It
#: is acknowledged by Finance once seen (`ACK_PERMISSION`).
FINANCE_PLATFORM_FUNDED_END = 'finance.platform_funded_trip_end'
#: Module 12 (Promotions): a campaign used its whole budget and stopped (info). Anchored at the
#: campaign's city or country, so it is seen by whoever sees that place (Marketing for a rider
#: promo, City Ops for an incentive, Super Admin always) and acknowledged by the campaign's owner (`promo.write` or `incentive.write`).
PROMOTION_BUDGET_EXHAUSTED = 'promotion.budget_exhausted'
#: Module 12: trips that already held a discount were honoured past the campaign's budget
#: (warning). Money, so Finance (`finance.read`) and Super Admin, like the other money alerts.
PROMOTION_BUDGET_OVERSHOOT = 'promotion.budget_overshoot'
#: A payout the provider reported paid was then reversed by the bank (AUDIT 27.71): the amount
#: is back in the driver's balance. Warning, one per payout, anchored to the driver's home city,
#: Finance (`finance.read` in scope) and Super Admin, acknowledged by Finance.
FINANCE_PAYOUT_REVERSED = 'finance.payout_reversed'
#: The provider reported a payout PAID after it had failed and been refunded (AUDIT 27.72): it is
#: frozen as `conflict` until Finance resolves it with a maker-checker adjustment. Critical, one per
#: payout, Finance and Super Admin; it clears itself when resolved (or when the provider reverses it).
FINANCE_PAYOUT_CONFLICT = 'finance.payout_conflict'
#: An old online order was paid after the trip had been paid another way (AUDIT 27.75): never
#: settled, refunded in full automatically. Critical, one per payment; the title says whether the
#: refund went through or must be made by hand.
FINANCE_PAID_AFTER_SETTLED = 'finance.paid_after_settled'
FINANCE_ALERT_KINDS = frozenset({SETTLEMENT_NO_COMMISSION_RULE, FINANCE_PLATFORM_FUNDED_END,
                                 PROMOTION_BUDGET_OVERSHOOT, FINANCE_PAYOUT_REVERSED,
                                 FINANCE_PAYOUT_CONFLICT, FINANCE_PAID_AFTER_SETTLED})
#: Module 11: a notification provider's failure rate over the last 15 minutes is above its
#: threshold (warning). Platform health, not operations: visible to Super Admin only, which
#: is to say holders of `platform.settings`. It clears itself when the rate recovers, and can
#: also be acknowledged by the same holders.
NOTIFY_PROVIDER_FAILING = 'notify.provider_failing'
#: Module 13 (Fraud & Risk): a new high-severity flag (warning, rate-limited to one per city per
#: half hour) and open flags older than 48 hours piling up in a city (info). Seen by `fraud.read`
#: holders in scope (and Super Admin) only, so Finance and Analyst never see them; taken by
#: `fraud.review`. They carry a count and a city, never a person.
FRAUD_FLAG_HIGH = 'fraud.flag_high'
FRAUD_QUEUE_AGING = 'fraud.queue_aging'
#: Fraud money actions (AUDIT 27.56): a payout hold reached its maximum duration and lapsed
#: (warning). Seen by Finance (`finance.read`) AND the flag's reviewers (`fraud.read`), to renew it
#: or leave the payout released. A city, never a person.
FRAUD_PAYOUT_HOLD_EXPIRED = 'fraud.payout_hold_expired'
FRAUD_ALERT_KINDS = frozenset({FRAUD_FLAG_HIGH, FRAUD_QUEUE_AGING, FRAUD_PAYOUT_HOLD_EXPIRED})
#: Capabilities, besides `ACK_PERMISSION`'s, that may also acknowledge a kind.
ACK_ALSO = {PROMOTION_BUDGET_EXHAUSTED: ('incentive.write',),
            FRAUD_PAYOUT_HOLD_EXPIRED: ('finance.payout',)}
#: Kinds only Super Admin (`platform.settings`) sees.
PLATFORM_ALERT_KINDS = frozenset({NOTIFY_PROVIDER_FAILING})
ACK_PERMISSION = {
    NOTIFY_PROVIDER_FAILING: 'platform.settings',
    FINANCE_PLATFORM_FUNDED_END: 'finance.read',
    FINANCE_PAYOUT_REVERSED: 'finance.read',
    PROMOTION_BUDGET_OVERSHOOT: 'finance.read',
    FRAUD_FLAG_HIGH: 'fraud.review',
    FRAUD_QUEUE_AGING: 'fraud.review',
    FRAUD_PAYOUT_HOLD_EXPIRED: 'fraud.review',
    # A budget alert is taken by whoever OWNS the campaign (Marketing: promo.write; City Ops:
    # incentive.write, see ACK_ALSO). promo.read would let a read-only Analyst take it.
    PROMOTION_BUDGET_EXHAUSTED: 'promo.write',
    SOS_UNANCHORED: 'safety.respond',
    SAFETY_SOS_ESCALATED: 'safety.respond',
    SAFETY_INCIDENT_UNASSIGNED: 'safety.respond',
    SAFETY_SIGNAL: 'safety.respond',
    # Threshold alerts: an operational rate (unfulfilled, acceptance, ETA) is
    # the City Ops Manager's to take (`trip.write`); an SOS line is Safety's;
    # the payout-age line is Finance's (`finance.read`, the capability that
    # opens the withdrawals queue where the fix is made).
    **{metric.alert_kind: metric.ack_permission for metric in METRICS.values()},
}
#: The kind of every threshold alert, for the `?kind=` filter and the task.
THRESHOLD_KINDS = frozenset(metric.alert_kind for metric in METRICS.values())


class OperatorAlert(models.Model):
    kind = models.CharField(max_length=40, db_index=True)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default=WARNING)
    title = models.CharField(max_length=200)
    #: Enough to open the thing the alert is about (`sos_id`, ...). Never a
    #: phone number or a name: the list is read by more roles than may see
    #: those, and the masked detail endpoints exist for the rest.
    payload = models.JSONField(default=dict, blank=True)
    #: What raised it, for de-duplication and for the console's deep link.
    source_type = models.CharField(max_length=40, blank=True, default='')
    source_id = models.CharField(max_length=64, blank=True, default='')
    #: The city a threshold alert is about. NULL for an alert with no place
    #: (an unroutable SOS), which stays visible to every listing holder.
    city = models.ForeignKey(
        'admin_access.GeoNode', null=True, blank=True,
        on_delete=models.PROTECT, related_name='operator_alerts',
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True, db_index=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='acknowledged_operator_alerts',
    )
    acknowledged_note = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source_type', 'source_id'], name='opalert_source_idx'),
        ]

    def __str__(self):
        state = 'open' if self.acknowledged_at is None else 'acknowledged'
        return f'Alert#{self.id} [{state}] {self.kind}: {self.title}'

    @property
    def is_open(self):
        return self.acknowledged_at is None


# ---------------------------------------------------------------------------
# Alert thresholds: versioned config per city
# ---------------------------------------------------------------------------

WINDOW_CHOICES = [(w, w) for w in (*RATE_WINDOWS, WINDOW_NOW)]


class AlertThreshold(models.Model):
    """One version of the lines for (city, metric).

    Shaped like `approvals.ApprovalPolicy` on purpose: exactly one active row
    per (city, metric); a change closes the current row (`valid_to`,
    `is_active=False`) and opens the next version, so "what was the SOS line
    on the night of the incident" is a query, not a guess.
    """

    city = models.ForeignKey(
        'admin_access.GeoNode', on_delete=models.PROTECT, related_name='alert_thresholds',
    )
    metric = models.CharField(max_length=40, db_index=True)
    #: In the metric's unit: percent, minutes, hours or a count. NULL means
    #: there is no line of that severity for this threshold.
    warn_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    critical_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    #: The window a rate is measured over; `now` for a stock metric.
    window = models.CharField(max_length=8, choices=WINDOW_CHOICES)

    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    valid_from = models.DateTimeField(default=timezone.now, editable=False)
    valid_to = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default='')

    # Who put this version in place: a snapshot, like an audit row.
    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    created_by_email = models.CharField(max_length=254, blank=True, default='')
    created_by_role = models.CharField(max_length=64, blank=True, default='')
    #: The maker-checker request that wrote this version; NULL for a seed.
    changed_via = models.ForeignKey(
        'approvals.ApprovalRequest', null=True, blank=True,
        on_delete=models.PROTECT, related_name='alert_thresholds_written',
    )

    class Meta:
        ordering = ['city__path', 'metric', '-version']
        constraints = [
            models.UniqueConstraint(
                fields=['city', 'metric'], condition=Q(is_active=True),
                name='cc_one_active_threshold',
            ),
            models.UniqueConstraint(
                fields=['city', 'metric', 'version'],
                name='cc_threshold_version_unique',
            ),
            # At least one line, or the row watches nothing.
            models.CheckConstraint(
                condition=Q(warn_value__isnull=False) | Q(critical_value__isnull=False),
                name='cc_threshold_has_a_line',
            ),
        ]

    def __str__(self):
        return f'{self.city_id}:{self.metric} v{self.version}'

    @property
    def definition(self):
        return metric_for(self.metric)

    def clean(self):
        try:
            metric = metric_for(self.metric)
        except ValueError as exc:
            raise ValidationError({'metric': str(exc)})
        if metric.is_rate:
            if self.window not in RATE_WINDOWS:
                raise ValidationError({'window': f'{self.metric} is measured over one of {RATE_WINDOWS}.'})
        elif self.window != WINDOW_NOW:
            raise ValidationError({'window': f'{self.metric} is a current value; its window is {WINDOW_NOW!r}.'})
        if self.warn_value is None and self.critical_value is None:
            raise ValidationError('A threshold needs a warn line or a critical line.')
        if self.warn_value is not None and self.critical_value is not None:
            warn, crit = Decimal(self.warn_value), Decimal(self.critical_value)
            # Critical must be the more extreme line in the metric's direction.
            if (metric.direction == 'above' and crit <= warn) or (metric.direction == 'below' and crit >= warn):
                raise ValidationError('The critical line must be beyond the warn line.')

    def save(self, *args, **kwargs):
        self.full_clean(exclude=['changed_via'])
        with transaction.atomic():
            super().save(*args, **kwargs)

    def set_created_by(self, user):
        self.set_created_by_snapshot(actor_snapshot(user))

    def set_created_by_snapshot(self, snapshot):
        self.created_by_id = snapshot.get('actor_id')
        self.created_by_label = (snapshot.get('actor_label') or '')[:255]
        self.created_by_email = (snapshot.get('actor_email') or '')[:254]
        self.created_by_role = (snapshot.get('actor_role') or '')[:64]

    def as_dict(self):
        return {
            'id': self.id,
            'city': self.city.code if self.city_id else None,
            'city_name': self.city.name if self.city_id else None,
            'metric': self.metric,
            'warn_value': plain_number(self.warn_value),
            'critical_value': plain_number(self.critical_value),
            'window': self.window,
            'version': self.version,
            'is_active': self.is_active,
            'valid_from': self.valid_from.isoformat() if self.valid_from else None,
            'valid_to': self.valid_to.isoformat() if self.valid_to else None,
            'reason': self.reason,
            'created_by': {
                'id': self.created_by_id, 'label': self.created_by_label,
                'email': self.created_by_email, 'role': self.created_by_role,
            },
            'changed_via': self.changed_via_id,
        }


def plain_number(value):
    """A Decimal as JSON text: '15', '7.5', never '1.5E+1' or '15.00'."""
    if value is None:
        return None
    d = Decimal(str(value))
    return str(d.quantize(Decimal(1))) if d == d.to_integral() else str(d.normalize())
