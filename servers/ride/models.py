from django.db import models
from django.contrib.auth import get_user_model
from servers.driver.models import Vehicle,Driver,VehicleType

User=get_user_model()
# Create your models here.
class TripStatus(models.Model):
    status_code=models.CharField(max_length=20,choices=[
        ('accepted','Accepted'),('reached','Reached'),('in_progress','In Progress'),
        ('completed','Completed'),('cancelled','Cancelled'),('requested','Requested')
    ],unique=True)
    description=models.TextField(blank=True,null=True)
    def __str__(self):
        return self.status_code

def get_default_trip_status():
    status, _ = TripStatus.objects.get_or_create(status_code='requested')
    return status.id

class Trip(models.Model):
    user_id=models.ForeignKey(User,on_delete=models.CASCADE,related_name='trips')
    driver_id=models.ForeignKey(Driver,on_delete=models.DO_NOTHING,related_name='trips',blank=True,null=True)
    vehicle_id=models.ForeignKey(Vehicle,on_delete=models.DO_NOTHING,related_name='trips',blank=True,null=True)
    requested_vehicle_type=models.ForeignKey(VehicleType,on_delete=models.SET_NULL,null=True,blank=True,related_name='requested_trips')
    status_id=models.ForeignKey(TripStatus,on_delete=models.DO_NOTHING,related_name='trips',default=get_default_trip_status)
    requested_at=models.DateTimeField(auto_now_add=True)
    accepted_at=models.DateTimeField(blank=True,null=True)
    reached_at=models.DateTimeField(blank=True,null=True)
    started_at=models.DateTimeField(blank=True,null=True)
    completed_at=models.DateTimeField(blank=True,null=True, db_index=True)
    cancelled_at=models.DateTimeField(blank=True,null=True)
    pickup_address=models.CharField(max_length=512,blank=True,null=True)
    pickup_lat=models.DecimalField(max_digits=10,decimal_places=7)
    pickup_long=models.DecimalField(max_digits=10,decimal_places=7)
    destination_address=models.CharField(max_length=512,blank=True,null=True)
    destination_lat=models.DecimalField(max_digits=10,decimal_places=7)
    destination_long=models.DecimalField(max_digits=10,decimal_places=7)
    estimated_distance_km=models.DecimalField(max_digits=10,decimal_places=2,blank=True,null=True)
    actual_distance_km=models.DecimalField(max_digits=10,decimal_places=2,blank=True,null=True)
    estimated_fare=models.DecimalField(max_digits=10,decimal_places=2,blank=True,null=True)
    final_fare=models.DecimalField(max_digits=10,decimal_places=2,blank=True,null=True)
    surge_multiplier=models.DecimalField(max_digits=4,decimal_places=2,default=1.00)
    payment_method=models.CharField(max_length=50,blank=True,null=True)
    payment_status=models.CharField(max_length=50,blank=True,null=True)
    otp=models.CharField(max_length=6,blank=True,null=True)

    # Who ended the trip, and why. `status_id` stays a single 'cancelled'
    # code so the four clients keep working, but ops, refunds, the driver
    # penalty ledger and the MVA-2020 cancellation policy all need to tell
    # a rider-cancel from a driver-cancel from a dispatch timeout.
    CANCELLED_BY_CHOICES = [
        ('rider', 'Rider'),
        ('driver', 'Driver'),
        ('system', 'System / auto'),
        ('admin', 'Admin'),
    ]
    cancelled_by=models.CharField(
        max_length=16, choices=CANCELLED_BY_CHOICES, blank=True, default='',
        db_index=True,
    )
    cancellation_reason=models.CharField(max_length=255, blank=True, default='')
    cancellation_fee=models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text='Charged to the cancelling party, per the published cancellation policy.',
    )

    # Zone the pickup fell in, resolved at booking time. Stamped on the row
    # so fare/commission/GST attribution and per-city reporting do not have
    # to re-run a point-in-polygon lookup against today's zone table.
    zone=models.ForeignKey(
        'pricing.ServiceZone', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='trips',
    )

    def __str__(self):
        return f'Trip {self.id} - {self.user_id}'

    class Meta:
        indexes = [
            models.Index(fields=['user_id', '-requested_at']),
            models.Index(fields=['driver_id', '-requested_at']),
            models.Index(fields=['status_id', '-requested_at'], name='trip_status_recent_idx'),
            models.Index(fields=['zone', '-requested_at'], name='trip_zone_recent_idx'),
        ]
