"""Fraud & Risk (module 13): the review queue, its timeline and the detection rule settings.

A `RiskFlag` names the SUBJECT (the user the evidence is about), a `kind` (the rule that raised
it), the evidence as JSON and a status. THE EVIDENCE CARRIES NO RAW PERSONAL DATA: user ids, the
names of the signals that matched (`same_device`, `same_phone`, `same_instrument`), trip and
campaign ids, counts and amounts in minor units. Never a phone number, an email, a token or an
account number; when two accounts were compared by a fingerprint, the evidence says WHICH signal
matched, not the value.

LIFECYCLE
---------
    open  ->  in_review (assigned)  ->  confirmed | dismissed       reopen (with a reason) -> open

Every transition is a `FlagEvent` (actor, from, to, reason, note, at) AND an audit row. A decision
needs a reason code and a note. A flag is never deleted.

WHAT A FLAG NEVER DOES
----------------------
A flag, its confirmation and its dismissal record a decision. They do not change a fare, hold a
payout, take a reward back or notify the subject. The ONLY money effect of a flag is the reward hold
(owner ruling a, AUDIT 27.56; see `money`): the unpaid reward linked to the flagged activity is
reserved until the flag is decided or the hold runs out. Confirming can hand off to actions that
already exist in other modules (see `review.HANDOFFS`); it never performs them. Clawback, a payout
hold and online-only are separate, gated actions (see `money`).

DEDUPE
------
`dedupe_key` is `<rule_key>:<subject_id>` while a flag is active (open or in review): a database
constraint allows one active flag per key, so a repeated hit appends to the same row (hit count,
last seen, new references) instead of raising another. The module-12 referral hook keeps its own
key (`source_ref`, one per referral) and leaves `dedupe_key` blank.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from servers.admin_audit.models import actor_snapshot

# --- kinds (one per rule) -----------------------------------------------------------------

REFERRAL_ABUSE = 'referral_abuse'
MULTI_ACCOUNTING = 'multi_accounting'
COLLUSION = 'collusion'
INCENTIVE_FARMING = 'incentive_farming'
GPS_ANOMALY = 'gps_anomaly'
CANCELLATION_ABUSE = 'cancellation_abuse'
PAYMENT_ABUSE = 'payment_abuse'

KIND_CHOICES = [
    (REFERRAL_ABUSE, 'Referral abuse'),
    (MULTI_ACCOUNTING, 'Multiple accounts'),
    (COLLUSION, 'Rider-driver collusion'),
    (INCENTIVE_FARMING, 'Incentive farming'),
    (GPS_ANOMALY, 'GPS anomaly'),
    (CANCELLATION_ABUSE, 'Cancellation abuse'),
    (PAYMENT_ABUSE, 'Payment abuse'),
]

# --- status -------------------------------------------------------------------------------

OPEN, IN_REVIEW, DISMISSED, CONFIRMED = 'open', 'in_review', 'dismissed', 'confirmed'
STATUS_CHOICES = [(OPEN, 'Open'), (IN_REVIEW, 'In review'), (DISMISSED, 'Dismissed'),
                  (CONFIRMED, 'Confirmed')]
ACTIVE_STATUSES = (OPEN, IN_REVIEW)
DECIDED_STATUSES = (CONFIRMED, DISMISSED)

# --- severity -----------------------------------------------------------------------------

LOW, MEDIUM, HIGH = 'low', 'medium', 'high'
SEVERITY_CHOICES = [(LOW, 'Low'), (MEDIUM, 'Medium'), (HIGH, 'High')]
#: Lower sorts first in the queue.
SEVERITY_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}

# --- decision reason codes ----------------------------------------------------------------

CONFIRM_REASONS = {
    'multi_accounting': 'Multiple accounts for one person',
    'collusion': 'Rider and driver colluding',
    'gps_spoofing': 'GPS spoofing or tampering',
    'promo_abuse': 'Promotion or referral abuse',
    'payment_abuse': 'Payment abuse',
    'other': 'Other',
}
DISMISS_REASONS = {
    'false_positive': 'False positive',
    'legitimate_shared_device': 'Legitimate shared device',
    'insufficient_evidence': 'Insufficient evidence',
    'duplicate': 'Duplicate of another flag',
    'other': 'Other',
}
#: Why a decided flag is being reopened.
REOPEN_REASONS = {
    'new_evidence': 'New evidence',
    'decided_in_error': 'Decided in error',
    'appeal': 'Appeal or complaint',
    'other': 'Other',
}
NOTE_MIN, NOTE_MAX = 5, 500

# --- timeline actions ---------------------------------------------------------------------

EV_RAISED, EV_HIT, EV_ASSIGNED, EV_UNASSIGNED = 'raised', 'hit', 'assigned', 'unassigned'
EV_NOTE, EV_CONFIRMED, EV_DISMISSED, EV_REOPENED = 'note', 'confirmed', 'dismissed', 'reopened'
EV_HANDOFF = 'handoff'
EVENT_CHOICES = [(a, a) for a in (EV_RAISED, EV_HIT, EV_ASSIGNED, EV_UNASSIGNED, EV_NOTE,
                                  EV_CONFIRMED, EV_DISMISSED, EV_REOPENED, EV_HANDOFF)]


class RiskFlag(models.Model):
    subject = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='risk_flags')
    kind = models.CharField(max_length=40, choices=KIND_CHOICES, db_index=True)
    #: Signal names, ids, counts and amounts. No raw personal data (see the module docstring).
    #: Rule flags carry `signals`, `refs` ({trip_ids, campaign_ids, counterpart_user_ids}),
    #: `hit_count`, `first_seen`, `last_seen` and the rule's own measurements.
    evidence = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=OPEN, db_index=True)
    #: Where the flag sits for scope: the subject's city (else their country). NULL is visible
    #: to unrestricted callers only.
    anchor_node = models.ForeignKey('admin_access.GeoNode', null=True, blank=True,
                                    on_delete=models.PROTECT, related_name='risk_flags')
    #: What raised it, so one event raises one flag: e.g. `referral:41`.
    source_ref = models.CharField(max_length=64, blank=True, default='', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # --- module 13 ---
    severity = models.CharField(max_length=8, choices=SEVERITY_CHOICES, default=MEDIUM,
                                db_index=True)
    #: 0-100, from the rule that raised it.
    score = models.PositiveSmallIntegerField(default=0)
    #: Which rule, and which version of that country's settings, raised it. Blank / 0 for a flag
    #: that predates module 13 (it is backfilled to its kind).
    rule_key = models.CharField(max_length=40, blank=True, default='', db_index=True)
    rule_version = models.PositiveIntegerField(default=0)
    #: `<rule_key>:<subject_id>` while the flag is active; blank on referral flags.
    dedupe_key = models.CharField(max_length=80, blank=True, default='')
    hit_count = models.PositiveIntegerField(default=1)
    last_seen_at = models.DateTimeField(default=timezone.now)

    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='+')
    assigned_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=32, blank=True, default='')
    decision_note = models.CharField(max_length=NOTE_MAX, blank=True, default='')
    #: The hand-offs taken from a confirmed flag: [{kind, at, by}]. A record, not an action.
    handoffs = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['kind', 'source_ref'],
                                    condition=~models.Q(source_ref=''),
                                    name='riskflag_one_per_source'),
            models.UniqueConstraint(fields=['dedupe_key'],
                                    condition=Q(status__in=['open', 'in_review'])
                                    & ~Q(dedupe_key=''),
                                    name='riskflag_one_active_per_dedupe_key'),
        ]
        indexes = [models.Index(fields=['status', 'severity', 'created_at'],
                                name='riskflag_queue_idx')]

    def __str__(self):
        return f'RiskFlag#{self.pk} {self.kind} {self.status}'

    @property
    def is_active(self):
        return self.status in ACTIVE_STATUSES


class FlagEvent(models.Model):
    """One line of a flag's timeline. Insert-only."""

    flag = models.ForeignKey(RiskFlag, on_delete=models.CASCADE, related_name='events')
    action = models.CharField(max_length=16, choices=EVENT_CHOICES)
    from_status = models.CharField(max_length=12, blank=True, default='')
    to_status = models.CharField(max_length=12, blank=True, default='')
    reason = models.CharField(max_length=32, blank=True, default='')
    note = models.CharField(max_length=NOTE_MAX, blank=True, default='')
    #: Who: a snapshot, so the line survives an account change. Blank actor = the system (a rule).
    actor_id = models.BigIntegerField(null=True, blank=True)
    actor_label = models.CharField(max_length=255, blank=True, default='')
    data = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['at', 'id']

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError('A flag event is insert-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('A flag event is insert-only.')


class RiskRuleConfig(models.Model):
    """One version of a rule's settings for one country.

    Exactly one active row per (country, rule). A change closes the current row and opens the next
    version, so "which numbers raised this flag" is a lookup (`RiskFlag.rule_version`). Written by
    the seed (version 1) and the approvals executor (`fraud.rule_change`, always a checker). NO
    FALLBACK: a country with no active row does not run that rule.
    """

    country = models.CharField(max_length=2, db_index=True)
    rule_key = models.CharField(max_length=40, choices=KIND_CHOICES, db_index=True)
    thresholds = models.JSONField(default=dict)
    enabled = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_to = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default='')
    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    created_by_email = models.CharField(max_length=254, blank=True, default='')
    created_by_role = models.CharField(max_length=64, blank=True, default='')
    changed_via = models.ForeignKey('approvals.ApprovalRequest', null=True, blank=True,
                                    on_delete=models.PROTECT, related_name='+')
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['country', 'rule_key', '-version']
        constraints = [
            models.UniqueConstraint(fields=['country', 'rule_key'], condition=Q(is_active=True),
                                    name='fraud_one_active_rule_config'),
            models.UniqueConstraint(fields=['country', 'rule_key', 'version'],
                                    name='fraud_rule_config_version_unique'),
        ]

    def __str__(self):
        return f'{self.country}:{self.rule_key} v{self.version}'

    def set_created_by(self, user):
        self.set_created_by_snapshot(actor_snapshot(user))

    def set_created_by_snapshot(self, snap):
        self.created_by_id = snap.get('actor_id')
        self.created_by_label = (snap.get('actor_label') or '')[:255]
        self.created_by_email = (snap.get('actor_email') or '')[:254]
        self.created_by_role = (snap.get('actor_role') or '')[:64]

    def as_dict(self):
        return {
            'id': self.id, 'country': self.country, 'rule_key': self.rule_key,
            'version': self.version, 'enabled': self.enabled, 'is_active': self.is_active,
            'thresholds': dict(self.thresholds or {}),
            'valid_from': self.valid_from.isoformat() if self.valid_from else None,
            'valid_to': self.valid_to.isoformat() if self.valid_to else None,
            'reason': self.reason,
            'created_by': {'id': self.created_by_id, 'label': self.created_by_label,
                           'email': self.created_by_email, 'role': self.created_by_role},
            'changed_via': self.changed_via_id,
        }


