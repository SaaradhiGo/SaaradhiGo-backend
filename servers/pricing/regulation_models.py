"""Aggregator regulation per country, overridable per state (MVAG 2025, 2026-11-09): the fare
band, the cancellation penalty and the driver-records gate.

The Motor Vehicle Aggregator Guidelines 2025 let each state notify its own base fare and
dynamic-pricing band; the central guideline gives the defaults (India: 0.50x to 2.00x of the
base fare). So the band is CONFIG, never code: one row per country (`state_code` blank) and an
optional row per state. A NULL field on a state row means "not set here": the country value
applies. Changes are the `pricing.change` maker-checker action, like `CommissionCountryConfig`.

What the fare quote applies (`regulation.fare_limits`):
    cap   = the lowest of the rate card's `surge_cap_multiplier`, the state cap, the country cap
    floor = the highest of the state floor and the country floor
"""

from django.db import models

STATE_NOTIFIED = 'state_notified'
PLATFORM = 'platform'
BASE_FARE_SOURCE_CHOICES = [
    (STATE_NOTIFIED, 'Base fare notified by the state'),
    (PLATFORM, 'Base fare set by the platform'),
]


class MarketRegulation(models.Model):
    country = models.CharField(max_length=2)
    #: Blank for the country row; else the ISO 3166-2 sub-division ("TG").
    state_code = models.CharField(max_length=8, blank=True, default='')
    base_fare_source = models.CharField(max_length=16, blank=True, default='',
                                        choices=BASE_FARE_SOURCE_CHOICES)
    max_dynamic_multiplier = models.DecimalField(max_digits=4, decimal_places=2,
                                                 null=True, blank=True)
    min_dynamic_multiplier = models.DecimalField(max_digits=4, decimal_places=2,
                                                 null=True, blank=True)
    # Cancellation penalty (MVAG 2025): an unjustified cancellation costs `percent` of the fare,
    # at most `cap_minor` (in the country's currency), for rider or driver alike, once
    # `free_seconds` have passed since the driver accepted. NULL = not set here.
    cancel_penalty_percent = models.DecimalField(max_digits=5, decimal_places=2,
                                                 null=True, blank=True)
    cancel_penalty_cap_minor = models.BigIntegerField(null=True, blank=True)
    cancel_free_seconds = models.PositiveIntegerField(null=True, blank=True)
    # Driver records gate (MVAG 2025): to go online a driver needs health insurance of at least
    # `health_insurance_min_minor`, term insurance of at least `term_insurance_min_minor` (minor
    # units of the country's currency) and `induction_min_hours` of training, all current. Off
    # until operations is ready (`driver_records_gate` NULL/False); the owner turns it on.
    driver_records_gate = models.BooleanField(null=True, blank=True)
    health_insurance_min_minor = models.BigIntegerField(null=True, blank=True)
    term_insurance_min_minor = models.BigIntegerField(null=True, blank=True)
    induction_min_hours = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    #: Why the row holds what it holds, e.g. "state notification not confirmed".
    note = models.CharField(max_length=255, blank=True, default='')
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by_label = models.CharField(max_length=255, blank=True, default='')
    reason = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['country', 'state_code']
        constraints = [
            models.UniqueConstraint(fields=['country', 'state_code'],
                                    name='market_regulation_unique_scope'),
            models.CheckConstraint(
                condition=(models.Q(max_dynamic_multiplier__isnull=True)
                           | models.Q(max_dynamic_multiplier__gte=1)),
                name='market_regulation_cap_at_least_1'),
            models.CheckConstraint(
                condition=(models.Q(min_dynamic_multiplier__isnull=True)
                           | (models.Q(min_dynamic_multiplier__gt=0)
                              & models.Q(min_dynamic_multiplier__lte=1))),
                name='market_regulation_floor_0_1'),
        ]

    def __str__(self):
        scope = f'{self.country}-{self.state_code}' if self.state_code else self.country
        return f'Regulation {scope} v{self.version}'
