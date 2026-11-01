from django.db import models
from django.contrib.auth import get_user_model
from base.media import PrefixedUUIDPath, validate_document_file, validate_file_size, validate_image_file
from base.storage_backends import private_document_storage, public_media_storage
from base.currency_stamp import CurrencyStampedModel
from base.money import MinorUnitsAmount
from base.db_indexes import PatternPrefixIndex
from servers.driver.plates import normalised_plate_expression
# Create your models here.
User=get_user_model()
class Driver(models.Model):

    user_id=models.OneToOneField(User,on_delete=models.CASCADE,related_name='driver')
    license_doc=models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('license_docs'),
        validators=[validate_document_file, validate_file_size],
    )
    license_doc_back=models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('license_docs_back'),
        validators=[validate_document_file, validate_file_size],
    )
    license_expiry=models.DateField(blank=True,null=True)
    status=models.CharField(max_length=20,choices=[
        ('online','Online'),('off','Off'),('active','Active'),
        ('on ride','On Ride'),('off ride','Off Ride'),('blocked','Blocked')
    ],default='off')
    total_trips=models.IntegerField(default=0)
    doc_status=models.CharField(max_length=20,choices=[
        ('pending','Pending'),('approved','Approved'),('rejected','Rejected')
    ],default='pending')
    ratings=models.DecimalField(max_digits=3,decimal_places=2,default=0.00)
    approved=models.BooleanField(default=False)
    active_vehicle=models.ForeignKey('Vehicle',on_delete=models.SET_NULL,null=True,blank=True)
    last_withdrawal_at=models.DateTimeField(null=True,blank=True)
    upi_id=models.CharField(max_length=256,blank=True,null=True,help_text='UPI ID for UPI payouts')
    uploaded_timestamp=models.DateTimeField(null=True,blank=True)
    doc_status_updated_at=models.DateTimeField(null=True,blank=True)
    doc_rejection_reason=models.TextField(blank=True,null=True)
    # If set in the future, the driver is locked out of going online
    # (and out of accepting new trips) until this timestamp. Used by:
    #   - MVA 2020 12h/24h fatigue cap (servers.driver.services)
    #   - Rolling cancellation cooldown (3 cancels in 24h -> 1h lockout)
    # Nullable so legacy drivers continue to work; default unlocked.
    fatigue_lockout_until=models.DateTimeField(null=True,blank=True,db_index=True)
    # SIMULATED dispatch eligibility for a QA test account. NOT an approval.
    #
    # `approved` is the output of a real MVA-2020 document review and stays
    # the only thing that means that. This flag says "this is a test account
    # and we are pretending, in QA". It is honoured only where
    # servers.driver.qa_eligibility.qa_simulation_allowed() is true -- an
    # explicit QA environment -- so the same row in production grants
    # nothing. A driver carrying this has doc_status 'pending' and shows as
    # KYC PENDING in the console, which is the truth.
    qa_simulated_eligibility=models.BooleanField(
        default=False,
        db_index=True,
        help_text=(
            'QA test account: simulated dispatch eligibility. Never a KYC '
            'approval, and ignored outside a QA environment.'
        ),
    )

    # ------------------------------------------------------------------
    # ADMIN SCOPE ANCHOR
    # ------------------------------------------------------------------
    # Which city's operations team owns this driver. It exists so a
    # Hyderabad City Ops Manager's driver list is Hyderabad's drivers, and
    # so a US-scoped admin cannot open an Indian driver's identity
    # documents. It is NOT dispatch, pricing or eligibility: nothing in the
    # rider or driver app reads it, and a driver is not restricted to
    # driving here.
    #
    # Nullable, and null means UNASSIGNED rather than "everywhere": an
    # unassigned driver is visible only at Global scope. That is the safe
    # direction, but it is also a hole -- a driver nobody can see is a
    # driver whose KYC nobody reviews -- so `check_geo_links` counts them
    # and the KYC queue reports the count rather than silently filtering
    # them away.
    home_city=models.ForeignKey(
        'admin_access.GeoNode',
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name='drivers',
        help_text=(
            'City whose operations team owns this driver, for admin scoping '
            'only. Does not limit where the driver may drive.'
        ),
    )

    def __str__(self) -> str:
        return self.user_id.full_name if self.user_id.full_name else self.user_id.phone_number
class VehicleType(models.Model):
    type=models.CharField(max_length=50,unique=True)
    description=models.TextField(blank=True,null=True)
    def __str__(self):
        return self.type
