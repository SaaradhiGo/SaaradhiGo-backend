from django.contrib import admin

from servers.pricing.models import PlatformSettings, RateCard, ServiceZone


@admin.register(ServiceZone)
class ServiceZoneAdmin(admin.ModelAdmin):
    """The zone is where a market is configured, so the market fields are
    first-class here rather than buried.

    `currency`, `tax_mode`, `distance_unit` and `timezone_name` all inherit from
    the country when left blank, which is why they are grouped with a note: an
    operator creating a Hyderabad zone sets `country='IN'` and nothing else,
    while one creating a California zone must set the timezone explicitly
    because the US has six.
    """

    list_display = (
        'code', 'name', 'zone_type', 'city', 'state_code',
        'country', 'currency', 'tax_mode', 'distance_unit', 'timezone_name',
        'priority', 'is_active', 'updated_at',
    )
    list_filter = (
        'is_active', 'zone_type', 'country', 'currency', 'tax_mode',
        'distance_unit', 'state_code',
    )
    search_fields = ('code', 'name', 'city', 'state_code')
    ordering = ('-priority', 'code')
    readonly_fields = ('created_at', 'updated_at', 'resolved_market')
    fieldsets = (
        ('Identity', {'fields': ('code', 'name', 'zone_type', 'parent',
                                 'is_active', 'priority')}),
        ('Geography', {'fields': ('country', 'state_code', 'city',
                                  'polygon_geojson')}),
        ('Market semantics', {
            'fields': ('currency', 'timezone_name', 'distance_unit',
                       'tax_mode', 'per_trip_fee', 'per_trip_fee_label',
                       'resolved_market'),
            'description': (
                'Leave blank to inherit from the country. '
                '<strong>Set timezone_name explicitly for any country with more '
                'than one timezone</strong> — a single national value measures '
                "every driver's &ldquo;today&rdquo; against the wrong midnight. "
                '<strong>tax_mode</strong>: <em>inclusive</em> means the quoted '
                'fare already contains the tax and the receipt extracts it '
                '(India/GST); <em>additive</em> means the tax is added on top as '
                'its own line (US sales tax).'
            ),
        }),
        ('Other', {'fields': ('metadata',)}),
        ('Audit', {'fields': ('created_at', 'updated_at')}),
    )

    @admin.display(description='Resolved market profile')
    def resolved_market(self, obj):
        """What this zone will actually behave as, after inheritance.

        Shown because the stored value can be blank while the effective value is
        not, and an operator checking their configuration should not have to know
        the inheritance rules to read it.
        """
        if obj is None or not obj.pk:
            return 'Saved zones only.'
        from base.countries import get_country
        c = get_country(obj.country)
        return (
            f'{c.name} — {obj.currency or c.currency} · '
            f'{obj.tax_mode or c.tax_mode} {c.tax_label} · '
            f'priced per {obj.distance_unit or c.distance_unit} · '
            f'{obj.timezone_name or c.default_timezone}'
        )


@admin.register(RateCard)
class RateCardAdmin(admin.ModelAdmin):
    list_display = (
        'zone', 'zone_market', 'vehicle_type', 'version', 'base_fare',
        'per_km_fare', 'per_min_fare', 'min_fare', 'surge_cap_multiplier',
        'effective_from', 'effective_to', 'is_active',
    )

    @admin.display(description='Currency / unit', ordering='zone__currency')
    def zone_market(self, obj):
        """The units these numbers are in.

        Fare columns are bare decimals, so a rate card for a US zone looks
        identical to one for an Indian zone. Showing the currency and the
        pricing unit next to them is what stops a per-mile rate being typed
        into a per-km field, or dollar magnitudes into a rupee card.
        """
        zone = getattr(obj, 'zone', None)
        if zone is None:
            return '—'
        from base.countries import get_country
        c = get_country(zone.country)
        return f'{zone.currency or c.currency} / {zone.distance_unit or c.distance_unit}'
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
        ('Commercials', {
            'fields': ('gst_percent',),
            'description': (
                'gst_percent is the tax RATE for this zone whatever the market '
                'calls it — GST in India, sales tax in the US. Whether it is '
                'added on top of the fare or extracted from it is the '
                "zone's <strong>tax_mode</strong>, not this field."
            ),
        }),
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
