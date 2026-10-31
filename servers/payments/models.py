from django.db import models
from django.utils import timezone
from django.contrib.auth import get_user_model
from base.currency_stamp import CurrencyStampedModel
from base.money import MinorUnitsAmount

User = get_user_model()


class PaymentGateway(models.TextChoices):
    """Supported payment gateways."""
    CASHFREE = 'cashfree', 'Cashfree'
    # Add more gateways as needed


class Payment(CurrencyStampedModel, models.Model):
    # Where this row's currency is derived from. See base/currency_stamp.py.
    #  A payment is for a trip.
    currency_source_fields = ('trip_id',)

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]
    METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('online', 'Online'),
        ('wallet',"Wallet"),
        ('deferred', 'Deferred')
    ]

    trip_id = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='payments')
    user_id = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments')
    # Minor units of `currency` (base/money.py); `amount` is the Decimal view.
    amount_minor = models.BigIntegerField()
    amount = MinorUnitsAmount('amount_minor')
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    # Stamped at write time from the trip's zone. Denormalised on purpose: the
    # currency a row was written in is a fact about that row, not a lookup
    # against today's zone configuration.
    currency = models.CharField(max_length=3, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Payment gateway
    payment_gateway = models.CharField(
        max_length=20,
        choices=PaymentGateway.choices,
        default=PaymentGateway.CASHFREE,
        db_index=True
    )
    
    # Generic gateway fields
    gateway_order_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    gateway_payment_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    gateway_signature = models.CharField(max_length=512, blank=True, null=True)
    gateway_metadata = models.JSONField(default=dict, blank=True)  # Store gateway-specific data
    
    # Cashfree-specific fields (for backward compatibility with existing data)
    cashfree_order_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    cashfree_payment_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    cashfree_payment_session_id = models.CharField(max_length=256, blank=True, null=True)
    
    # Legacy fields kept for compatibility
    driver_txn_id = models.CharField(max_length=256, blank=True, null=True)
    driver_name = models.CharField(max_length=256, blank=True, null=True)

    # Payment method switching fields
    replaced_by = models.OneToOneField(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='replaces'
    )
    switch_reason = models.CharField(
        max_length=50, null=True, blank=True,
        choices=[('user_requested', 'User Requested'), ('system_error', 'System Error')]
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Payment {self.id} - Trip {self.trip_id.id} ({self.status})'

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Finance reads and the daily reconciliation scan payments by a
            # created_at window; without this they scan the whole table.
            models.Index(fields=['-created_at'], name='payment_created_idx'),
        ]
        constraints = [
            # Belt-and-braces against the switch/retry double-pay vector:
            # at most one *completed* Payment may exist per trip. PR #8
            # added a runtime poll-then-settle guard; this is the DB-level
            # invariant that catches anything the application-layer guard
            # misses (e.g. a race that slipped past select_for_update).
            models.UniqueConstraint(
                fields=['trip_id'],
                condition=models.Q(status='completed'),
                name='one_completed_payment_per_trip',
            ),
        ]


class TransactionHistory(CurrencyStampedModel, models.Model):
    # Where this row's currency is derived from. See base/currency_stamp.py.
    #  trip_id is nullable here (a withdrawal has no trip), so this falls
    #  back to the default, which is correct: a payout is in the driver's
    #  home-market currency and there is no trip to ask.
    currency_source_fields = ('trip_id',)

    TXN_TYPE_CHOICES = [
        ('payment', 'Payment from Rider'),
        ('credit', 'Credit to Driver'),
        ('debit', 'Debit from Wallet'),
        ('refund', 'Refund to Rider'),
    ]

    def _derive_currency(self):
        """The trip's currency; a payout (no trip) takes its withdrawal's, else
        the driver's wallet's. Never a platform default: a US payout row must
        not be stamped in rupees."""
        from base.money import normalize_currency

        found = super()._derive_currency()
        if found:
            return found
        withdrawal = self.withdrawal_request if self.withdrawal_request_id else None
        if withdrawal is not None and withdrawal.currency:
            return normalize_currency(withdrawal.currency)
        try:
            from servers.rider.models import Wallet

            wallet_currency = (Wallet.objects
                               .filter(user_id=self.driver_id.user_id_id).order_by('scope')
                               .values_list('currency', flat=True).first())
        except Exception:  # noqa: BLE001 - no driver yet: nothing to derive
            wallet_currency = None
        return normalize_currency(wallet_currency) if wallet_currency else None

    trip_id = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='transactions', null=True, blank=True)
    user_id = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    driver_id = models.ForeignKey('driver.Driver', on_delete=models.CASCADE, related_name='transactions')
    withdrawal_request = models.ForeignKey('driver.WithdrawalRequest', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    # Minor units of `currency` (base/money.py); `amount` is the Decimal view.
    amount_minor = models.BigIntegerField()
    amount = MinorUnitsAmount('amount_minor')
    method = models.CharField(max_length=50)
    # Stamped at write time from the trip's zone. Denormalised on purpose: the
    # currency a row was written in is a fact about that row, not a lookup
    # against today's zone configuration.
    currency = models.CharField(max_length=3, db_index=True)
    
    # Payment gateway fields
    payment_gateway = models.CharField(
        max_length=20,
        choices=PaymentGateway.choices,
        default=PaymentGateway.CASHFREE,
        db_index=True
    )
    gateway_payment_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    gateway_transaction_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    
    # Cashfree-specific fields (for backward compatibility with existing data)
    cashfree_payment_id = models.CharField(max_length=256, blank=True, null=True)
    cashfree_transfer_id = models.CharField(max_length=256, blank=True, null=True)  # For payouts
    
    user_name = models.CharField(max_length=256, blank=True, null=True)
    user_txn_id = models.CharField(max_length=256, blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    txn_type = models.CharField(max_length=20, choices=TXN_TYPE_CHOICES, default='payment')
    gateway_metadata = models.JSONField(default=dict, blank=True)  # Store gateway-specific data
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Transaction {self.id} - Trip {self.trip_id.id}'

    class Meta:
        verbose_name_plural = 'Transaction histories'
        ordering = ['-created_at']
        indexes = [
            # Payouts paid per day (Finance overview) filter by created_at.
            models.Index(fields=['-created_at'], name='txnhistory_created_idx'),
        ]


class WebhookEvent(models.Model):
    """Idempotency record for inbound webhook deliveries.

    Webhook delivery is at-least-once: a gateway will retry a delivery
    until it sees a 2xx. Without dedupe, the same signed payload —
    whether a legitimate retry or a captured-and-replayed one — fires
    the side effects (status flips, driver credits) more than once.

    We key on (gateway, dedupe_key) and use the row's existence as the
    idempotency gate: a duplicate insertion attempt fails the unique
    constraint and the handler short-circuits with a 200 'already
    processed' response.

    `dedupe_key` is whatever uniquely identifies a single delivery for
    the gateway. For Cashfree we prefer the webhook signature (which
    is timestamp+body HMAC'd with the secret — varies per delivery
    even for replays of the same event), falling back to a composite
    of cf_payment_id + event_type.
    """
    gateway = models.CharField(max_length=32, db_index=True)
    dedupe_key = models.CharField(max_length=512)
    event_type = models.CharField(max_length=64, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=32, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['gateway', 'dedupe_key'],
                name='webhook_event_unique_per_gateway',
            ),
        ]
        indexes = [
            # Name pinned to match the 0009_webhookevent migration to keep
            # `makemigrations --check` clean.
            models.Index(fields=['gateway', '-received_at'], name='webhook_event_gw_recv_idx'),
        ]
        ordering = ['-received_at']

    def __str__(self):
        return f'WebhookEvent {self.gateway}:{self.dedupe_key[:32]}'


class TripSettlement(CurrencyStampedModel, models.Model):
    """Immutable economic evidence for one settled trip.

    `WalletTransaction` stays the authoritative ledger: it says what money moved.
    This says WHY, and it is the difference between being able to answer a dispute
    and having to recompute one. Today `admin_dashboard` calls
    `commission_percent_for_trip` at RENDER time, so editing a rate card silently
    changes last month's reported platform revenue, and the driver-earnings report
    has to infer commission from the ledger entry's direction. Both of those stop
    once something reads this table.

    Deliberately not columns on `WalletTransaction`. That model is generic -- rider
    top-ups, refunds, promo credits, support credits, withdrawals and payouts all use
    it -- so trip economics there would be null on the large majority of rows and
    would put a commission rate on a rider's wallet top-up. The FK in this direction
    keeps both models honest.

    No tax field. GST is a rider-side tax on the fare and already lives on the
    versioned `Receipt`; the platform commission's tax treatment is an accounting
    matter nobody has stated a per-trip rule for, and inventing a column for it now
    would be guessing.

    **Corrections, and how this differs from the approved design.** That design
    specified `trip` as a OneToOne ("one settlement per trip") and also `version`
    for corrections written as new rows. Those two cannot both hold -- a OneToOne
    permits exactly one row, so no correction could ever be inserted. Resolved in
    favour of keeping corrections possible:

      * `trip` is a ForeignKey;
      * `(trip, version)` is unique, so a version cannot be written twice;
      * a PARTIAL unique index guarantees exactly one row at `version=1` per trip,
        which is the "one normal settlement per trip" guarantee the design wanted;
      * corrections are `version + 1` and the current settlement is the highest
        version. There is no update path.
    """

    # Where this row's currency is derived from. See base/currency_stamp.py.
    #  Settlement is per trip; note the FK is `trip`, not `trip_id`.
    currency_source_fields = ('trip',)

    SOURCE_NATIVE = 'native'
    SOURCE_RECONSTRUCTED = 'reconstructed'
    SOURCE_CHOICES = [
        (SOURCE_NATIVE, 'Written at settlement time'),
        (SOURCE_RECONSTRUCTED, 'Reconstructed from historical facts'),
    ]

    trip = models.ForeignKey(
        'ride.Trip', on_delete=models.PROTECT, related_name='settlements',
    )
    # Denormalised on purpose: a trip's driver assignment is mutable, a settlement's
    # is not. Who was paid for this ride is a fact about the past.
    driver = models.ForeignKey(
        'driver.Driver', on_delete=models.PROTECT, related_name='settlements',
    )
    # The ledger row that actually moved the money. Nullable only so a
    # reconstructed row for a historical trip can point at nothing when the link
    # cannot be established honestly.
    wallet_transaction = models.ForeignKey(
        'rider.WalletTransaction', on_delete=models.PROTECT,
        null=True, blank=True, related_name='settlements',
    )

    # The fare the settlement was computed on.
    # Amounts are minor units of `currency` (base/money.py); the bare names are
    # Decimal views.
    gross_fare_minor = models.BigIntegerField()
    # The rate AS APPLIED, not a pointer to a card that can be superseded.
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    # Stored rather than derived, because rounding is part of the answer.
    commission_amount_minor = models.BigIntegerField()
    # gross_fare - commission_amount, stored so the identity is asserted at write
    # time rather than recomputed by every reader. In integers it is exact.
    driver_net_minor = models.BigIntegerField()
    gross_fare = MinorUnitsAmount('gross_fare_minor')
    commission_amount = MinorUnitsAmount('commission_amount_minor')
    driver_net = MinorUnitsAmount('driver_net_minor')
    # Stamped at write time from the trip's zone. Denormalised on purpose: the
    # currency a row was written in is a fact about that row, not a lookup
    # against today's zone configuration.
    currency = models.CharField(max_length=3, db_index=True)

    # Decides the ledger's direction and who is holding the fare meanwhile.
    payment_method = models.CharField(max_length=32, blank=True, default='')

    settled_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)
    source = models.CharField(
        max_length=16, choices=SOURCE_CHOICES, default=SOURCE_NATIVE, db_index=True,
    )
    # Free text, only for a reconstructed or corrected row: what was assumed.
    provenance_note = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['trip', 'version'],
                name='tripsettlement_unique_trip_version',
            ),
            # Exactly one ORIGINAL settlement per trip, enforced by the database
            # rather than by the one function that writes it -- a management
            # command, a data migration or a second worker all bypass application
            # logic, and "this ride was settled once" has to survive that.
            models.UniqueConstraint(
                fields=['trip'],
                condition=models.Q(version=1),
                name='tripsettlement_one_original_per_trip',
            ),
            models.CheckConstraint(
                condition=models.Q(commission_amount_minor__gte=0),
                name='tripsettlement_commission_non_negative',
            ),
            models.CheckConstraint(
                condition=models.Q(gross_fare_minor__gte=0),
                name='tripsettlement_gross_non_negative',
            ),
        ]
        indexes = [
            models.Index(fields=['driver', '-settled_at'],
                         name='tripsettlement_driver_idx'),
            models.Index(fields=['-settled_at'], name='tripsettlement_recent_idx'),
        ]

    def __str__(self):
        return (f'Settlement trip={self.trip_id} v{self.version} '
                f'net={self.driver_net}')

    @property
    def is_reconstructed(self):
        return self.source == self.SOURCE_RECONSTRUCTED


