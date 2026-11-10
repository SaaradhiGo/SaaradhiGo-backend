"""Driver fee plans ("Fair Share", owner brief 9 Nov). See `servers.pricing.fee_plans`.

A market (a country, or a state or city overriding it, resolved city -> state -> country like the
payout settings) has at most one ACTIVE plan at a time. The existing percentage commission is the
plan type `percent_commission`: under it nothing changes (the commission rules keep governing, and
settlement takes commission per trip as today). Under any other type the trip settles with no
commission and ONE `platform_fee` ledger row is written per driver and local day
(`fee_plans.charge_day`).

Amounts are per vehicle class: `amounts = {"sedan": {"per_trip_fee_minor": 1000, ...}, "*": {...}}`
("*" is every class not listed). A plan is versioned like a commission rule:
draft -> pending_approval -> active -> archived, through the `pricing.change` maker-checker.
"""

from django.db import models
from django.utils import timezone

PERCENT_COMMISSION = 'percent_commission'
PER_TRIP_FEE = 'per_trip_fee'
DAILY_PASS = 'daily_pass'
FAIR_SHARE = 'fair_share'
PLAN_TYPES = [
    (PERCENT_COMMISSION, 'Percentage commission (today)'),
    (PER_TRIP_FEE, 'Fixed fee per trip'),
    (DAILY_PASS, 'Daily pass'),
    (FAIR_SHARE, 'Fair Share: per-trip fee, daily cap, earnings floor'),
]
DRAFT, PENDING_APPROVAL, ACTIVE, ARCHIVED = 'draft', 'pending_approval', 'active', 'archived'
STATUSES = [(DRAFT, 'Draft'), (PENDING_APPROVAL, 'Pending approval'), (ACTIVE, 'Active'),
            (ARCHIVED, 'Archived')]


class DriverFeePlan(models.Model):
    country = models.CharField(max_length=2, db_index=True)
    #: '' for the country; else the GeoNode code of the state or city ('IN-TG', 'IN-TG-HYD').
    scope_code = models.CharField(max_length=32, blank=True, default='')
    currency = models.CharField(max_length=3, blank=True, default='')
    plan_type = models.CharField(max_length=24, choices=PLAN_TYPES)
    #: Per vehicle class: per_trip_fee_minor, daily_pass_minor, fee_free_after_trips,
    #: commission_percent (percent_commission, informational: the rules still govern),
    #: rider_platform_fee_minor. "*" is the default class.
    amounts = models.JSONField(default=dict, blank=True)
    #: A local day whose completed fares are below this pays nothing. 0 = off.
    earnings_floor_minor = models.BigIntegerField(default=0)
    #: The most a driver pays in one local day. NULL = no cap.
    daily_cap_minor = models.BigIntegerField(null=True, blank=True)
    #: Launch waiver: no fee before this date, or for a driver's first N days after their first trip.
    waiver_until = models.DateField(null=True, blank=True)
    waiver_first_days = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default=DRAFT, db_index=True)
    version = models.PositiveIntegerField(default=1)
    #: The first local day the plan governs (a change never applies in the middle of a day).
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['country', 'scope_code', '-version']
        constraints = [models.UniqueConstraint(fields=['country', 'scope_code', 'version'],
                                               name='fee_plan_version_unique')]

    def save(self, *args, **kwargs):
        from base.countries import get_country

        self.country = str(self.country).upper()
        self.currency = get_country(self.country).currency
        return super().save(*args, **kwargs)

    def amounts_for(self, vehicle_type):
        table = self.amounts or {}
        key = str(vehicle_type or '').lower()
        return {**(table.get('*') or {}), **(table.get(key) or {})}

    def __str__(self):
        return f'DriverFeePlan {self.country}{"/" + self.scope_code if self.scope_code else ""} ' \
               f'{self.plan_type} v{self.version} {self.status}'


class DriverDailyFee(models.Model):
    """One driver's platform fee for one local day: the ledger row's explanation."""

    driver = models.ForeignKey('driver.Driver', on_delete=models.PROTECT, related_name='daily_fees')
    day = models.DateField()
    plan = models.ForeignKey(DriverFeePlan, on_delete=models.PROTECT, related_name='daily_fees')
    plan_version = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    trips = models.PositiveIntegerField(default=0)
    fares_minor = models.BigIntegerField(default=0)
    fee_minor = models.BigIntegerField(default=0)
    #: What the fee would have been before the legal 80% guard, when the guard reduced it.
    guard_reduced_from_minor = models.BigIntegerField(null=True, blank=True)
    #: 'charged' | 'waiver' | 'under_floor' | 'no_trips'
    reason = models.CharField(max_length=16)
    explanation = models.CharField(max_length=255)
    trip_ids = models.JSONField(default=list)
    wallet_transaction_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['driver', 'day'], name='daily_fee_one_per_day')]

    def __str__(self):
        return f'DriverDailyFee driver={self.driver_id} {self.day} {self.fee_minor} {self.reason}'


class MonthlyFixedCost(models.Model):
    """Open books (B4): the fixed costs Srini enters for a market and month (minor units)."""

    country = models.CharField(max_length=2)
    month = models.DateField(help_text='The first day of the month.')
    amount_minor = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3, blank=True, default='')
    note = models.CharField(max_length=255, blank=True, default='')
    updated_by_label = models.CharField(max_length=255, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['country', 'month'], name='fixed_cost_one_per_month')]
