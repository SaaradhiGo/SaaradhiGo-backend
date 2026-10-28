"""Maker-checker: the policy that decides whether a second admin is needed,
and the request that waits for one.

THE SHAPE
---------
    ApprovalPolicy   (country, action) -> "a maker may do this alone up to X"
    ApprovalRequest  one maker's attempt to do something that needed a checker

A policy is PER COUNTRY, in that country's currency, in integer minor units
(paise, cents) -- the same rule as every other amount in this codebase
(`base/money.py`). Rupee and dollar limits are never compared with each
other, and a request whose currency does not match its policy is refused
rather than converted.

WHY A COUNTRY WITH NO POLICY IS BLOCKED, NOT DEFAULTED
------------------------------------------------------
The tempting default -- "no row, so no limit" or "no row, so use India's"
-- is a decision about US money taken by whoever forgot to add the row. The
owner's ruling is the opposite: an action that needs a threshold in a
country that has none is refused with `THRESHOLD_NOT_CONFIGURED` until a
named person configures one. The refusal is loud so the gap is closed by a
decision, not discovered by a payout.

WHY POLICIES ARE VERSIONED ROWS AND NEVER EDITED
------------------------------------------------
"What was the limit when this refund went out alone?" is a question an
audit will ask. An edited row cannot answer it; a closed row beside a new
one can. So a change is a new row with `version + 1`, the previous row
gets `valid_to` and `is_active=False`, and exactly one row per (country,
action) is active at a time -- enforced by a partial unique constraint,
not by discipline. And a change is itself a maker-checker request, so the
row names the request that made it.

THE REQUEST CARRIES SNAPSHOTS, NOT FOREIGN KEYS, FOR ITS PEOPLE
---------------------------------------------------------------
Who asked and who approved are recorded the way the audit log records an
actor: id, label, email and role as they were at that moment. A checker
who later leaves, is renamed or loses the role still approved this request
under the authority they held then. The one constraint that matters most
is in the database: `maker_id` and `checker_id` may not be equal.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from base.countries import COUNTRIES
from servers.admin_audit.models import actor_snapshot

# --- policies -------------------------------------------------------------

#: What `maker_limit` is measured in.
MONEY, COUNT, ALWAYS = 'money', 'count', 'always'
LIMIT_KIND_CHOICES = [
    (MONEY, 'Minor units of `currency`'),
    (COUNT, 'Number of records'),
    # No amount is compared at all: this action needs a checker every time
    # (payout runs, pricing, permanent deactivation). `maker_limit` and
    # `currency` are empty, and the constraint below says so.
    (ALWAYS, 'Always needs a checker'),
]

COUNTRY_CHOICES = [(code, country.name) for code, country in COUNTRIES.items()]


class ApprovalPolicy(models.Model):
    """One version of the rule for (country, action)."""

    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES, db_index=True)
    action_key = models.CharField(max_length=64, db_index=True)
    limit_kind = models.CharField(max_length=8, choices=LIMIT_KIND_CHOICES, default=MONEY)
    #: ISO 4217, the country's local currency, when `limit_kind` is money.
    #: Blank otherwise.
    currency = models.CharField(max_length=3, blank=True, default='')
    #: Up to and including this, the maker acts alone. In minor units of
    #: `currency` for money, a record count for count. NULL means there is
    #: no amount at which a maker acts alone: always a checker.
    maker_limit = models.BigIntegerField(null=True, blank=True)

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
        on_delete=models.PROTECT, related_name='policies_written',
    )

    class Meta:
        ordering = ['country', 'action_key', '-version']
        constraints = [
            # Exactly one live rule per (country, action).
            models.UniqueConstraint(
                fields=['country', 'action_key'], condition=Q(is_active=True),
                name='approvals_one_active_policy',
            ),
            models.UniqueConstraint(
                fields=['country', 'action_key', 'version'],
                name='approvals_policy_version_unique',
            ),
            models.CheckConstraint(
                condition=Q(limit_kind__in=[MONEY, COUNT, ALWAYS]),
                name='approvals_policy_limit_kind_enum',
            ),
            # money <-> a currency; count and always <-> no currency.
            models.CheckConstraint(
                condition=(
                    (Q(limit_kind=MONEY) & ~Q(currency=''))
                    | (Q(limit_kind__in=[COUNT, ALWAYS]) & Q(currency=''))
                ),
                name='approvals_policy_currency_matches_kind',
            ),
            # "Always" has no limit by definition.
            models.CheckConstraint(
                condition=~Q(limit_kind=ALWAYS) | Q(maker_limit__isnull=True),
                name='approvals_policy_always_has_no_limit',
            ),
            models.CheckConstraint(
                condition=Q(maker_limit__isnull=True) | Q(maker_limit__gte=0),
                name='approvals_policy_limit_not_negative',
            ),
        ]

    def __str__(self):
        return f'{self.country}:{self.action_key} v{self.version}'

    @property
    def always_checker(self):
        return self.limit_kind == ALWAYS or self.maker_limit is None

    def set_created_by(self, user):
        self.set_created_by_snapshot(actor_snapshot(user))

    def set_created_by_snapshot(self, snapshot):
        """From an audit-shaped snapshot (`actor_id`, `actor_label`, ...).

        The policy-change executor has the maker only as the snapshot on
        the request, which is the right source: the person as they were
        when they proposed it, not as they are when a checker approves."""
        self.created_by_id = snapshot.get('actor_id')
        self.created_by_label = snapshot.get('actor_label') or ''
        self.created_by_email = snapshot.get('actor_email') or ''
        self.created_by_role = snapshot.get('actor_role') or ''

    def as_dict(self):
        return {
            'id': self.id,
            'country': self.country,
            'action_key': self.action_key,
            'limit_kind': self.limit_kind,
            'currency': self.currency,
            'maker_limit': self.maker_limit,
            'always_checker': self.always_checker,
            'is_active': self.is_active,
            'version': self.version,
            'valid_from': self.valid_from.isoformat() if self.valid_from else None,
            'valid_to': self.valid_to.isoformat() if self.valid_to else None,
            'reason': self.reason,
            'created_by': {
                'id': self.created_by_id, 'email': self.created_by_email,
                'label': self.created_by_label, 'role': self.created_by_role,
            },
            'changed_via_request_id': self.changed_via_id,
        }


# --- requests -------------------------------------------------------------

REQUESTED, APPROVED, REJECTED, EXECUTED, FAILED, CANCELLED, EXPIRED = (
    'requested', 'approved', 'rejected', 'executed', 'failed', 'cancelled', 'expired',
)
STATUS_CHOICES = [
    (REQUESTED, 'Requested: waiting for a checker'),
    # Transitional: the checker said yes and the executor is running. A row
    # left here means the process died between the two; it is the state an
    # operator looks for after an incident.
    (APPROVED, 'Approved: executing'),
    (REJECTED, 'Rejected by the checker'),
    (EXECUTED, 'Executed'),
    (FAILED, 'Approved, but execution failed'),
    (CANCELLED, 'Cancelled by the maker'),
    (EXPIRED, 'Expired before a checker acted'),
]
ALL_STATUSES = frozenset(code for code, _ in STATUS_CHOICES)
#: States in which the idempotency key is held: the request is still open,
#: or it was carried out. A rejected, cancelled, expired or failed request
#: releases its key, so the maker may raise the same thing again -- a
#: rejected payout re-requested after the dispute is settled is a new
#: request, with a new checker's name on it.
KEY_HELD_STATUSES = (REQUESTED, APPROVED, EXECUTED)
TERMINAL_STATUSES = (REJECTED, EXECUTED, FAILED, CANCELLED, EXPIRED)

#: Hours a request waits for a checker before it lapses. Settable because a
#: payout batch raised on Friday evening should still be approvable on
#: Monday morning, and a refund request from last quarter should not be.
DEFAULT_TTL_HOURS = 72


def request_ttl():
    hours = getattr(settings, 'APPROVAL_REQUEST_TTL_HOURS', DEFAULT_TTL_HOURS)
    return timezone.timedelta(hours=int(hours))


class ApprovalRequest(models.Model):
    """One maker's attempt at an action that needed a checker.

    A within-limit action also leaves a row here, already `executed`, so
    the idempotency key protects both paths and "everything this maker did
    under the framework" is one query.
    """

    action_key = models.CharField(max_length=64, db_index=True)
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES, db_index=True)
    #: Where this happened, for scope. The country node when the caller
    #: gave nothing narrower; NULL only if the country has no node at all,
    #: in which case the row is visible at Global only (the same rule as a
    #: driver with no home city).
    scope_node = models.ForeignKey(
        'admin_access.GeoNode', null=True, blank=True, on_delete=models.PROTECT,
        related_name='approval_requests',
    )
    #: Minor units of `currency`; or a record count for a count policy;
    #: NULL for an always-checker action with no amount.
    amount = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True, default='')
    #: Which policy version decided this request needed a checker (or did
    #: not); NULL for the built-in always-checker rule.
    policy = models.ForeignKey(
        ApprovalPolicy, null=True, blank=True, on_delete=models.PROTECT,
        related_name='requests',
    )

    target_type = models.CharField(max_length=64, blank=True, default='', db_index=True)
    target_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    request_reason = models.TextField(blank=True, default='')

    maker_id = models.BigIntegerField(db_index=True)
    maker_label = models.CharField(max_length=255, blank=True, default='')
    maker_email = models.CharField(max_length=254, blank=True, default='')
    maker_role = models.CharField(max_length=64, blank=True, default='')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=REQUESTED,
                              db_index=True)
    requested_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField(db_index=True)

    checker_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    checker_label = models.CharField(max_length=255, blank=True, default='')
    checker_email = models.CharField(max_length=254, blank=True, default='')
    checker_role = models.CharField(max_length=64, blank=True, default='')
    decision_reason = models.TextField(blank=True, default='')
    decided_at = models.DateTimeField(null=True, blank=True)

    executed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default='')
    error = models.TextField(blank=True, default='')

    #: Caller-chosen. Held while the request is open or executed; see
    #: `KEY_HELD_STATUSES`.
    idempotency_key = models.CharField(max_length=200, db_index=True)

    class Meta:
        ordering = ['-requested_at', '-id']
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=sorted(ALL_STATUSES)),
                name='approvals_request_status_enum',
            ),
            # The rule the whole framework exists for, held by the database.
            models.CheckConstraint(
                condition=Q(checker_id__isnull=True) | ~Q(checker_id=F('maker_id')),
                name='approvals_checker_is_not_maker',
            ),
            models.UniqueConstraint(
                fields=['idempotency_key'],
                condition=Q(status__in=list(KEY_HELD_STATUSES)),
                name='approvals_idempotency_key_held',
            ),
            models.CheckConstraint(
                condition=Q(amount__isnull=True) | Q(amount__gte=0),
                name='approvals_request_amount_not_negative',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'expires_at'], name='approvals_req_status_exp_idx'),
            models.Index(fields=['target_type', 'target_id'], name='approvals_req_target_idx'),
        ]

    def __str__(self):
        return f'#{self.id} {self.action_key} {self.status}'

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = (self.requested_at or timezone.now()) + request_ttl()
        super().save(*args, **kwargs)

    # -- snapshots ---------------------------------------------------------

    def set_maker(self, user):
        snapshot = actor_snapshot(user)
        self.maker_id = snapshot['actor_id']
        self.maker_label = snapshot['actor_label']
        self.maker_email = snapshot['actor_email']
        self.maker_role = snapshot['actor_role']

    def set_checker(self, user):
        snapshot = actor_snapshot(user)
        self.checker_id = snapshot['actor_id']
        self.checker_label = snapshot['actor_label']
        self.checker_email = snapshot['actor_email']
        self.checker_role = snapshot['actor_role']

    @property
    def is_open(self):
        return self.status == REQUESTED

    @property
    def is_past_expiry(self):
        return bool(self.expires_at) and timezone.now() >= self.expires_at

    def as_dict(self):
        return {
            'id': self.id,
            'action_key': self.action_key,
            'country': self.country,
            'scope': self.scope_node.code if self.scope_node_id else None,
            'amount': self.amount,
            'currency': self.currency,
            'policy_id': self.policy_id,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'payload': self.payload,
            'request_reason': self.request_reason,
            'maker': {
                'id': self.maker_id, 'email': self.maker_email,
                'label': self.maker_label, 'role': self.maker_role,
            },
            'status': self.status,
            'requested_at': self.requested_at.isoformat() if self.requested_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'checker': {
                'id': self.checker_id, 'email': self.checker_email,
                'label': self.checker_label, 'role': self.checker_role,
            } if self.checker_id else None,
            'decision_reason': self.decision_reason,
            'decided_at': self.decided_at.isoformat() if self.decided_at else None,
            'executed_at': self.executed_at.isoformat() if self.executed_at else None,
            'result': self.result,
            'error_code': self.error_code,
            'error': self.error,
            'idempotency_key': self.idempotency_key,
        }