# ---------------------------------------------------------------------------
# Payout configuration per country
# ---------------------------------------------------------------------------

class PayoutCountryConfig(models.Model):
    """The payout rules of ONE country, versioned. Replaces two hard-coded numbers.

    Until this model existed the platform fee (`fee_percent = 2.0`) and the
    smallest withdrawal (`minimum_withdrawal: 500.0`) were literals in
    `driver/views.py`, and a currency table in `driver/services.py` decided the
    minimum for the two currencies someone had thought of. A country with no row
    here has NO payout rules: the driver balance endpoint says "withdrawals not
    available in your country yet" and a withdrawal request is refused with
    `WITHDRAWALS_NOT_AVAILABLE`. There is deliberately no fallback to another
    country's numbers (the owner decides a new country's payout values).

    `minimum_withdrawal_minor` is in minor units of the country's own currency
    (`base.countries`). `stuck_payout_hours` is how long a payout may sit in
    `processed` before reconciliation raises it; NULL means "not configured" and
    the stuck-payout check is skipped for that country.

    Versioned like `ApprovalPolicy`: a change is a new row, the old one closes.
    Edits are the maker-checker action `finance.payout_config_change` (Part B).
    """

    country = models.CharField(max_length=2, db_index=True)
    #: The country's own currency (`base.countries`), filled on save. The minimum
    #: is an amount, so it carries the currency it is in.
    currency = models.CharField(max_length=3, blank=True)
    fee_percent = models.DecimalField(max_digits=5, decimal_places=2)
    minimum_withdrawal_minor = models.BigIntegerField()
    stuck_payout_hours = models.PositiveIntegerField(null=True, blank=True)

    # --- schedule (Part B, owner ruling AUDIT 27.43) -------------------------
    ON_REQUEST = 'on_request'
    WEEKLY = 'weekly'
    SCHEDULE_CHOICES = [(ON_REQUEST, 'On request'), (WEEKLY, 'Weekly')]
    #: `on_request`: a driver asks. `weekly`: a scheduled run creates the
    #: withdrawals on `payout_weekday` (Monday = 0) in the country's own zone.
    payout_schedule = models.CharField(
        max_length=12, choices=SCHEDULE_CHOICES, default=ON_REQUEST)
    payout_weekday = models.PositiveSmallIntegerField(null=True, blank=True)
    # Instant cash-out is NOT built. These two fields only keep the model open
    # for it; nothing reads them.
    instant_cashout_enabled = models.BooleanField(default=False)
    instant_cashout_fee_flat_minor = models.BigIntegerField(null=True, blank=True)

    # --- fraud money actions (owner rulings 2026-10-31, AUDIT 27.56) ---------
    #: Days an unpaid reward linked to an undecided fraud flag stays "pending review".
    reward_hold_days = models.PositiveSmallIntegerField(default=7)
    #: The most one single payout is reduced by a recovery of an approved reward clawback.
    recovery_max_percent = models.PositiveSmallIntegerField(default=50)
    #: The longest a full payout hold (with a suspension) lasts before it must be renewed.
    payout_hold_max_days = models.PositiveSmallIntegerField(default=14)

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

    class Meta:
        ordering = ['country', '-version']
        constraints = [
            models.UniqueConstraint(
                fields=['country'], condition=models.Q(is_active=True),
                name='payout_config_one_active_per_country',
            ),
            models.UniqueConstraint(
                fields=['country', 'version'], name='payout_config_version_unique',
            ),
            models.CheckConstraint(
                condition=models.Q(fee_percent__gte=0) & models.Q(fee_percent__lte=100),
                name='payout_config_fee_percent_range',
            ),
            models.CheckConstraint(
                condition=models.Q(minimum_withdrawal_minor__gte=0),
                name='payout_config_minimum_not_negative',
            ),
            models.CheckConstraint(
                condition=(models.Q(payout_weekday__isnull=True)
                           | models.Q(payout_weekday__lte=6)),
                name='payout_config_weekday_range',
            ),
        ]

    def __str__(self):
        return f'PayoutCountryConfig {self.country} v{self.version}'

    def save(self, *args, **kwargs):
        from base.countries import get_country

        # A country's payout minimum is in that country's currency, always.
        self.country = str(self.country).upper()
        self.currency = get_country(self.country).currency
        return super().save(*args, **kwargs)

    def as_dict(self):
        from base.countries import get_country
        from base.money import from_minor

        currency = self.currency or get_country(self.country).currency
        return {
            'id': self.id,
            'country': self.country,
            'currency': currency,
            'fee_percent': str(self.fee_percent),
            'minimum_withdrawal_minor': self.minimum_withdrawal_minor,
            'minimum_withdrawal': str(from_minor(self.minimum_withdrawal_minor, currency)),
            'stuck_payout_hours': self.stuck_payout_hours,
            'payout_schedule': self.payout_schedule,
            'payout_weekday': self.payout_weekday,
            'reward_hold_days': self.reward_hold_days,
            'recovery_max_percent': self.recovery_max_percent,
            'payout_hold_max_days': self.payout_hold_max_days,
            'instant_cashout_enabled': self.instant_cashout_enabled,
            'instant_cashout_fee_flat_minor': self.instant_cashout_fee_flat_minor,
            'is_active': self.is_active,
            'version': self.version,
            'valid_from': self.valid_from,
            'valid_to': self.valid_to,
            'reason': self.reason,
            'created_by': {
                'id': self.created_by_id, 'label': self.created_by_label,
                'role': self.created_by_role,
            },
        }