class FarePricing(models.Model):
    trip_id=models.ForeignKey(Trip,on_delete=models.CASCADE,related_name='fare_pricing')
    base_fare=models.DecimalField(max_digits=10,decimal_places=2)
    distance_fare=models.DecimalField(max_digits=10,decimal_places=2)
    time_fare=models.DecimalField(max_digits=10,decimal_places=2)
    surge_multiplier=models.DecimalField(max_digits=4,decimal_places=2,default=1.00)
    total_fare=models.DecimalField(max_digits=10,decimal_places=2)
    def __str__(self):
        return f'Fare for Trip {self.trip_id.id}'
class VehicleFarePricing(models.Model):
    vehicle_type_id=models.ForeignKey(VehicleType,on_delete=models.CASCADE,related_name='fare_pricing')
    base_fare=models.DecimalField(max_digits=10,decimal_places=2)
    per_km_fare=models.DecimalField(max_digits=10,decimal_places=2)
    per_min_fare=models.DecimalField(max_digits=10,decimal_places=2)
    min_fare=models.DecimalField(max_digits=10,decimal_places=2)
    night_surge_multiplier=models.DecimalField(max_digits=4,decimal_places=2,default=1.00)
    def __str__(self):
        return f'Pricing for {self.vehicle_type_id}'
class Rating(models.Model):
    trip_id=models.ForeignKey(Trip,on_delete=models.CASCADE,related_name='ratings')
    rater_id=models.ForeignKey(User,on_delete=models.CASCADE,related_name='ratings_given')
    score=models.IntegerField()
    comments=models.TextField(blank=True,null=True)
    created_at=models.DateTimeField(auto_now_add=True)
    def __str__(self):
        return f'Rating {self.score} for Trip {self.trip_id.id}'


class Receipt(models.Model):
    """Per-completed-trip receipt sent to the rider.

    Generated at trip-completion time. Stores the rendered HTML so
    Support can re-send the exact body the rider received, even if
    the trip / fare / driver records change later. GST captured at
    issue time as a snapshot (rate may change in future rate cards
    but the issued receipt stays correct).

    Multiple rows per trip = re-issues (e.g. dispute resolved,
    fare adjusted, new receipt version). Resending the latest
    receipt updates last_sent_at only.
    """
    from base.storage_backends import private_document_storage
    from base.media import PrefixedUUIDPath

    trip_id=models.ForeignKey(Trip,on_delete=models.CASCADE,related_name='receipts')
    user_id=models.ForeignKey(User,on_delete=models.CASCADE,related_name='receipts')
    receipt_number=models.CharField(max_length=64,unique=True,db_index=True)
    total_fare=models.DecimalField(max_digits=10,decimal_places=2)
    gst_amount=models.DecimalField(max_digits=10,decimal_places=2,default=0)
    payment_method=models.CharField(max_length=32,blank=True,default='')
    payment_status=models.CharField(max_length=32,blank=True,default='')
    sent_to_email=models.EmailField(blank=True,default='')
    html_body=models.TextField()
    # PDF attachment generated alongside the HTML on receipt issue.
    # Lives in private S3 (signed URLs); nullable so legacy rows
    # written before PDF generation still load.
    pdf_file=models.FileField(
        blank=True,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('receipts'),
        max_length=512,
    )
    version=models.IntegerField(default=1)
    issued_at=models.DateTimeField(auto_now_add=True,db_index=True)
    last_sent_at=models.DateTimeField(null=True,blank=True)
    send_failure_reason=models.TextField(blank=True,default='')

    class Meta:
        ordering = ['-issued_at']
        indexes = [
            models.Index(fields=['trip_id', '-version'], name='receipt_trip_ver_idx'),
        ]

    def __str__(self):
        return f'Receipt {self.receipt_number} trip={self.trip_id_id}'