class Vehicle(models.Model):

    driver_id = models.ForeignKey(
        Driver,
        on_delete=models.CASCADE
    )

    rc_doc = models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('rc_docs'),
        validators=[validate_document_file, validate_file_size],
    )

    permit_doc = models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('permit_docs'),
        validators=[validate_document_file, validate_file_size],
    )

    insurance_doc = models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('insurance_docs'),
        validators=[validate_document_file, validate_file_size],
    )

    fitness_doc = models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('fitness_docs'),
        validators=[validate_document_file, validate_file_size],
    )

    puc_doc = models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('puc_docs'),
        validators=[validate_document_file, validate_file_size],
    )

    # The US safety inspection. The five columns above are India's statutory
    # set; the US requires vehicle registration (that is `rc_doc` -- the same
    # document under another name), proof of insurance, and in most states a
    # periodic safety inspection, which had nowhere to be stored.
    #
    # Without this column `servers/driver/compliance.py` reported the
    # inspection as required-but-unrecordable, which correctly counts as NOT
    # compliant -- so a US driver could be onboarded as far as a permanent
    # block and no further. Counting it as satisfied instead would have
    # approved a driver whose inspection nobody ever saw.
    #
    # Nullable and unused in India, so no Indian row changes.
    inspection_doc = models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('inspection_docs'),
        validators=[validate_document_file, validate_file_size],
    )

    vehicle_type_id = models.ForeignKey(
        VehicleType,
        on_delete=models.CASCADE,
        related_name='vehicles'
    )

    brand = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    model = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    color = models.CharField(
        max_length=50,
        blank=True,
        null=True
    )

    year = models.IntegerField(
        blank=True,
        null=True
    )

    vehicle_number = models.CharField(
        max_length=20
    )

    capacity = models.IntegerField(
        default=1
    )

    vehicle_pic = models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=public_media_storage,
        upload_to=PrefixedUUIDPath('vehicle_pics'),
        validators=[validate_image_file, validate_file_size],
    )

    status = models.CharField(
        max_length=20,
        choices=[
            ('active', 'Active'),
            ('inactive', 'Inactive'),
            ('under_maintenance', 'Under Maintenance')
        ],
        default='active'
    )

    insurance_expiry = models.DateField(
        blank=True,
        null=True
    )

    permit_expiry = models.DateField(
        blank=True,
        null=True
    )

    fitness_expiry = models.DateField(
        blank=True,
        null=True
    )

    puc_expiry = models.DateField(
        blank=True,
        null=True
    )

    class Meta:
        indexes = [
            # Console search finds a plate however it was typed
            # (`TS09AB1234`, `ts-09-ab-1234`): the stored value is left as
            # the driver entered it and this indexes its normalised form,
            # the same expression `servers.search` filters on. On
            # PostgreSQL it carries `text_pattern_ops` so the prefix match
            # uses it too; see `base.db_indexes`.
            PatternPrefixIndex(normalised_plate_expression(), name='vehicle_plate_norm_idx'),
        ]

    def __str__(self) -> str:
        return f'{self.vehicle_number} - {self.driver_id}'
class DriverSession(models.Model):
    """One row per online -> offline interval for fatigue tracking.

    Motor Vehicles Aggregator Guidelines 2020 cap a driver's active
    duration at 12 hours in any rolling 24-hour window. We use this
    table to compute the rolling total at trip-accept time; the gate
    lives in `servers.driver.services.fatigue_check`. A session is
    created on driver_online (channels consumer) and closed on
    driver_offline; reconnects within a short grace period extend the
    same session rather than creating a new one.

    `duration_seconds` is the sum of completed wall-clock seconds; for
    a still-open session callers should add `now() - started_at` on top.
    """
    driver = models.ForeignKey(
        Driver,
        on_delete=models.CASCADE,
        related_name='sessions',
    )
    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    # Why the session ended: 'offline', 'disconnect_timeout',
    # 'forced_logout_fatigue', 'shift_change', 'admin_close'.
    end_reason = models.CharField(max_length=32, blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['driver', '-started_at'], name='session_driver_recent_idx'),
        ]
        ordering = ['-started_at']

    def __str__(self):
        return f'Session {self.id} driver={self.driver_id} {self.started_at}'


class DriverCancellation(models.Model):
    """Tracks driver-initiated cancellations after accepting a trip.

    Used by the rolling-cancellation cooldown rule: N cancellations
    in 24 hours triggers a temporary online lockout
    (`Driver.fatigue_lockout_until`). Also fed into the driver rating
    decay so a chronic canceller's rating drifts down.
    """
    REASON_CHOICES = [
        ('no_show', 'Rider no-show'),
        ('vehicle_issue', 'Vehicle issue'),
        ('safety', 'Safety concern'),
        ('personal', 'Personal'),
        ('other', 'Other'),
    ]
    driver = models.ForeignKey(
        Driver,
        on_delete=models.CASCADE,
        related_name='cancellations',
    )
    trip = models.ForeignKey(
        'ride.Trip',
        on_delete=models.CASCADE,
        related_name='driver_cancellations',
    )
    reason = models.CharField(max_length=32, choices=REASON_CHOICES)
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['driver', '-created_at'], name='cancel_driver_recent_idx'),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'Cancel driver={self.driver_id} trip={self.trip_id} reason={self.reason}'