# ---------------------------------------------------------------------------
# Money actions (owner rulings 2026-10-31, AUDIT 27.56)
# ---------------------------------------------------------------------------
#
# a. REWARD HOLD  -> RewardHold: only the unpaid incentive / referral reward linked to the flagged
#    activity; never trip earnings. Auto-released after `reward_hold_days`.
# b. RECOVERY     -> RecoveryDebt / RecoveryDeduction: a confirmed promo abuse / collusion flag can
#    claw back linked rewards through a Finance adjustment request (maker-checker); never more than
#    the reward; the weekly payout takes at most `recovery_max_percent` of one payout.
# d. PAYOUT HOLD  -> PayoutHold: only with a suspension, a second admin, and a maximum duration.
#
# Nothing here is created by a flag, a confirmation or a dismissal on its own account except the
# reward HOLD, which is exactly what ruling (a) asks for.

class RewardHold(models.Model):
    HELD, RELEASED, FORFEITED = 'held', 'released', 'forfeited'
    STATUS_CHOICES = [(HELD, 'Held (pending review)'), (RELEASED, 'Released'),
                      (FORFEITED, 'Forfeited')]
    RELEASE_REASONS = {
        'auto_expired': 'The hold ran out before the flag was decided',
        'dismissed': 'The flag was dismissed',
        'confirmed_no_recovery': 'The flag was confirmed for a reason that does not recover rewards',
        'expired_after_confirm': 'The hold ran out after the flag was confirmed',
        'reopened_refreshed': 'The flag was reopened',
    }

    flag = models.ForeignKey(RiskFlag, on_delete=models.CASCADE, related_name='reward_holds')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='reward_holds')
    #: `driver` (a credit on the settlement wallet) or `rider` (a wallet credit).
    role = models.CharField(max_length=8)
    ledger_entry = models.ForeignKey('promotions.LedgerEntry', on_delete=models.PROTECT,
                                     related_name='+')
    kind = models.CharField(max_length=40)           # incentive_reward | referral_reward
    #: What is held: the part of the reward still unpaid (never more than the reward).
    amount_minor = models.BigIntegerField()
    reward_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=HELD, db_index=True)
    held_at = models.DateTimeField(default=timezone.now)
    release_at = models.DateTimeField(db_index=True)
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=32, blank=True, default='')
    #: The Finance adjustment that forfeited it.
    forfeited_via = models.ForeignKey('approvals.ApprovalRequest', null=True, blank=True,
                                      on_delete=models.PROTECT, related_name='+')

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['ledger_entry'], condition=Q(status='held'),
                                    name='rewardhold_one_active_per_reward'),
            models.CheckConstraint(condition=Q(amount_minor__gt=0),
                                   name='rewardhold_amount_positive'),
        ]

    def __str__(self):
        return f'RewardHold#{self.pk} {self.status} {self.amount_minor} {self.currency}'


