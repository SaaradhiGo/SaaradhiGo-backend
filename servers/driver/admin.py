from django.contrib import admin
from .models import Driver, Vehicle, VehicleType, WithdrawalRequest, DriverUPIContact
# Register your models here.
admin.site.register(Driver)
admin.site.register(Vehicle)
admin.site.register(VehicleType)
admin.site.register(WithdrawalRequest)
admin.site.register(DriverUPIContact)
