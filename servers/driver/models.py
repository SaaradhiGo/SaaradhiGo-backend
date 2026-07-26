from django.db import models
from django.contrib.auth import get_user_model
from base.media import PrefixedUUIDPath, validate_document_file, validate_file_size, validate_image_file
from base.storage_backends import private_document_storage, public_media_storage
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
    license_expiry=models.DateField(blank=True,null=True)
    status=models.CharField(max_length=20,choices=[
        ('online','Online'),('off','Off'),('active','Active'),
        ('on ride','On Ride'),('off ride','Off Ride'),('blocked','Blocked')
    ],default='off')
    total_trips=models.IntegerField(default=0)
    ratings=models.DecimalField(max_digits=3,decimal_places=2,default=0.00)
    approved=models.BooleanField(default=False)
    active_vehicle=models.ForeignKey('Vehicle',on_delete=models.SET_NULL,null=True,blank=True)
    last_withdrawal_at=models.DateTimeField(null=True,blank=True)
    upi_id=models.CharField(max_length=256,blank=True,null=True,help_text='UPI ID for UPI payouts')
    # If set in the future, the driver is locked out of going online
    # (and out of accepting new trips) until this timestamp. Used by:
    #   - MVA 2020 12h/24h fatigue cap (servers.driver.services)
    #   - Rolling cancellation cooldown (3 cancels in 24h -> 1h lockout)
    # Nullable so legacy drivers continue to work; default unlocked.
    fatigue_lockout_until=models.DateTimeField(null=True,blank=True,db_index=True)
    def __str__(self) -> str:
        return self.user_id.full_name if self.user_id.full_name else self.user_id.phone_number
class VehicleType(models.Model):
    type=models.CharField(max_length=50,unique=True)
    description=models.TextField(blank=True,null=True)
    def __str__(self):
        return self.type
class Vehicle(models.Model):
    driver_id=models.ForeignKey(Driver,on_delete=models.CASCADE)
    rc_doc=models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=private_document_storage,
        upload_to=PrefixedUUIDPath('rc_docs'),
        validators=[validate_document_file, validate_file_size],
    )
    vehicle_type_id=models.ForeignKey(VehicleType,on_delete=models.CASCADE,related_name='vehicles')
    brand=models.CharField(max_length=100,blank=True,null=True)
    model=models.CharField(max_length=100,blank=True,null=True)
    color=models.CharField(max_length=50,blank=True,null=True)
    year=models.IntegerField(blank=True,null=True)
    vehicle_number=models.CharField(max_length=20)
    capacity=models.IntegerField(default=1)
    vehicle_pic=models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=public_media_storage,
        upload_to=PrefixedUUIDPath('vehicle_pics'),
        validators=[validate_image_file, validate_file_size],
    )
    status=models.CharField(max_length=20,choices=[
        ('active','Active'),('inactive','Inactive'),
        ('under_maintenance','Under Maintenance')
    ],default='active')
    # MVA 2020 mandates the platform verify and enforce these. Daily
    # Celery sweeper (servers.driver.tasks.block_expired_driver_credentials)
    # blocks any driver whose active vehicle has any expired credential.
    # Nullable so legacy rows continue to work; new vehicles should be
    # required to supply them via the admin UI.
    insurance_expiry = models.DateField(blank=True, null=True)
    permit_expiry = models.DateField(blank=True, null=True)
    fitness_expiry = models.DateField(blank=True, null=True)
    puc_expiry = models.DateField(blank=True, null=True)

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


class WithdrawalRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('processed', 'Processed'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    PAYOUT_METHOD_CHOICES = [
        ('bank', 'Bank Transfer'),
        ('upi', 'UPI Payout'),
    ]
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='withdrawal_requests')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
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

    def __str__(self):
        return f'Withdrawal {self.id} - Driver {self.driver} - {self.amount}'

    class Meta:
        ordering = ['-requested_at']


class DriverUPIContact(models.Model):
    """
    Stores payment gateway contact and fund account information for drivers
    to enable UPI payouts without recreating contacts each time.
    """
    driver = models.OneToOneField(Driver, on_delete=models.CASCADE, related_name='upi_contact')
    gateway_contact_id = models.CharField(max_length=256, blank=True, null=True)
    gateway_fund_account_id = models.CharField(max_length=256, blank=True, null=True)
    upi_id = models.CharField(max_length=256, blank=True, null=True, help_text='UPI ID stored at time of contact creation')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return f'UPI Contact for {self.driver}'
    
    class Meta:
        verbose_name = 'Driver UPI Contact'
        verbose_name_plural = 'Driver UPI Contacts'
