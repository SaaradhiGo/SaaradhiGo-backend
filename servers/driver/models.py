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