class RecoveryDebt(models.Model):
    """What a person still owes back from a recovery Finance approved.

    DRIVER: collected from weekly payouts (and driver-requested withdrawals), at most
    `recovery_max_percent` of any one payout, the rest carrying over. RIDER: taken from the wallet
    balance at once, never below zero; what the wallet could not cover stays here as
    `uncollected` for Finance to see and nothing else is ever collected.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='recovery_debts')
    role = models.CharField(max_length=8)
    flag = models.ForeignKey(RiskFlag, on_delete=models.PROTECT, related_name='recovery_debts')
    approval_request = models.OneToOneField('approvals.ApprovalRequest', on_delete=models.PROTECT,
                                            related_name='+')
    #: Total of the request, and how it was met: forfeited from held rewards, taken so far,
    #: and (rider only) what the wallet could not cover.
    requested_minor = models.BigIntegerField()
    forfeited_minor = models.BigIntegerField(default=0)
    amount_minor = models.BigIntegerField()              # the debt after forfeiting held rewards
    recovered_minor = models.BigIntegerField(default=0)
    uncollected_minor = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3)
    note = models.CharField(max_length=NOTE_MAX, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.CheckConstraint(
                condition=Q(recovered_minor__gte=0) & Q(recovered_minor__lte=models.F('amount_minor')),
                name='recoverydebt_recovered_within_amount'),
        ]

    @property
    def remaining_minor(self):
        if self.closed_at is not None:
            return 0
        return max(0, self.amount_minor - self.recovered_minor - self.uncollected_minor)


class RecoveryDeduction(models.Model):
    """One weekly-payout (or withdrawal) deduction against a debt: its own line."""

    debt = models.ForeignKey(RecoveryDebt, on_delete=models.PROTECT, related_name='deductions')
    withdrawal_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    wallet_transaction = models.ForeignKey('rider.WalletTransaction', null=True, blank=True,
                                           on_delete=models.PROTECT, related_name='+')
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default='INR')
    payout_gross_minor = models.BigIntegerField(default=0)
    percent_cap = models.PositiveSmallIntegerField(default=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class PayoutHold(models.Model):
    """A full payout hold on a SUSPENDED driver, for serious fraud, approved by a second admin.

    Active while `status == 'active'` and `expires_at` is in the future. At `expires_at` it lapses
    (the driver is paid again) unless renewed; renewing is another checker request. Releasing early
    needs no checker (it only pays out).
    """

    ACTIVE, RELEASED, LAPSED = 'active', 'released', 'lapsed'
    STATUS_CHOICES = [(ACTIVE, 'Active'), (RELEASED, 'Released'), (LAPSED, 'Lapsed')]

    driver = models.ForeignKey('driver.Driver', on_delete=models.CASCADE,
                               related_name='payout_holds')
    flag = models.ForeignKey(RiskFlag, on_delete=models.PROTECT, related_name='payout_holds')
    approval_request = models.OneToOneField('approvals.ApprovalRequest', on_delete=models.PROTECT,
                                            related_name='+')
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=ACTIVE, db_index=True)
    starts_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    max_days = models.PositiveSmallIntegerField(default=14)
    renewal_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='renewals')
    note = models.CharField(max_length=NOTE_MAX, blank=True, default='')
    released_at = models.DateTimeField(null=True, blank=True)
    released_by_id = models.BigIntegerField(null=True, blank=True)
    release_note = models.CharField(max_length=NOTE_MAX, blank=True, default='')
    expiry_alerted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=['driver'], condition=Q(status='active'),
                                    name='payouthold_one_active_per_driver'),
        ]
