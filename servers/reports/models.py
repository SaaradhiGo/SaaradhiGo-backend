"""Reports & Analytics (module 14): pre-aggregated daily rollups.

DERIVED DATA, NEVER A SOURCE OF TRUTH
-------------------------------------
Every table here is a cache of facts that live elsewhere (trips, settlements, the wallet ledger,
the promotions ledger, sessions, tickets, incidents, flags). A day's rows are deleted and
re-inserted in one transaction by `compute.rebuild_day`, so a rebuild is idempotent and a wrong
rollup is repaired by running it again. Nothing in this app writes a trip, an account, a setting
or a money row; it only reads them. See `compute.py` for what each column is measured from.

A DAY IS A CITY-LOCAL DAY
-------------------------
`date` is the calendar date in the city's own time zone (Hyderabad: IST, Austin: Central), the
same clock the Finance overview and the Command Centre use. A trip at 23:30 Central on the 4th
is on the 4th for Austin whatever UTC says.

MONEY IS PER CURRENCY
---------------------
Amounts are integer minor units of `currency`, and `currency` is part of the key of every money
row. Nothing here, and nothing reading from here, adds INR to USD.

`vehicle_type` is the vehicle class name ('' on a row that cannot be attributed to a class, such
as a wallet reward or a payout). Trips with no pricing zone cannot be placed in a city and are in
no rollup (the same rule as the Finance and Command Centre anchors).
"""

from django.db import models
from django.utils import timezone


class RebuildLog(models.Model):
    """When each (city, date) was last rebuilt: the console's "data as of"."""

    city = models.ForeignKey('admin_access.GeoNode', on_delete=models.CASCADE, related_name='+')
    date = models.DateField()
    built_at = models.DateTimeField(default=timezone.now, db_index=True)
    trigger = models.CharField(max_length=24, blank=True, default='')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['city', 'date'], name='rebuildlog_city_date')]

    def __str__(self):
        return f'{self.city_id} {self.date} @ {self.built_at:%Y-%m-%d %H:%M}'


class DailyCityMetrics(models.Model):
    """Trips and money for one city, local day, vehicle class and currency."""

    city = models.ForeignKey('admin_access.GeoNode', on_delete=models.CASCADE, related_name='+')
    date = models.DateField(db_index=True)
    vehicle_type = models.CharField(max_length=50, blank=True, default='')
    currency = models.CharField(max_length=3)

    # Trip counts. Requested, accepted and the three cancellation causes are counted on the day
    # the trip was REQUESTED; completed on the day it COMPLETED (the Command Centre's rule).
    trips_requested = models.PositiveIntegerField(default=0)
    trips_accepted = models.PositiveIntegerField(default=0)
    trips_completed = models.PositiveIntegerField(default=0)
    cancelled_by_rider = models.PositiveIntegerField(default=0)
    cancelled_by_driver = models.PositiveIntegerField(default=0)
    cancelled_by_ops = models.PositiveIntegerField(default=0)
    no_driver_found = models.PositiveIntegerField(default=0)

    # Distance and time of the trips completed that day.
    distance_km = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    duration_min = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    # Pickup ETA (accepted -> reached), for the trips requested that day that reached pickup.
    eta_count = models.PositiveIntegerField(default=0)
    eta_seconds_sum = models.BigIntegerField(default=0)
    eta_p50_seconds = models.PositiveIntegerField(null=True, blank=True)
    eta_p90_seconds = models.PositiveIntegerField(null=True, blank=True)

    # Money (minor units of `currency`).
    settled_trips = models.PositiveIntegerField(default=0)
    gmv_minor = models.BigIntegerField(default=0)                    # TripSettlement.gross_fare_minor
    rider_paid_minor = models.BigIntegerField(default=0)             # charged fare of trips completed
    commission_minor = models.BigIntegerField(default=0)             # TripSettlement.commission_amount_minor
    driver_net_minor = models.BigIntegerField(default=0)             # TripSettlement.driver_net_minor
    promo_cost_minor = models.BigIntegerField(default=0)             # promotions.LedgerEntry, discount kind
    platform_funded_trip_end_minor = models.BigIntegerField(default=0)   # wallet ledger kind
    refunds_minor = models.BigIntegerField(default=0)                # TripMoneyAction kind=refund
    incentive_rewards_minor = models.BigIntegerField(default=0)      # wallet ledger kind
    referral_rewards_minor = models.BigIntegerField(default=0)       # wallet ledger kind
    reward_recovery_minor = models.BigIntegerField(default=0)        # wallet ledger kind (a debit taken back)
    regulatory_fee_expense_minor = models.BigIntegerField(default=0)  # PlatformExpense

    built_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['city', 'date', 'vehicle_type', 'currency'], name='dailycity_key')]
        indexes = [models.Index(fields=['city', 'date'], name='dailycity_city_date_idx')]


