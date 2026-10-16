from decimal import Decimal

from django.db import models
from django.contrib.auth import get_user_model
from base.currency_stamp import CurrencyStampedModel

User=get_user_model()
class Rider(models.Model):
    """Rider profile.

    `rating` is the EWMA-style score driven by ratings drivers leave
    after each trip. New riders start at 5.0; below 3.0 they're
    flagged for ops review (drivers can decline) and below 2.5 they're
    soft-blocked from booking until support clears them.

    `rating_total` / `rating_count` track the underlying observation
    counts so we can reconstruct or audit the EWMA path later if a
    rider disputes a low score.
    """
    user_id=models.OneToOneField(User,on_delete=models.CASCADE,related_name='rider')
    created_at=models.DateTimeField(auto_now_add=True)
    rating=models.DecimalField(decimal_places=2,max_digits=3,default=5.00)
    rating_count=models.PositiveIntegerField(default=0)
    # Set when rating drops below RIDER_REVIEW_RATING_THRESHOLD (default
    # 3.0). Until ops clears it, drivers see a "low-rated rider" badge
    # at the ride-request stage and may decline without penalty.
    flagged_for_review=models.BooleanField(default=False,db_index=True)
    review_flagged_at=models.DateTimeField(null=True,blank=True)
    review_cleared_at=models.DateTimeField(null=True,blank=True)
    review_notes=models.TextField(blank=True,default='')

    def __str__(self):
        return self.user_id.full_name if self.user_id.full_name else self.user_id.phone_number
class FavoritePlace(models.Model):
    user_id=models.ForeignKey(User,on_delete=models.CASCADE,related_name='favorite_places')
    address_text=models.CharField(max_length=512,blank=True,null=True)
    latitude=models.DecimalField(max_digits=20,decimal_places=10)
    longitude=models.DecimalField(max_digits=20,decimal_places=10)
    def __str__(self):
        return f'{self.user_id} - {self.address_text}'
class Wallet(models.Model):
    """A balance held for one user in one role.

    A user who drives *and* rides holds TWO rows: their rider credit
    balance and their driver settlement balance. They were previously the
    same row, which meant a driver could spend money the platform owed
    them as settlement by paying for their own rides through the rider
    wallet endpoints — and it made the closed-loop credit posture
    (ADR-0003) unenforceable, because settlement money is not a credit.
    """
    SCOPE_RIDER = 'rider'
    SCOPE_DRIVER = 'driver'
    SCOPE_CHOICES = [
        (SCOPE_RIDER, 'Rider credits (closed-loop)'),
        (SCOPE_DRIVER, 'Driver settlement balance'),
    ]

    user_id=models.ForeignKey(User,on_delete=models.CASCADE,related_name='wallets')
    scope=models.CharField(
        max_length=16, choices=SCOPE_CHOICES, default=SCOPE_RIDER, db_index=True,
    )
    # default uses Decimal explicitly so a freshly-constructed Wallet
    # instance holds a Decimal in memory (not a Python float). The
    # mismatch caused `wallet.balance + Decimal(amount)` to TypeError on
    # the first ever top-up. See QA-7 in the Phase-0 QA report.
    balance=models.DecimalField(max_digits=12,decimal_places=2,default=Decimal('0.00'))
    # The currency this amount is in, stamped at write time from the trip's
    # zone. Denormalised on purpose: the currency a row was written in is a
    # FACT about that row, not a lookup against today's zone configuration --
    # same reasoning as the already-stored `zone_code` and `rate_card_version`.
    # Without it, every cross-market Sum() in this codebase is a meaningless
    # number and nothing says so.
    currency = models.CharField(max_length=3, default='INR', db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user_id', 'scope'], name='wallet_unique_user_scope',
            ),
        ]

    def __str__(self):
        return f'{self.user_id} [{self.scope}] - {self.balance}'


def get_wallet(user, scope=Wallet.SCOPE_RIDER, lock=False):
    """Fetch (or create) the wallet for a user in a given role.

    Always go through this rather than `Wallet.objects.get(user_id=...)`,
    which is now ambiguous. `lock=True` takes SELECT FOR UPDATE — required
    for every balance mutation.
    """
    qs = Wallet.objects.select_for_update() if lock else Wallet.objects
    wallet, _ = qs.get_or_create(user_id=user, scope=scope)
    if wallet.balance is None:
        wallet.balance = Decimal('0.00')
    return wallet

