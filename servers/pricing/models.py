"""Multi-city pricing data model.

Replaces the single-row-per-vehicle-type VehicleFarePricing table and the
hardcoded Hyderabad polygon in base/service_area.py with two normalised
tables:

  ServiceZone  -- A polygon on the globe where SaaradhiGo operates.
                  Hierarchical (country -> state -> city -> sub-zone).
                  Polygons are stored as GeoJSON inside a JSONField so we
                  do not require PostGIS — point-in-polygon checks use
                  Shapely in-process.

  RateCard     -- Versioned, effective-dated fare schedule per
                  (zone x vehicle_type). Exactly one card is active at
                  any given time per (zone, vehicle_type); the resolver
                  in pricing/services.py walks the parent chain so a
                  city-level card serves as the default when no
                  sub-zone-specific card exists.

The lookup contract used by the rest of the system is:

    quote_fare(pickup_lat, pickup_lon, drop_lat, drop_lon, vehicle_type)
        -> {total_fare, base_fare, distance_fare, time_fare,
            surge_multiplier, zone, rate_card, ...}

Adding a new city is a *data* operation (insert one ServiceZone +
N RateCards), not a code change. Scheduling a fare change is also
data-only: insert a new RateCard with a future effective_from and the
resolver will pick it up at that timestamp.
"""

import json
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from servers.driver.models import VehicleType


class PlatformSettings(models.Model):
    """Runtime config stored in the database so operators can change it
    without restarting the web process.

    This avoids downtime caused by editing env vars and rolling a new app
    container just to tweak business settings like the platform commission.
    """

    SETTING_TYPES = [
        ('decimal', 'Decimal'),
        ('integer', 'Integer'),
        ('string', 'String'),
        ('boolean', 'Boolean'),
        ('json', 'JSON'),
    ]

    key = models.CharField(
        max_length=256,
        unique=True,
        db_index=True,
        help_text='Stable setting key, e.g. "PLATFORM_COMMISSION_PERCENT".',
    )
    value = models.TextField(help_text='Serialized value; typed by setting_type.')
    setting_type = models.CharField(max_length=20, choices=SETTING_TYPES, default='string')
    description = models.TextField(blank=True, default='')
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='platform_settings_updates',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'Platform Settings'
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.key} = {self.value}'

    def get_value(self):
        if self.setting_type == 'decimal':
            return Decimal(self.value)
        if self.setting_type == 'integer':
            return int(self.value)
        if self.setting_type == 'boolean':
            return self.value.lower() in {'true', '1', 'yes', 'y'}
        if self.setting_type == 'json':
            return json.loads(self.value)
        return self.value

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete(f'platform_setting:{self.key}')


class ServiceZone(models.Model):
    """Geographic zone where SaaradhiGo operates.

    Use the four-level hierarchy (country -> state -> city -> subzone)
    so we can scope rate cards at the right level. A trip's pickup
    coordinate matches AT MOST ONE leaf zone; the resolver then walks
    parents to find the most specific RateCard.
    """

    ZONE_TYPE_CHOICES = [
        ('country', 'Country'),
        ('state', 'State'),
        ('city', 'City'),
        ('subzone', 'Sub-zone'),
    ]

    code = models.CharField(
        max_length=64,
        unique=True,
        help_text='Stable identifier, e.g. "IN-TG-HYD" or "IN-TG-HYD-AIRPORT".',
    )
    name = models.CharField(max_length=128)
    country = models.CharField(max_length=2, default='IN')
    state_code = models.CharField(
        max_length=8,
        blank=True,
        help_text='ISO 3166-2 sub-division code without the country prefix, e.g. "TG", "KA".',
    )
    city = models.CharField(max_length=64, blank=True)

    zone_type = models.CharField(max_length=16, choices=ZONE_TYPE_CHOICES)
    parent = models.ForeignKey(
        'self',
        null=True, blank=True,
        related_name='children',
        on_delete=models.PROTECT,
    )

    # GeoJSON Polygon with [lon, lat] vertices, e.g.
    # {"type": "Polygon", "coordinates": [[[78.205, 17.190], ...]]}.
    # Stored as JSON so we don't require PostGIS; Shapely loads it on
    # demand in pricing.services.
    polygon_geojson = models.JSONField()

    # Higher priority wins when multiple zones contain a point. Used so
    # an airport sub-zone (priority 100) wins over the city (priority 10).
    priority = models.IntegerField(default=10)

    # Currency + timezone metadata. We're INR-only for now but storing
    # this on the zone keeps later expansion clean.
    currency = models.CharField(max_length=3, default='INR')
    timezone_name = models.CharField(max_length=64, default='Asia/Kolkata')

    is_active = models.BooleanField(default=True, db_index=True)

    # Free-form attrs (launch_date, operator notes, license refs).
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['is_active', 'zone_type'], name='zone_active_type_idx'),
            models.Index(fields=['country', 'state_code', 'city'], name='zone_country_state_city_idx'),
            models.Index(fields=['priority'], name='zone_priority_idx'),
        ]
        ordering = ['-priority', 'code']

    def __str__(self):
        return f'{self.code} ({self.name})'


