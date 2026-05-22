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
    def __str__(self):
        return f'Trip {self.id} - {self.user_id}'
    
    class Meta:
        indexes = [
            models.Index(fields=['user_id', '-requested_at']),
            models.Index(fields=['driver_id', '-requested_at']),
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
    trip_id=models.ForeignKey(Trip,on_delete=models.CASCADE,related_name='receipts')
    user_id=models.ForeignKey(User,on_delete=models.CASCADE,related_name='receipts')
    receipt_number=models.CharField(max_length=64,unique=True,db_index=True)
    total_fare=models.DecimalField(max_digits=10,decimal_places=2)
    gst_amount=models.DecimalField(max_digits=10,decimal_places=2,default=0)
    payment_method=models.CharField(max_length=32,blank=True,default='')
    payment_status=models.CharField(max_length=32,blank=True,default='')
    sent_to_email=models.EmailField(blank=True,default='')
    html_body=models.TextField()
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
