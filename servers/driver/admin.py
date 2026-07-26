from django.contrib import admin
from .models import Driver, Vehicle, VehicleType, WithdrawalRequest, DriverUPIContact
# Register your models here.
admin.site.register(Driver)
admin.site.register(Vehicle)
@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    search_fields = ('type',)
    list_display = ('type',)
admin.site.register(WithdrawalRequest)
admin.site.register(DriverUPIContact)