class DailyPayouts(models.Model):
    """Driver payouts by the driver's home city and day (Finance overview rules)."""

    city = models.ForeignKey('admin_access.GeoNode', on_delete=models.CASCADE, related_name='+')
    date = models.DateField(db_index=True)
    currency = models.CharField(max_length=3)
    requested_count = models.PositiveIntegerField(default=0)
    requested_minor = models.BigIntegerField(default=0)
    paid_count = models.PositiveIntegerField(default=0)
    paid_minor = models.BigIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    failed_minor = models.BigIntegerField(default=0)
    built_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['city', 'date', 'currency'], name='dailypayouts_key')]


class DailyDriverSupply(models.Model):
    """Drivers of one home city on one local day."""

    city = models.ForeignKey('admin_access.GeoNode', on_delete=models.CASCADE, related_name='+')
    date = models.DateField(db_index=True)
    active_drivers = models.PositiveIntegerField(default=0)       # at least one completed trip
    online_drivers = models.PositiveIntegerField(default=0)       # at least one online session
    online_seconds = models.BigIntegerField(default=0)            # DriverSession time, clipped to the day
    new_approved_drivers = models.PositiveIntegerField(default=0)
    built_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['city', 'date'], name='dailysupply_key')]


class DailyRiderMetrics(models.Model):
    """Riders of one city on one local day."""

    city = models.ForeignKey('admin_access.GeoNode', on_delete=models.CASCADE, related_name='+')
    date = models.DateField(db_index=True)
    new_riders = models.PositiveIntegerField(default=0)           # signed up that day, placed in this city
    active_riders = models.PositiveIntegerField(default=0)        # requested at least one trip
    first_trip_riders = models.PositiveIntegerField(default=0)    # first ever completed trip, here
    built_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['city', 'date'], name='dailyriders_key')]


class WeeklyRiderCohort(models.Model):
    """Riders whose first completed trip fell in `cohort_week` (Monday, city-local) in this city,
    and how many of them rode again `weeks_since` weeks later."""

    city = models.ForeignKey('admin_access.GeoNode', on_delete=models.CASCADE, related_name='+')
    cohort_week = models.DateField(db_index=True)
    weeks_since = models.PositiveSmallIntegerField()
    cohort_size = models.PositiveIntegerField(default=0)
    retained = models.PositiveIntegerField(default=0)
    built_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['city', 'cohort_week', 'weeks_since'], name='cohort_key')]


class DailySafety(models.Model):
    """Safety incidents (by category and severity) and SOS events, by anchor city and local day.

    `source='sos'` rows have `kind` = the SOS event type and `severity=''`; the row with
    `kind='*'` is every SOS of the day and carries the response-time figures (raised ->
    first acknowledgement). No trip id, coordinate or person is stored here.
    """

    SOURCE_INCIDENT, SOURCE_SOS = 'incident', 'sos'

    city = models.ForeignKey('admin_access.GeoNode', on_delete=models.CASCADE, related_name='+')
    date = models.DateField(db_index=True)
    source = models.CharField(max_length=10)
    kind = models.CharField(max_length=32)
    severity = models.CharField(max_length=10, blank=True, default='')
    count = models.PositiveIntegerField(default=0)
    responded_count = models.PositiveIntegerField(default=0)
    response_seconds_sum = models.BigIntegerField(default=0)
    response_median_seconds = models.PositiveIntegerField(null=True, blank=True)
    built_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['city', 'date', 'source', 'kind', 'severity'], name='dailysafety_key')]


class DailySupport(models.Model):
    """Support tickets by anchor city and local day (opened / resolved / SLA)."""

    city = models.ForeignKey('admin_access.GeoNode', on_delete=models.CASCADE, related_name='+')
    date = models.DateField(db_index=True)
    opened = models.PositiveIntegerField(default=0)
    resolved = models.PositiveIntegerField(default=0)
    first_response_breached = models.PositiveIntegerField(default=0)   # of those opened that day
    resolution_breached = models.PositiveIntegerField(default=0)       # of those opened that day
    first_response_count = models.PositiveIntegerField(default=0)
    first_response_seconds_sum = models.BigIntegerField(default=0)
    built_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['city', 'date'], name='dailysupport_key')]


class DailyFraud(models.Model):
    """Fraud flags by anchor city, local day and rule kind: raised, confirmed, dismissed."""

    city = models.ForeignKey('admin_access.GeoNode', on_delete=models.CASCADE, related_name='+')
    date = models.DateField(db_index=True)
    kind = models.CharField(max_length=40)
    raised = models.PositiveIntegerField(default=0)
    confirmed = models.PositiveIntegerField(default=0)
    dismissed = models.PositiveIntegerField(default=0)
    built_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['city', 'date', 'kind'], name='dailyfraud_key')]