# ---------------------------------------------------------------------------
# Reconciliation (local data only)
# ---------------------------------------------------------------------------

class ReconciliationRun(models.Model):
    """One day's reconciliation for (country, currency, gateway).

    `date` is the calendar day in the country's default timezone; the window it
    covers is that day's UTC bounds. The run is written once per key
    (`recon_run_unique`), so the daily task is idempotent.
    """

    OK = 'ok'
    ISSUES = 'issues'
    ERROR = 'error'
    STATUS_CHOICES = [(OK, 'No findings'), (ISSUES, 'Findings raised'), (ERROR, 'Run failed')]

    date = models.DateField(db_index=True)
    country = models.CharField(max_length=2, db_index=True)
    currency = models.CharField(max_length=3)
    gateway = models.CharField(max_length=32)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OK)
    #: `{kind: new items raised by this run}`.
    counts = models.JSONField(default=dict, blank=True)
    #: `{kind: 'ran' | 'not_configured'}` plus the statement-source state.
    checks = models.JSONField(default=dict, blank=True)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True, default='')

    class Meta:
        ordering = ['-date', 'country', 'currency']
        constraints = [
            models.UniqueConstraint(
                fields=['date', 'country', 'currency', 'gateway'], name='recon_run_unique',
            ),
        ]

    def __str__(self):
        return f'Reconciliation {self.date} {self.country}/{self.currency}/{self.gateway}'


