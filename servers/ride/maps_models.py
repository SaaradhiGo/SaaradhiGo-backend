"""Maps cost control (9 Nov): what every Google call cost, per day, and the budget it is held to.

`MapsUsage` is one counter row per (day, SKU, surface, country, trip): the meter
(`maps_meter.record`) increments it at the single point each call is made. `trip_id` is 0 for a
call with no trip (search before booking). Cache hits and misses are counted the same way under
the pseudo-SKUs in `maps_meter.UNBILLED`, so the console can show the route cache hit rate.

`MapsSkuPrice` is the list price per SKU (USD per 1,000 calls) and the free monthly calls, edited
in the console. `MapsBudget` is a country's monthly budget and the degrade switches applied, in
order, once it is spent (`maps_budget`). Booking and SOS are never blocked.
"""

from decimal import Decimal

from django.db import models


class MapsUsage(models.Model):
    day = models.DateField()
    sku = models.CharField(max_length=40)
    #: rider_web | rider_app | driver_app | backend
    surface = models.CharField(max_length=16, default='backend')
    country = models.CharField(max_length=2, default='')
    trip_id = models.BigIntegerField(default=0)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['day', 'sku', 'surface', 'country', 'trip_id'],
                                               name='maps_usage_one_row')]
        indexes = [models.Index(fields=['day', 'country'], name='maps_usage_day_country'),
                   models.Index(fields=['trip_id'], name='maps_usage_trip')]

    def __str__(self):
        return f'MapsUsage {self.day} {self.sku} {self.surface} {self.country} trip={self.trip_id} {self.count}'


class MapsSkuPrice(models.Model):
    sku = models.CharField(max_length=40, unique=True)
    usd_per_1000 = models.DecimalField(max_digits=8, decimal_places=3, default=Decimal('0'))
    free_monthly_calls = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=200, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by_label = models.CharField(max_length=255, blank=True, default='')

    def __str__(self):
        return f'MapsSkuPrice {self.sku} ${self.usd_per_1000}/1000'


class MapsBudget(models.Model):
    country = models.CharField(max_length=2, unique=True)
    #: The month's budget in USD (Google bills in USD). NULL: no budget, no alerts.
    monthly_usd = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    amber_percent = models.PositiveSmallIntegerField(default=80)
    red_percent = models.PositiveSmallIntegerField(default=100)
    #: Degrade steps, applied in this order once the budget is spent. Each is a switch.
    degrade_longer_cache = models.BooleanField(default=True)
    degrade_straight_line_eta = models.BooleanField(default=True)
    degrade_autocomplete_5_chars = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by_label = models.CharField(max_length=255, blank=True, default='')

    def __str__(self):
        return f'MapsBudget {self.country} ${self.monthly_usd}'