class RateCard(models.Model):
    """Versioned, effective-dated fare schedule per (zone, vehicle_type).

    At any given timestamp T, there is at most one card with
    is_active=True AND effective_from <= T AND (effective_to IS NULL OR
    effective_to > T) for a given (zone, vehicle_type). Scheduling a
    rate change = inserting a new card with a future effective_from;
    the resolver picks it up automatically without ops intervention.
    """

    zone = models.ForeignKey(
        ServiceZone,
        on_delete=models.PROTECT,
        related_name='rate_cards',
    )
    vehicle_type = models.ForeignKey(
        VehicleType,
        on_delete=models.PROTECT,
        related_name='zone_rate_cards',
    )

    base_fare = models.DecimalField(max_digits=10, decimal_places=2)
    per_km_fare = models.DecimalField(max_digits=10, decimal_places=2)
    per_min_fare = models.DecimalField(max_digits=10, decimal_places=2)
    min_fare = models.DecimalField(max_digits=10, decimal_places=2)

    # Time-of-day surcharge applied automatically when the local hour
    # falls in [night_surge_start_hour, night_surge_end_hour). If the
    # multiplier is 1.00 this is a no-op.
    night_surge_multiplier = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('1.00'),
    )
    night_surge_start_hour = models.IntegerField(default=23)
    night_surge_end_hour = models.IntegerField(default=5)

    # Maximum dynamic surge multiplier permitted in this zone. MVA 2020
    # caps aggregator surge at 1.5x normal fare; configurable per zone
    # because different state regulators may pick different caps.
    surge_cap_multiplier = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('1.50'),
    )

    # Driver commission percentage taken by SaaradhiGo (informational;
    # actual driver payout computed in payments service).
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('18.00'),
    )

    # GST rate. Phase-0: 5% on taxi services without ITC. Configurable
    # because RBI/CBIC may shift this.
    gst_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('5.00'),
    )

    effective_from = models.DateTimeField(default=timezone.now, db_index=True)
    effective_to = models.DateTimeField(null=True, blank=True, db_index=True)
    version = models.IntegerField(default=1)

    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=['zone', 'vehicle_type', 'is_active'],
                name='ratecard_zone_vt_active_idx',
            ),
            models.Index(
                fields=['effective_from', 'effective_to'],
                name='ratecard_effective_idx',
            ),
        ]
        ordering = ['-effective_from', '-version']

    def __str__(self):
        return f'{self.zone.code} / {self.vehicle_type.type} v{self.version}'

    def is_currently_effective(self, at=None):
        at = at or timezone.now()
        if not self.is_active:
            return False
        if self.effective_from and at < self.effective_from:
            return False
        if self.effective_to and at >= self.effective_to:
            return False
        return True
