from django.contrib import admin
from .models import TripStatus,Trip,FarePricing,VehicleFarePricing,Rating
# Register your models here.
admin.site.register(TripStatus)
admin.site.register(Trip)
admin.site.register(FarePricing)
admin.site.register(VehicleFarePricing)
admin.site.register(Rating)
