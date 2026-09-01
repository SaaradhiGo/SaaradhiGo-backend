from django.contrib import admin

from servers.pricing.models import PlatformSettings, RateCard, ServiceZone


@admin.register(ServiceZone)
class ServiceZoneAdmin(admin.ModelAdmin):
    list_display = (
        'code', 'name', 'zone_type', 'city', 'state_code',
        'country', 'priority', 'is_active', 'updated_at',
    )
    list_filter = ('is_active', 'zone_type', 'country', 'state_code')
    search_fields = ('code', 'name', 'city', 'state_code')
    ordering = ('-priority', 'code')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(RateCard)
class RateCardAdmin(admin.ModelAdmin):
    list_display = (
        'zone', 'vehicle_type', 'version', 'base_fare', 'per_km_fare',
        'per_min_fare', 'min_fare', 'surge_cap_multiplier',
        'effective_from', 'effective_to', 'is_active',
    )
    list_filter = ('is_active', 'zone', 'vehicle_type')
    search_fields = ('zone__code', 'vehicle_type__type', 'notes')
    autocomplete_fields = ('zone', 'vehicle_type')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Scope', {'fields': ('zone', 'vehicle_type', 'version', 'is_active', 'notes')}),
        ('Fare components', {
            'fields': ('base_fare', 'per_km_fare', 'per_min_fare', 'min_fare'),
        }),
        ('Surge + caps', {
            'fields': (
                'night_surge_multiplier',
                'night_surge_start_hour',
                'night_surge_end_hour',
                'surge_cap_multiplier',
            ),
        }),
        ('Commercials', {'fields': ('commission_percent', 'gst_percent')}),
        ('Validity window', {'fields': ('effective_from', 'effective_to')}),
        ('Audit', {'fields': ('created_at', 'updated_at')}),
    )


@admin.register(PlatformSettings)
class PlatformSettingsAdmin(admin.ModelAdmin):
    list_display = ('key', 'setting_type', 'value', 'updated_at', 'updated_by')
    list_filter = ('setting_type',)
    search_fields = ('key', 'description', 'value')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Setting', {'fields': ('key', 'value', 'setting_type', 'description')}),
        ('Audit', {'fields': ('updated_by', 'created_at', 'updated_at')}),
    )
