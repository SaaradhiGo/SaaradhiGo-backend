from django.contrib import admin
from .models import Driver, Vehicle, VehicleType, WithdrawalRequest, DriverUPIContact, DriverBankAccount
# Register your models here.
admin.site.register(Driver)
admin.site.register(Vehicle)
@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    search_fields = ('type',)
    list_display = ('type',)
admin.site.register(WithdrawalRequest)
admin.site.register(DriverUPIContact)

@admin.register(DriverBankAccount)
class DriverBankAccountAdmin(admin.ModelAdmin):
    list_display = ('driver', 'bank_name', 'account_number', 'ifsc_code', 'updated_at')
    search_fields = ('account_number', 'ifsc_code', 'driver__user_id__phone_number')
