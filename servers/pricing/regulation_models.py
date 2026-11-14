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
    #: The country's currency (`base.countries`), filled on save: `cancel_penalty_cap_minor` is
    #: an amount in it.
    currency = models.CharField(max_length=3, blank=True, default='')
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
    # `health_insurance_min_sum`, term insurance of at least `term_insurance_min_sum` (WHOLE
    # units of the country's currency: a sum insured has no paise) and `induction_min_hours` of training, all current. Off
    # until operations is ready (`driver_records_gate` NULL/False); the owner turns it on.
    driver_records_gate = models.BooleanField(null=True, blank=True)
    health_insurance_min_sum = models.BigIntegerField(null=True, blank=True)
    term_insurance_min_sum = models.BigIntegerField(null=True, blank=True)
    induction_min_hours = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    #: Why the row holds what it holds, e.g. "state notification not confirmed".
    # What a driver must provide here (10 Nov; servers.driver.requirements): `{item: {"need":
    # "required"|"optional"|"off", "blocks_online": bool}}`. A state row overrides per item.
    driver_requirements = models.JSONField(null=True, blank=True)
    min_driver_age = models.PositiveSmallIntegerField(null=True, blank=True)
    max_vehicle_age_years = models.PositiveSmallIntegerField(null=True, blank=True)
    #: The app and web languages offered here, e.g. ["en", "te", "hi"].
    languages = models.JSONField(null=True, blank=True)
    # ---- Per-state operating rules (14 Nov, USA from day 1; Idaho TNC Act, Idaho Code 49-37).
    # Every field is NULL = "not set here": the country value applies, then the code default
    # (`regulation.market_rules`). None of them changes India unless set on an India row.
    #: Cash may be offered and accepted (Idaho 49-3712: no; payment only through the app).
    cash_allowed = models.BooleanField(null=True, blank=True)
    #: Riders may tip at trip end; the tip goes 100% to the driver.
    tips_enabled = models.BooleanField(null=True, blank=True)
    #: The tip presets offered, in percent of the fare, e.g. [15, 18, 20].
    tip_presets = models.JSONField(null=True, blank=True)
    #: Sales tax on rides is charged and shown. Off in the US unless the CPA says otherwise.
    sales_tax_enabled = models.BooleanField(null=True, blank=True)
    #: The ride classes this market offers, e.g. ["standard", "xl"]; NULL = every class.
    vehicle_classes = models.JSONField(null=True, blank=True)
    #: The oldest model year accepted (US default 2011) and the minimum number of doors.
    min_vehicle_year = models.PositiveSmallIntegerField(null=True, blank=True)
    vehicle_min_doors = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Records kept, in days (Idaho 49-3714: trips 2 years; driver records 1 year after the
    #: driver's activation ends; 49-3709(3): zero-tolerance complaints 2 years).
    trip_record_retention_days = models.PositiveIntegerField(null=True, blank=True)
    driver_record_retention_days = models.PositiveIntegerField(null=True, blank=True)
    complaint_record_retention_days = models.PositiveIntegerField(null=True, blank=True)
    #: Background checks are repeated every N months (0 = never).
    background_recheck_months = models.PositiveSmallIntegerField(null=True, blank=True)
    #: A rider's impairment report suspends the driver at once, pending review (49-3709(2)).
    zero_tolerance_suspend = models.BooleanField(null=True, blank=True)
    #: No driver may go online until the platform insurance below is configured (41-2519).
    platform_insurance_required = models.BooleanField(null=True, blank=True)
    #: The platform's TNC policy: {"insurer", "policy_number", "holder": platform|driver|both,
    #: "logged_in": "...limits...", "on_trip": "...limits...", "certificate_url", "expires"}.
    platform_insurance = models.JSONField(null=True, blank=True)
    #: No driver may go online until a driver fee plan is active here.
    fee_plan_required = models.BooleanField(null=True, blank=True)
    #: The agent for service of process (Idaho 49-3705): {"name", "address", "phone"}.
    service_agent = models.JSONField(null=True, blank=True)
    #: The statute behind each rule, shown in the console: {"cash_allowed": "Idaho Code 49-3712"}.
    citations = models.JSONField(null=True, blank=True)
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

    def save(self, *args, **kwargs):
        from base.countries import get_country

        self.currency = get_country(self.country).currency
        return super().save(*args, **kwargs)

    def __str__(self):
        scope = f'{self.country}-{self.state_code}' if self.state_code else self.country
        return f'Regulation {scope} v{self.version}'