class ChatMessage(models.Model):
    """One message in the in-trip chat between rider and driver.

    Chat is per-Trip; the channel closes when the trip ends
    (completed / cancelled). Messages persist after close so support
    can review for disputes (lost-item, safety, fare complaints).

    `sender_role` is denormalised onto the row so we don't have to
    re-resolve rider-vs-driver on every read; `is_system` flags
    server-emitted lines (e.g. "Driver arrived at pickup").
    """
    SENDER_CHOICES = [
        ('rider', 'Rider'),
        ('driver', 'Driver'),
        ('system', 'System'),
    ]
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name='chat_messages')
    sender = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='chat_messages_sent',
    )
    sender_role = models.CharField(max_length=8, choices=SENDER_CHOICES)
    body = models.TextField()
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['trip', 'created_at'], name='chat_trip_time_idx'),
        ]

    def __str__(self):
        return f'ChatMessage trip={self.trip_id} role={self.sender_role}'


class PromoCode(models.Model):
    """Rider-facing promo code.

    Designed for two common cases:
      1. Percentage-off (e.g. WELCOME50 = 50% off up to Rs 100)
      2. Flat discount (e.g. AIRPORT100 = Rs 100 off)

    Caps + per-rider redemption count + global redemption count keep
    a popular code from breaking the unit economics. Validity window
    is closed-open: [valid_from, valid_to). Codes can be scoped to a
    zone (None = all zones) so we can run Hyderabad-only campaigns
    once we have multiple cities live.
    """
    DISCOUNT_TYPE_CHOICES = [
        ('percent', 'Percentage off'),
        ('flat', 'Flat amount off'),
    ]
    code = models.CharField(max_length=32, unique=True, db_index=True)
    description = models.CharField(max_length=256, blank=True, default='')

    discount_type = models.CharField(max_length=8, choices=DISCOUNT_TYPE_CHOICES)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    # Used only for percent codes -- cap the discount in absolute Rs.
    max_discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )
    # Minimum fare required before the code is allowed (Rs).
    min_fare = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
    )

    # Optional zone scope. NULL = applies in all active zones.
    zone = models.ForeignKey(
        'pricing.ServiceZone', on_delete=models.PROTECT,
        null=True, blank=True, related_name='promo_codes',
    )

    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField()

    max_total_redemptions = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='None = unlimited globally. The redemption counter is checked atomically before crediting.',
    )
    max_per_user_redemptions = models.PositiveIntegerField(default=1)
    redemption_count = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-valid_from']
        indexes = [
            models.Index(fields=['is_active', 'valid_from', 'valid_to'], name='promo_active_window_idx'),
        ]

    def __str__(self):
        return self.code


class PromoRedemption(models.Model):
    """One row per successful (code, user, trip) apply.

    Trip is nullable for the "validate at booking time, attach to trip
    once trip is created" flow -- we reserve the redemption when the
    rider applies the code in the fare-estimate screen and then
    attach the trip id once the booking lands. A redemption with a
    null trip_id older than 1 hour can be swept by a cleanup task.
    """
    promo = models.ForeignKey(PromoCode, on_delete=models.PROTECT, related_name='redemptions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='promo_redemptions')
    trip = models.ForeignKey(
        Trip, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='promo_redemptions',
    )
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Free-form note (e.g. the fare-quote id the user saw when applying)
    notes = models.CharField(max_length=256, blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['promo', 'user'], name='redeem_promo_user_idx'),
            models.Index(fields=['user', '-created_at'], name='redeem_user_recent_idx'),
        ]

    def __str__(self):
        return f'Redeem promo={self.promo_id} user={self.user_id} amount={self.discount_amount}'