class WalletTransaction(CurrencyStampedModel, models.Model):
    """A ledger row. Carries its own currency.

    This model has no trip FK -- the only link back to a trip is
    `reference_id = 'TRIP_<id>'`, which `servers/driver/earnings.py` already
    documents as a real modelling weakness. So the currency cannot be derived by
    the generic mixin and `_derive_currency` is overridden below to parse it.

    Parsing a string to find the currency of a money row is not good. It is,
    however, strictly better than defaulting every settlement row to INR, and it
    does not require changing the settlement write path in the same change that
    introduces currency. When `reference_id` becomes a proper FK, delete the
    override and add 'trip' to `currency_source_fields`.
    """

    currency_source_fields = ()

    def _derive_currency(self):
        from base.money import normalize_currency

        ref = (self.reference_id or '') if hasattr(self, 'reference_id') else ''
        if ref.startswith('TRIP_'):
            try:
                trip_pk = int(ref[len('TRIP_'):])
            except (TypeError, ValueError):
                trip_pk = None
            if trip_pk is not None:
                from servers.ride.models import Trip
                found = (Trip.objects.filter(pk=trip_pk)
                         .values_list('currency', flat=True).first())
                if found:
                    return normalize_currency(found)

        # No trip to ask (a top-up, a withdrawal, a promo credit). Use the
        # wallet's currency, which is the user's home market.
        try:
            wallet = Wallet.objects.filter(user_id=self.user_id_id).first()
        except Exception:
            wallet = None
        if wallet and wallet.currency:
            return normalize_currency(wallet.currency)
        return None

    user_id = models.ForeignKey(User, on_delete=models.CASCADE, related_name='wallet_transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # The currency this amount is in, stamped at write time from the trip's
    # zone. Denormalised on purpose: the currency a row was written in is a
    # FACT about that row, not a lookup against today's zone configuration --
    # same reasoning as the already-stored `zone_code` and `rate_card_version`.
    # Without it, every cross-market Sum() in this codebase is a meaningless
    # number and nothing says so.
    currency = models.CharField(max_length=3, default='INR', db_index=True)
    txn_type = models.CharField(max_length=20, choices=[
        ('credit', 'Credit'),  # Added via payment gateway
        ('debit', 'Debit')     # Used for both gateway and direct payments
    ])
    status = models.CharField(max_length=20, choices=[('pending', 'Pending'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending')
    # Generic gateway fields
    gateway_order_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    gateway_payment_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    gateway_signature = models.CharField(max_length=512, blank=True, null=True)
    payment_gateway = models.CharField(max_length=50, default='cashfree', choices=[
        ('cashfree', 'Cashfree'),
    ])
    created_at = models.DateTimeField(auto_now_add=True)
    # New fields for direct payments
    purpose = models.CharField(max_length=100, blank=True, null=True)
    reference_id = models.CharField(max_length=100, blank=True, null=True)
    idempotency_key = models.CharField(max_length=100, unique=True, blank=True, null=True)

    def __str__(self):
        return f'{self.user_id} - {self.amount} ({self.txn_type})'

class Notification(models.Model):
    """An in-app notification.

    `notif_type` lets the mobile app branch on category (ride event,
    payment outcome, payout, KYC update, etc.) instead of string-matching
    the title. `trip` is a nullable FK so notifications tied to a ride
    can deep-link to it without parsing the message body. `data` carries
    arbitrary payload mirroring the FCM data block.

    Composite index on (user_id, is_read, -created_at) makes the
    common "unread notifications for this user, newest first" query
    cheap even after millions of rows.
    """
    NOTIF_TYPES = [
        ('ride_event', 'Ride event'),
        ('payment', 'Payment'),
        ('payout', 'Payout'),
        ('wallet', 'Wallet'),
        ('kyc', 'KYC'),
        ('sos', 'SOS'),
        ('system', 'System'),
        ('marketing', 'Marketing'),
    ]
    user_id = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=256)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    # Optional categorisation + deep-link target. Both nullable so legacy
    # rows still load without backfill.
    notif_type = models.CharField(max_length=32, choices=NOTIF_TYPES, blank=True, default='', db_index=True)
    trip = models.ForeignKey('ride.Trip', on_delete=models.SET_NULL, null=True, blank=True, related_name='notifications')
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Pinned name so the migration (handwritten below) and the
            # model declaration agree — avoids `makemigrations --check`
            # phantom rename migrations.
            models.Index(fields=['user_id', 'is_read', '-created_at'], name='notif_user_unread_idx'),
        ]

    def __str__(self):
        return f'{self.user_id} - {self.title}'


class NotificationPreference(models.Model):
    """Per-user notification opt-in/opt-out preferences.

    Default posture for DPDP Act 2023 explicit-consent compliance:
      transactional / ride_event / payment / payout / sos = ON
        (cannot opt out -- these are functional / safety-critical)
      kyc / system        = ON  (account-state changes; opt-outable
                                 but on by default)
      marketing / promo   = OFF (require explicit opt-in)

    The dispatcher in base.utils.send_notification (and any Celery
    task that fires marketing pushes) MUST consult this before
    creating a Notification or sending an FCM. Code paths that touch
    `transactional` channels do not need to check -- those are
    delivered regardless because the user is in the middle of a flow
    that requires them.
    """
    user_id = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='notification_preferences',
    )
    transactional = models.BooleanField(default=True)
    ride_event = models.BooleanField(default=True)
    payment = models.BooleanField(default=True)
    payout = models.BooleanField(default=True)
    sos = models.BooleanField(default=True)
    kyc = models.BooleanField(default=True)
    system = models.BooleanField(default=True)
    marketing = models.BooleanField(default=False)
    promo = models.BooleanField(default=False)
    # Channels (per category). All ON for transactional types so the
    # user always gets a push for a critical event. Marketing channels
    # respect the per-category flag above too.
    push_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Categories that the user is allowed to disable via the API.
    # Anything not listed here is forced True (e.g. SOS, payouts).
    USER_TOGGLEABLE = ('ride_event', 'payment', 'kyc', 'system', 'marketing', 'promo')

    def is_enabled_for(self, category: str) -> bool:
        if category in ('transactional', 'sos', 'payout'):
            return True  # always
        return bool(getattr(self, category, True))

    def __str__(self):
        return f'NotifPrefs for {self.user_id_id}'