class ReconciliationItem(models.Model):
    """One thing reconciliation found that a human should look at."""

    PAYMENT_NO_WEBHOOK = 'payment_no_webhook'
    WEBHOOK_NO_PAYMENT = 'webhook_no_payment'
    AMOUNT_MISMATCH = 'amount_mismatch'
    TRIP_NO_SETTLEMENT = 'trip_no_settlement'
    SETTLEMENT_GROSS_MISMATCH = 'settlement_gross_mismatch'
    PAYOUT_WEBHOOK_NOT_PAID = 'payout_webhook_not_paid'
    PAYOUT_STUCK = 'payout_stuck'
    KIND_CHOICES = [
        (PAYMENT_NO_WEBHOOK, 'Payment captured without a confirming webhook'),
        (WEBHOOK_NO_PAYMENT, 'Confirming webhook with no captured payment'),
        (AMOUNT_MISMATCH, 'Amount or currency differs between payment and webhook'),
        (TRIP_NO_SETTLEMENT, 'Completed trip with no settlement'),
        (SETTLEMENT_GROSS_MISMATCH, "Settlement gross differs from the trip's final fare"),
        (PAYOUT_WEBHOOK_NOT_PAID, 'Payout marked processed by webhook, withdrawal not paid'),
        (PAYOUT_STUCK, 'Payout stuck in processing'),
    ]
    KINDS = [k for k, _ in KIND_CHOICES]

    run = models.ForeignKey(ReconciliationRun, on_delete=models.CASCADE, related_name='items')
    kind = models.CharField(max_length=32, choices=KIND_CHOICES, db_index=True)
    #: One live finding per subject: a re-run does not raise it twice, and a
    #: resolved item is not reopened by the next day's run.
    dedupe_key = models.CharField(max_length=120, unique=True)
    country = models.CharField(max_length=2, db_index=True)
    currency = models.CharField(max_length=3)
    payment = models.ForeignKey(
        'payments.Payment', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    withdrawal = models.ForeignKey(
        'driver.WithdrawalRequest', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+')
    trip = models.ForeignKey(
        'ride.Trip', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    #: A gateway reference when no local row exists (a webhook with no payment).
    reference = models.CharField(max_length=256, blank=True, default='')
    expected = models.CharField(max_length=255, blank=True, default='')
    seen = models.CharField(max_length=255, blank=True, default='')
    #: The city the subject sits in, for scope. NULL = nothing placeable:
    #: unrestricted callers only.
    anchor_node = models.ForeignKey(
        'admin_access.GeoNode', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    resolved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    resolved_by_label = models.CharField(max_length=255, blank=True, default='')
    resolution_note = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['resolved_at', '-created_at'], name='recon_item_open_idx'),
            models.Index(fields=['run', 'kind'], name='recon_item_run_kind_idx'),
        ]

    def __str__(self):
        return f'ReconciliationItem {self.kind} {self.dedupe_key}'


class DriverPayoutNotice(models.Model):
    """OUTBOX: ask a driver to add payout details (the weekly run skipped them).

    The Notifications module does not exist yet, so the row is real and the SEND is a
    stub (`weekly_payout.deliver_payout_notice` logs and leaves it `queued`). Unique per
    (run, driver): running the same week twice never queues a second notice.
    """

    run = models.ForeignKey('payments.PayoutRun', on_delete=models.CASCADE, related_name='notices')
    driver = models.ForeignKey('driver.Driver', on_delete=models.CASCADE, related_name='payout_notices')
    kind = models.CharField(max_length=32, default='add_payout_details')
    status = models.CharField(max_length=10, default='queued', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'driver'],
                                               name='payout_notice_one_per_run_driver')]

    def __str__(self):
        return f'PayoutNotice run={self.run_id} driver={self.driver_id} {self.status}'