class WithdrawalRequest(CurrencyStampedModel, models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('processed', 'Processed'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        # The provider request was issued and we never learned the outcome -- a
        # timeout or a 5xx. Distinct from 'failed', which asserts no money moved.
        #
        # Money stays held: the wallet is NOT refunded, because Cashfree may have
        # paid the driver and a refund on top of that is a double credit. The row
        # waits for a human to check the provider and move it to 'processed' or
        # 'failed'. There is no automated resolution because no provider status
        # call has been verified against the sandbox.
        ('unresolved', 'Unresolved - provider outcome unknown'),
    ]
    PAYOUT_METHOD_CHOICES = [
        ('bank', 'Bank Transfer'),
        ('upi', 'UPI Payout'),
    ]
    # Where a payout's currency comes from: the driver's wallet, which is their
    # home market. A payout has no trip, so there is nothing more specific to
    # ask -- and paying a driver in the currency of whichever zone their last
    # ride happened to be in would be wrong, not merely imprecise.
    currency_source_fields = ()

    def _derive_currency(self):
        from base.money import normalize_currency
        from servers.rider.models import Wallet

        try:
            wallet = Wallet.objects.filter(
                user_id=self.driver.user_id_id).first()
        except Exception:
            wallet = None
        if wallet and wallet.currency:
            return normalize_currency(wallet.currency)
        return None

    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='withdrawal_requests')
    # Minor units of `currency` (base/money.py); `amount` is the Decimal view.
    amount_minor = models.BigIntegerField()
    amount = MinorUnitsAmount('amount_minor')
    # The currency this amount is in, stamped at write time from the trip's
    # zone. Denormalised on purpose: the currency a row was written in is a
    # FACT about that row, not a lookup against today's zone configuration --
    # same reasoning as the already-stored `zone_code` and `rate_card_version`.
    # Without it, every cross-market Sum() in this codebase is a meaningless
    # number and nothing says so.
    currency = models.CharField(max_length=3, db_index=True)
    payout_method = models.CharField(max_length=20, choices=PAYOUT_METHOD_CHOICES, default='upi')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    admin_notes = models.TextField(blank=True, null=True)
    payout_reference_id = models.CharField(max_length=256, blank=True, null=True, help_text='Reference ID for tracking the payout')
    payout_status = models.CharField(max_length=50, blank=True, null=True, help_text='Current status from payment gateway (created, processing, processed, failed)')
    payout_mode = models.CharField(max_length=20, blank=True, null=True, help_text='Payout mode (UPI, bank_transfer, etc.)')
    failure_count = models.IntegerField(default=0, help_text='Number of times payout attempt failed')
    last_failure_at = models.DateTimeField(null=True, blank=True, help_text='Timestamp of last failure')
    failure_reason = models.CharField(max_length=255, blank=True, null=True, help_text='Reason for last failure')
    # The transfer fee actually charged on this request, in minor units, stamped
    # at creation from `payout_config.withdrawal_fee` (the one source of the fee
    # a driver is shown). The gateway is sent `amount_minor - fee_minor`.
    fee_minor = models.BigIntegerField(default=0)
    # `driver` (the driver asked) or `scheduled` (the weekly run created it).
    origin = models.CharField(max_length=12, default='driver')
    # Finance has flagged this payout for a closer look. Moves no money.
    investigation_note = models.TextField(blank=True, default='')
    investigation_since = models.DateTimeField(null=True, blank=True)
    investigation_by_label = models.CharField(max_length=255, blank=True, default='')

    def __str__(self):
        return f'Withdrawal {self.id} - Driver {self.driver} - {self.amount}'

    class Meta:
        ordering = ['-requested_at']
        indexes = [
            # The payout queue's date filter and the stuck-payout check
            # (status = processed, processed_at older than N hours).
            models.Index(fields=['-requested_at'], name='withdrawal_requested_idx'),
            models.Index(fields=['status', 'processed_at'], name='withdrawal_status_proc_idx'),
        ]


class DriverUPIContact(models.Model):
    """
    Stores payment gateway contact and fund account information for drivers
    to enable UPI payouts without recreating contacts each time.
    """
    driver = models.OneToOneField(Driver, on_delete=models.CASCADE, related_name='upi_contact')
    gateway_contact_id = models.CharField(max_length=256, blank=True, null=True)
    gateway_fund_account_id = models.CharField(max_length=256, blank=True, null=True)
    # Which provider issued the two ids above. A contact / fund account id belongs to ONE
    # provider: it is reused only when this equals the provider paying now, so a Cashfree id is
    # never sent to Razorpay (or the reverse). NULL = recorded before this field existed.
    gateway_provider = models.CharField(max_length=32, blank=True, null=True)
    upi_id = models.CharField(max_length=256, blank=True, null=True, help_text='UPI ID stored at time of contact creation')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return f'UPI Contact for {self.driver}'
    
    class Meta:
        verbose_name = 'Driver UPI Contact'
        verbose_name_plural = 'Driver UPI Contacts'


