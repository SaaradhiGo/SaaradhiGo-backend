from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class PaymentGateway(models.TextChoices):
    """Supported payment gateways."""
    CASHFREE = 'cashfree', 'Cashfree'
    # Add more gateways as needed


class Payment(models.Model):
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
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
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


class TransactionHistory(models.Model):
    TXN_TYPE_CHOICES = [
        ('payment', 'Payment from Rider'),
        ('credit', 'Credit to Driver'),
        ('debit', 'Debit from Wallet'),
        ('refund', 'Refund to Rider'),
    ]

    trip_id = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='transactions', null=True, blank=True)
    user_id = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    driver_id = models.ForeignKey('driver.Driver', on_delete=models.CASCADE, related_name='transactions')
    withdrawal_request = models.ForeignKey('driver.WithdrawalRequest', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=50)
    
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


class TripSettlement(models.Model):
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
    gross_fare = models.DecimalField(max_digits=10, decimal_places=2)
    # The rate AS APPLIED, not a pointer to a card that can be superseded.
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    # Stored rather than derived, because rounding is part of the answer.
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2)
    # gross_fare - commission_amount, stored so the identity is asserted at write
    # time rather than recomputed by every reader.
    driver_net = models.DecimalField(max_digits=10, decimal_places=2)

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
                condition=models.Q(commission_amount__gte=0),
                name='tripsettlement_commission_non_negative',
            ),
            models.CheckConstraint(
                condition=models.Q(gross_fare__gte=0),
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