class PayoutRun(models.Model):
    """One scheduled weekly payout run for a country: the idempotency record.

    `(country, week_start)` is unique, so the beat task can fire every hour and
    still create each week's withdrawals and its ONE approval request exactly
    once. `week_start` is the Monday of the week in the country's own zone.
    """

    country = models.CharField(max_length=2, db_index=True)
    currency = models.CharField(max_length=3)
    week_start = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    withdrawal_count = models.PositiveIntegerField(default=0)
    amount_minor = models.BigIntegerField(default=0)
    #: The payout-run ApprovalRequest, when one was opened.
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    #: `opened`, `blocked_no_policy`, `nothing_to_pay`.
    outcome = models.CharField(max_length=24, default='opened')
    #: True when the per-run cap cut the list short (the rest waits a week).
    truncated = models.BooleanField(default=False)
    #: Drivers the run SKIPPED, with why: `[{driver_id, reason, balance_minor}]`.
    #: Today the only reason is `no_payout_handle` (no UPI / bank fund account).
    skipped = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-week_start', 'country']
        constraints = [
            models.UniqueConstraint(fields=['country', 'week_start'],
                                    name='payout_run_one_per_country_week'),
        ]

    def __str__(self):
        return f'PayoutRun {self.country} {self.week_start}'


class PlatformExpense(models.Model):
    """Money the PLATFORM owes that belongs on no driver and no rider wallet.

    Today one kind: `platform_regulatory_fee_expense`, the per-trip regulatory fee on a trip an
    operator ended under the platform-funded rule (the rider was charged 0, so none of it is
    charged to the rider). Shown in the Finance overview's platform-funded section. One row per
    (kind, trip). US accountant to confirm (go-live blocker #15).
    """

    REGULATORY_FEE = 'platform_regulatory_fee_expense'
    KIND_CHOICES = [(REGULATORY_FEE, 'Per-trip regulatory fee (platform expense)')]

    kind = models.CharField(max_length=40, choices=KIND_CHOICES, db_index=True)
    trip_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, db_index=True)
    zone_code = models.CharField(max_length=64, blank=True, default='')
    note = models.CharField(max_length=300, blank=True, default='')
    idempotency_key = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.CheckConstraint(condition=models.Q(amount_minor__gt=0),
                                              name='platformexpense_amount_positive')]

    def __str__(self):
        return f'{self.kind} {self.amount_minor} {self.currency} trip={self.trip_id}'