class DriverBankAccount(models.Model):
    """
    Stores bank account details for a driver for payouts.
    """
    driver = models.OneToOneField(Driver, on_delete=models.CASCADE, related_name='bank_account')
    bank_name = models.CharField(max_length=256)
    account_number = models.CharField(max_length=256)
    ifsc_code = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Bank Account for {self.driver}"


class IncentiveQuest(models.Model):
    """
    Defines a quest/incentive for drivers to complete.
    e.g. Complete 10 trips this week to get ₹500.
    """
    title = models.CharField(max_length=256)
    description = models.TextField(blank=True, default='')
    target_trips = models.PositiveIntegerField(help_text='Number of trips required to complete the quest')
    # Minor units of `currency`. Quests are not zone-scoped yet, so the
    # currency is set explicitly rather than stamped.
    reward_amount_minor = models.BigIntegerField()
    reward_amount = MinorUnitsAmount('reward_amount_minor')
    currency = models.CharField(max_length=3, default='INR', db_index=True)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} ({self.target_trips} trips for {self.reward_amount})"


# ---------------------------------------------------------------------------
# Drivers & KYC module (operator console, module 3)
# ---------------------------------------------------------------------------

class DriverDocumentReview(models.Model):
    """One operator decision on one document. The latest row per document is
    its review state; older rows are its history.

    Needed because `Driver.doc_status` / `doc_rejection_reason` hold ONE
    state for the whole driver, and the console reviews each document on its
    own. Those two columns stay what the driver app reads: a rejection here
    also writes them, so the driver sees the reason in the app they have.

    `file_name` is the stored file the decision was about. A re-upload has a
    different name, so the document is back to "not reviewed" without anyone
    having to reset it.
    """

    APPROVED = 'approved'
    REJECTED = 'rejected'
    STATE_CHOICES = [(APPROVED, 'Approved'), (REJECTED, 'Rejected')]

    driver = models.ForeignKey(Driver, on_delete=models.CASCADE,
                               related_name='document_reviews')
    vehicle = models.ForeignKey('Vehicle', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='document_reviews')
    document_key = models.CharField(max_length=32)
    file_name = models.CharField(max_length=512, blank=True, default='')
    state = models.CharField(max_length=10, choices=STATE_CHOICES)
    #: Shown to the driver on a rejection.
    reason = models.TextField(blank=True, default='')
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    reviewed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=['driver', 'document_key', '-reviewed_at'],
                                name='doc_review_driver_key_idx')]

    def __str__(self):
        return f'DriverDocumentReview driver={self.driver_id} {self.document_key} {self.state}'


class DriverSuspension(models.Model):
    """An operator suspension. Active while `lifted_at` is NULL.

    Suspending writes the same two columns the licence-expiry sweeper writes
    (`status='blocked'`, `approved=False`), which is what the dispatch gates
    already check (`qa_eligibility.may_operate` at socket connect and on
    accept, `status == 'blocked'` on offers): no new dispatch rule.
    `previous_approved` is what reinstating restores.
    """

    REASON_CHOICES = [
        ('documents', 'Document problem'),
        ('safety', 'Safety concern'),
        ('fraud', 'Suspected fraud'),
        ('conduct', 'Conduct / complaints'),
        ('rider_complaints', 'Repeated rider complaints'),
        ('other', 'Other (note required)'),
        ('deactivated', 'Permanently deactivated'),
    ]

    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='suspensions')
    reason_code = models.CharField(max_length=32, choices=REASON_CHOICES)
    note = models.TextField(blank=True, default='')
    previous_approved = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    lifted_at = models.DateTimeField(null=True, blank=True)
    lifted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='+')
    lift_note = models.TextField(blank=True, default='')

    class Meta:
        constraints = [
            # Suspending twice is the same suspension.
            models.UniqueConstraint(fields=['driver'], condition=models.Q(lifted_at__isnull=True),
                                    name='driver_one_active_suspension'),
        ]

    def __str__(self):
        return f'DriverSuspension driver={self.driver_id} {self.reason_code}'


class DriverNote(models.Model):
    """An internal operator note. Never shown to the driver."""

    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='operator_notes')
    body = models.TextField()
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='+')
    idempotency_key = models.CharField(max_length=200, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f'DriverNote driver={self.driver_id}'
