"""Two defects found by completing a real ride (trip #81) on QA.

1. ADMIN REPORTED GMV 0.00 FOR A COMPLETED CASH RIDE.
   `Trip.final_fare` is written in exactly ONE place -- `ride/promos.py`, when
   a promo code is applied. Every ordinary trip completes with it NULL. The
   per-trip views all knew that and read `final_fare or estimated_fare`; the
   AGGREGATES summed `final_fare` alone, so GMV and revenue counted promo
   trips and nothing else. Trip #81 settled 135.68 INR in cash and every
   money tile read 0.00.

2. `Driver.total_trips` WAS NEVER INCREMENTED by anything, so it read 0 for
   every driver forever.

The numbers below are trip #81's real shape: one completed trip, no promo,
estimated_fare 135.68 INR, paid in cash.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from base.money import charged_fare, sum_by_currency
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus

User = get_user_model()
pytestmark = pytest.mark.django_db

TRIP_81_FARE = Decimal('135.68')


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _driver(phone='+917770001003'):
    user = User.objects.create_user(phone_number=phone, role='driver')
    driver = Driver.objects.create(user_id=user)
    vtype, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vtype, vehicle_number='AP16QA0001',
    )
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    return driver


def _rider(phone='+917770001002'):
    return User.objects.create_user(phone_number=phone, role='rider')


def _actor_email():
    """The operator the recompute is attributed to; `--apply` requires one."""
    email = 'ops.lead@saaradhigo.test'
    if not User.objects.filter(email=email).exists():
        User.objects.create_user(phone_number='+917770009999', email=email,
                                 role='admin', is_staff=True)
    return email


def _trip(rider, driver, *, status='completed', estimated=TRIP_81_FARE,
          final=None, currency='INR', completed=True):
    now = timezone.now()
    return Trip.objects.create(
        user_id=rider,
        driver_id=driver,
        status_id=_status(status),
        pickup_lat=Decimal('16.5062'), pickup_long=Decimal('80.6480'),
        destination_lat=Decimal('16.5190'), destination_long=Decimal('80.6305'),
        estimated_fare=estimated,
        final_fare=final,
        currency=currency,
        payment_method='cash',
        requested_at=now,
        completed_at=now if completed else None,
    )


# --------------------------------------------------------------------------
# 1. the money the dashboard reports
# --------------------------------------------------------------------------

def test_trip_81_shape_is_counted_in_gmv():
    """The exact defect: one completed cash trip, no promo, must not be 0.00."""
    driver, rider = _driver(), _rider()
    _trip(rider, driver)                       # final_fare stays NULL

    totals = sum_by_currency(
        Trip.objects.filter(status_id__status_code='completed'),
        charged_fare(),
    )
    assert totals == {'INR': TRIP_81_FARE}, (
        'a completed trip with no promo must contribute its estimated_fare; '
        'summing final_fare alone is what reported 0.00 for trip #81'
    )


def test_summing_final_fare_alone_reproduces_the_bug():
    """Guards the diagnosis itself, so the reason stays visible."""
    driver, rider = _driver(), _rider()
    _trip(rider, driver)

    broken = sum_by_currency(
        Trip.objects.filter(status_id__status_code='completed'), 'final_fare_minor',
    )
    assert broken.get('INR', Decimal('0.00')) == Decimal('0.00')


def test_promo_trip_contributes_its_discounted_final_fare():
    """A promo trip charged LESS than the estimate. Count what was charged."""
    driver, rider = _driver(), _rider()
    _trip(rider, driver, estimated=Decimal('200.00'), final=Decimal('150.00'))

    totals = sum_by_currency(
        Trip.objects.filter(status_id__status_code='completed'), charged_fare(),
    )
    assert totals == {'INR': Decimal('150.00')}, (
        'the discounted amount is what the rider paid; the estimate is not'
    )


def test_cancelled_trips_contribute_nothing():
    driver, rider = _driver(), _rider()
    _trip(rider, driver, status='cancelled', completed=False)

    totals = sum_by_currency(
        Trip.objects.filter(status_id__status_code='completed'), charged_fare(),
    )
    assert totals == {}


def test_currencies_stay_separate():
    """Never add rupees to dollars to make one bigger number."""
    driver, rider = _driver(), _rider()
    _trip(rider, driver)
    _trip(rider, driver, estimated=Decimal('20.00'), currency='USD')

    totals = sum_by_currency(
        Trip.objects.filter(status_id__status_code='completed'), charged_fare(),
    )
    assert totals == {'INR': TRIP_81_FARE, 'USD': Decimal('20.00')}


def test_a_trip_with_no_fare_at_all_contributes_zero_not_an_error():
    driver, rider = _driver(), _rider()
    _trip(rider, driver, estimated=None)

    totals = sum_by_currency(
        Trip.objects.filter(status_id__status_code='completed'), charged_fare(),
    )
    assert totals.get('INR', Decimal('0.00')) == Decimal('0.00')


def test_gmv_does_not_double_count_the_cash_payment():
    """GMV is fares, not fares PLUS the payment row recording the same money."""
    driver, rider = _driver(), _rider()
    trip = _trip(rider, driver)

    from servers.payments.models import Payment
    Payment.objects.create(
        trip_id=trip, user_id=rider, amount=TRIP_81_FARE,
        currency='INR', method='cash', status='completed',
    )

    totals = sum_by_currency(
        Trip.objects.filter(status_id__status_code='completed'), charged_fare(),
    )
    assert totals == {'INR': TRIP_81_FARE}, (
        'the trip and its payment are the same 135.68, counted once'
    )


def test_dashboard_kpis_report_the_completed_cash_ride():
    """End to end through the real KPI builder the console renders."""
    from servers.ride.admin_views import build_admin_dashboard_kpis

    driver, rider = _driver(), _rider()
    _trip(rider, driver)

    kpis = build_admin_dashboard_kpis()
    gmv = kpis.get('gmv')
    assert gmv is not None
    assert Decimal(str(gmv)) == TRIP_81_FARE, (
        f'God View GMV reported {gmv}, not {TRIP_81_FARE}'
    )


# --------------------------------------------------------------------------
# 2. the driver's completed-trip count
# --------------------------------------------------------------------------

def test_recompute_counts_a_completed_trip_once():
    from django.core.management import call_command

    driver, rider = _driver(), _rider()
    _trip(rider, driver)
    assert driver.total_trips == 0

    call_command('recompute_driver_trip_counts', '--apply', '--actor-email', _actor_email())
    driver.refresh_from_db()
    assert driver.total_trips == 1


def test_recompute_is_idempotent():
    """Running it twice must not double the count."""
    from django.core.management import call_command

    driver, rider = _driver(), _rider()
    _trip(rider, driver)

    call_command('recompute_driver_trip_counts', '--apply', '--actor-email', _actor_email())
    call_command('recompute_driver_trip_counts', '--apply', '--actor-email', _actor_email())
    driver.refresh_from_db()
    assert driver.total_trips == 1


def test_recompute_ignores_cancelled_trips():
    from django.core.management import call_command

    driver, rider = _driver(), _rider()
    _trip(rider, driver)
    _trip(rider, driver, status='cancelled', completed=False)

    call_command('recompute_driver_trip_counts', '--apply', '--actor-email', _actor_email())
    driver.refresh_from_db()
    assert driver.total_trips == 1, 'a cancelled trip is not a trip the driver did'


def test_recompute_without_apply_changes_nothing():
    from django.core.management import call_command

    driver, rider = _driver(), _rider()
    _trip(rider, driver)

    call_command('recompute_driver_trip_counts')
    driver.refresh_from_db()
    assert driver.total_trips == 0, 'reporting mode must not write'


def test_recompute_corrects_an_inflated_counter_downwards():
    """It derives the value; it does not add to whatever was there."""
    from django.core.management import call_command

    driver, rider = _driver(), _rider()
    _trip(rider, driver)
    Driver.objects.filter(id=driver.id).update(total_trips=99)

    call_command('recompute_driver_trip_counts', '--apply', '--actor-email', _actor_email())
    driver.refresh_from_db()
    assert driver.total_trips == 1
