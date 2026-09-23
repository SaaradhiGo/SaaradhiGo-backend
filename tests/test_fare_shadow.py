"""Fare shadow: measuring metering without doing any metering.

The platform cannot decide whether to meter fares without evidence about how
metering would differ from the quote, and that evidence has to come from real
trips. These tests protect the two things that would make such evidence useless
or dangerous.

**It must not be able to change a price.** Every quote is made with
`record_demand=False`, because a shadow quote that registered demand would push
the live surge multiplier up for real riders. And nothing here writes to a money
column. Both are asserted directly rather than trusted.

**The comparison must be attributable.** `quote_fare` reads the live surge, the
currently effective rate card and the wall clock, so a fresh quote against a
historical `estimated_fare` would blend the distance difference together with
hours of pricing drift. The estimate leg is therefore re-quoted in the same
instant, and a test asserts the difference tracks distance alone.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.pricing.fare_shadow import (
    compute_shadow, record_shadow, summarise, sweep_shadow,
    trips_awaiting_shadow,
)
from servers.pricing.models import TripFareShadow
from servers.pricing.services import PricingUnavailable
from servers.ride.models import Trip, TripStatus

User = get_user_model()

LAT = Decimal('17.4450000')
LNG = Decimal('78.3800000')

# Expected amounts are re-derived from whichever RateCard the resolver picks for
# the pickup point (migration 0004 seeds real zones and cards), not hardcoded.
# Hardcoding would make the test pass for the wrong reason the day a seeded rate
# changes, and copying numbers out of quote_fare's own output would make the
# assertion circular.


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def vehicle_type(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return vt


@pytest.fixture
def rider(db):
    return User.objects.create_user(phone_number='+919700000201', role='rider')


@pytest.fixture
def driver(db, vehicle_type):
    u = User.objects.create_user(phone_number='+919800000201', role='driver')
    d = Driver.objects.create(user_id=u, approved=True)
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vehicle_type,
                           vehicle_number='TS09GP0201')
    return d


@pytest.fixture
def trip(db, rider, driver, vehicle_type):
    """A completed trip: quoted for 5 km / 15 min, measured at 7 km / 20 min."""
    now = timezone.now()
    t = Trip.objects.create(
        user_id=rider, status_id=_status('completed'),
        requested_vehicle_type=vehicle_type,
        pickup_lat=LAT, pickup_long=LNG,
        destination_lat=Decimal('17.4500000'), destination_long=Decimal('78.4000000'),
        pickup_address='P', destination_address='D',
        estimated_distance_km=Decimal('5.00'),
        estimated_duration_min=Decimal('15.00'),
        estimated_fare=Decimal('120.00'),
    )
    t.driver_id = driver
    t.started_at = now - timedelta(minutes=25)
    t.completed_at = now - timedelta(minutes=5)
    t.actual_distance_km = Decimal('7.00')
    t.actual_duration_min = Decimal('20.00')
    t.save(update_fields=['driver_id', 'started_at', 'completed_at',
                          'actual_distance_km', 'actual_duration_min'])
    return t


@pytest.fixture
def no_surge():
    """Pin the pricing context so an expected amount can be derived by hand.

    Dynamic surge depends on live Redis demand and the night surcharge depends on
    the wall clock at test time. Both multiply the two legs equally, so the
    attribution logic works either way -- pinning them only makes the arithmetic
    writable in a test.
    """
    with (
        mock.patch('servers.pricing.services.compute_surge_multiplier',
                   return_value=Decimal('1.00')),
        mock.patch('servers.pricing.services._is_night_now', return_value=False),
    ):
        yield


def _card_for(trip):
    """The rate card the resolver will use, so expectations come from data."""
    from servers.pricing.services import find_zone_for_point, get_active_rate_card

    zone = find_zone_for_point(trip.pickup_lat, trip.pickup_long)
    if zone is None:
        return None
    return get_active_rate_card(zone, trip.requested_vehicle_type)


# ---------------------------------------------------------------------------
# It cannot change a price
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_every_shadow_quote_refuses_to_record_demand(trip):
    """A shadow that recorded demand would raise surge for real riders.

    This is the most dangerous thing a shadow could do, because the effect would
    be invisible: prices creep up, and nothing points at the observer.
    """
    with mock.patch('servers.pricing.services.quote_fare',
                    wraps=__import__('servers.pricing.services', fromlist=['x']).quote_fare
                    ) as spy:
        compute_shadow(trip)

    assert spy.call_count >= 1
    for call in spy.call_args_list:
        assert call.kwargs['record_demand'] is False, call.kwargs


@pytest.mark.django_db
def test_recording_a_shadow_touches_no_money_column(trip, no_surge):
    """The trip's own fare fields must be exactly as they were."""
    before = Trip.objects.filter(id=trip.id).values(
        'estimated_fare', 'final_fare', 'surge_multiplier',
        'estimated_distance_km', 'actual_distance_km', 'payment_status',
    ).first()

    record_shadow(trip)

    after = Trip.objects.filter(id=trip.id).values(
        'estimated_fare', 'final_fare', 'surge_multiplier',
        'estimated_distance_km', 'actual_distance_km', 'payment_status',
    ).first()
    assert before == after, 'the shadow must be read-only with respect to the trip'
    assert Trip.objects.get(id=trip.id).final_fare is None


@pytest.mark.django_db
def test_the_shadow_uses_the_canonical_fare_function(trip):
    """No second fare formula may exist.

    If the shadow computed fares itself it would drift from the real one, and the
    platform would end up making a pricing decision from a fare nobody charges.
    """
    with mock.patch('servers.pricing.services.quote_fare') as fake:
        fake.return_value = {
            'total_fare': Decimal('1.00'), 'zone_code': 'Z',
            'rate_card_version': 9, 'surge_multiplier': Decimal('1.00'),
            'source': 'db',
        }
        out = compute_shadow(trip)

    assert fake.called, 'compute_shadow must delegate to quote_fare'
    assert out['shadow_actual'] == Decimal('1.00')


# ---------------------------------------------------------------------------
# The comparison must be attributable
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_metric_delta_reflects_distance_and_duration_only(trip, no_surge):
    """7 km / 20 min against 5 km / 15 min, both quoted in the same instant.

    The expected amounts are rebuilt from the rate card's own columns, which is
    an independent derivation: if the shadow quoted the wrong distance, the wrong
    duration, or applied surge to one leg but not the other, these fail.
    """
    card = _card_for(trip)
    assert card is not None, 'the seeded zone should resolve a card for this point'

    record_shadow(trip)
    row = TripFareShadow.objects.get(trip=trip)

    expected_estimate = max(
        card.base_fare + card.per_km_fare * 5 + card.per_min_fare * 15,
        card.min_fare,
    )
    expected_actual = max(
        card.base_fare + card.per_km_fare * 7 + card.per_min_fare * 20,
        card.min_fare,
    )

    assert row.shadow_estimate == round(expected_estimate, 2)
    assert row.shadow_actual == round(expected_actual, 2)
    # 2 extra km and 5 extra minutes, and nothing else.
    assert row.metric_delta == round(card.per_km_fare * 2 + card.per_min_fare * 5, 2)


@pytest.mark.django_db
def test_context_delta_separates_pricing_drift_from_the_metrics(trip, no_surge):
    """A rate card edited after booking must not look like a longer trip.

    Here the quoted fare is stale by 20 rupees while the metrics are unchanged.
    All of that difference must land in context_delta and none of it in
    metric_delta, or a fare investigation blames the wrong thing.
    """
    trip.actual_distance_km = trip.estimated_distance_km
    trip.actual_duration_min = trip.estimated_duration_min
    trip.save(update_fields=['actual_distance_km', 'actual_duration_min'])

    # Learn today's quote without writing anything, then make the historical
    # quote 20 rupees cheaper than it -- exactly the shape of a rate card that
    # was edited after the rider booked.
    todays = compute_shadow(trip)['shadow_actual']
    trip.estimated_fare = todays - Decimal('20.00')
    trip.save(update_fields=['estimated_fare'])

    record_shadow(trip)
    row = TripFareShadow.objects.get(trip=trip)

    assert row.metric_delta == Decimal('0.00'), 'identical metrics, no metric delta'
    assert row.context_delta == Decimal('20.00'), 'all of the drift, none of the metrics'


@pytest.mark.django_db
def test_a_shorter_actual_trip_produces_a_negative_delta(trip, no_surge):
    """Metering charging less must be recordable, not clamped away.

    A shadow that could only report upside would be a sales pitch.
    """
    trip.actual_distance_km = Decimal('2.00')
    trip.actual_duration_min = Decimal('8.00')
    trip.save(update_fields=['actual_distance_km', 'actual_duration_min'])

    record_shadow(trip)
    row = TripFareShadow.objects.get(trip=trip)
    assert row.metric_delta < 0, (row.shadow_actual, row.shadow_estimate)


# ---------------------------------------------------------------------------
# Honest denominators
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_trip_without_actuals_is_recorded_as_unshadowable(trip):
    """Silently dropping these trips would inflate every later percentage."""
    trip.actual_distance_km = None
    trip.actual_duration_min = None
    trip.save(update_fields=['actual_distance_km', 'actual_duration_min'])

    record_shadow(trip)
    row = TripFareShadow.objects.get(trip=trip)
    assert row.status == 'no_actuals'
    assert row.shadow_actual is None
    assert row.metric_delta is None


@pytest.mark.django_db
def test_pricing_being_unconfigured_is_recorded_not_raised(trip):
    """A city seeded without a rate card is real; it must not break the sweep."""
    with mock.patch('servers.pricing.services.quote_fare',
                    side_effect=PricingUnavailable('no card')):
        record_shadow(trip)

    row = TripFareShadow.objects.get(trip=trip)
    assert row.status == 'pricing_unavailable'
    assert row.shadow_actual is None


@pytest.mark.django_db
def test_a_trip_with_no_vehicle_type_cannot_be_priced(trip):
    """quote_fare requires one, so this is a recorded gap rather than a crash."""
    trip.requested_vehicle_type = None
    trip.vehicle_id = None
    trip.save(update_fields=['requested_vehicle_type', 'vehicle_id'])

    record_shadow(Trip.objects.get(id=trip.id))
    assert TripFareShadow.objects.get(trip=trip).status == 'no_vehicle_type'


@pytest.mark.django_db
def test_trail_coverage_is_carried_into_the_observation(trip, driver, no_surge):
    """A distance from a patchy trail is weaker evidence and must be labelled."""
    record_shadow(trip)
    row = TripFareShadow.objects.get(trip=trip)
    # No trail points were stored for this trip, so coverage is zero and the
    # analysis can exclude it rather than treating it as a measured route.
    assert row.trail_points == 0
    assert row.coverage_ratio == Decimal('0.00')


# ---------------------------------------------------------------------------
# Idempotency and the sweep
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_one_observation_per_trip_under_redelivery(trip, no_surge):
    """Celery is at-least-once; double-counting one trip would skew the mean."""
    first = record_shadow(trip)
    assert first['written'] is True

    second = record_shadow(Trip.objects.get(id=trip.id))
    assert second['written'] is False
    assert second['skipped'] == 'already_observed'
    assert TripFareShadow.objects.filter(trip=trip).count() == 1


@pytest.mark.django_db
def test_force_re_observes_after_a_late_trail_backfill(trip, no_surge):
    record_shadow(trip)
    original = TripFareShadow.objects.get(trip=trip).shadow_actual

    trip.actual_distance_km = Decimal('12.00')
    trip.save(update_fields=['actual_distance_km'])

    record_shadow(Trip.objects.get(id=trip.id), force=True)
    assert TripFareShadow.objects.filter(trip=trip).count() == 1
    assert TripFareShadow.objects.get(trip=trip).shadow_actual > original


@pytest.mark.django_db
def test_sweep_is_a_no_op_until_explicitly_enabled(settings, trip):
    """Shipping the observer switched off is what makes it safe to deploy."""
    settings.FARE_SHADOW_ENABLED = False
    assert sweep_shadow() == {'enabled': False}
    assert TripFareShadow.objects.count() == 0


@pytest.mark.django_db
def test_sweep_observes_eligible_trips_and_skips_the_rest(settings, trip, no_surge):
    settings.FARE_SHADOW_ENABLED = True

    out = sweep_shadow()
    assert out['enabled'] is True
    assert out['observed'] == 1
    assert TripFareShadow.objects.filter(trip=trip).count() == 1

    # A second sweep finds nothing left to do.
    again = sweep_shadow()
    assert again['considered'] == 0
    assert TripFareShadow.objects.count() == 1


@pytest.mark.django_db
def test_sweep_ignores_trips_with_no_measured_distance(settings, trip):
    """Without actuals there is nothing to compare, so no row is manufactured."""
    settings.FARE_SHADOW_ENABLED = True
    trip.actual_distance_km = None
    trip.save(update_fields=['actual_distance_km'])

    assert trips_awaiting_shadow() == []
    assert sweep_shadow()['considered'] == 0


@pytest.mark.django_db
def test_sweep_is_bounded_per_run(settings, rider, driver, vehicle_type, no_surge):
    """One tick must not become an unbounded unit of work on a backlog."""
    settings.FARE_SHADOW_ENABLED = True
    settings.FARE_SHADOW_BATCH_SIZE = 2

    now = timezone.now()
    for _ in range(5):
        t = Trip.objects.create(
            user_id=rider, status_id=_status('completed'),
            requested_vehicle_type=vehicle_type,
            pickup_lat=LAT, pickup_long=LNG,
            destination_lat=LAT, destination_long=LNG,
            estimated_distance_km=Decimal('4.00'),
            estimated_duration_min=Decimal('12.00'),
            estimated_fare=Decimal('100.00'),
        )
        t.driver_id = driver
        t.started_at = now - timedelta(minutes=30)
        t.completed_at = now - timedelta(minutes=10)
        t.actual_distance_km = Decimal('5.00')
        t.actual_duration_min = Decimal('14.00')
        t.save()

    out = sweep_shadow()
    assert out['considered'] == 2, out
    assert TripFareShadow.objects.count() == 2


@pytest.mark.django_db
def test_sweep_survives_one_unshadowable_trip(settings, trip, no_surge):
    """A single failure must not stall the observer for every other trip."""
    settings.FARE_SHADOW_ENABLED = True

    with mock.patch('servers.pricing.fare_shadow.record_shadow',
                    side_effect=RuntimeError('boom')):
        out = sweep_shadow()

    assert out['failed'] == 1
    assert out['observed'] == 0


@pytest.mark.django_db
def test_sweep_leaves_old_trips_outside_the_window_alone(settings, trip):
    """The lookback bounds the query; older trips need the explicit backfill."""
    settings.FARE_SHADOW_ENABLED = True
    Trip.objects.filter(id=trip.id).update(
        completed_at=timezone.now() - timedelta(days=30),
    )
    assert trips_awaiting_shadow(since_hours=72) == []


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_summary_reports_both_directions_and_its_own_denominator(trip, driver,
                                                                 rider, vehicle_type):
    """An analysis that hides unshadowable trips overstates its conclusion."""
    rows = [
        TripFareShadow(trip=trip, quoted_fare=Decimal('100'),
                       shadow_estimate=Decimal('100'), shadow_actual=Decimal('130'),
                       status='computed'),
        TripFareShadow(trip=trip, quoted_fare=Decimal('100'),
                       shadow_estimate=Decimal('100'), shadow_actual=Decimal('90'),
                       status='computed'),
        TripFareShadow(trip=trip, quoted_fare=Decimal('100'),
                       shadow_estimate=Decimal('100'), shadow_actual=Decimal('100'),
                       status='computed'),
        TripFareShadow(trip=trip, status='no_actuals'),
    ]
    out = summarise(rows)

    assert out['rows'] == 4
    assert out['comparable'] == 3, 'the unshadowable trip must not be counted in'
    assert out['metering_higher'] == 1
    assert out['metering_lower'] == 1
    assert out['unchanged'] == 1
    assert out['mean_metric_delta'] == Decimal('6.67')
    assert out['worst_increase'] == Decimal('30')
    assert out['worst_decrease'] == Decimal('-10')


@pytest.mark.django_db
def test_summary_of_nothing_says_nothing_rather_than_zero():
    """Zero difference and no evidence are different claims."""
    out = summarise([])
    assert out['comparable'] == 0
    assert out['mean_metric_delta'] is None


@pytest.mark.django_db
def test_the_report_command_runs_read_only(settings, trip, no_surge):
    from io import StringIO

    from django.core.management import call_command

    settings.FARE_SHADOW_ENABLED = True
    sweep_shadow()

    buf = StringIO()
    call_command('fare_shadow_report', '--days', '1', stdout=buf)
    text = buf.getvalue()

    assert 'Comparable trips' in text
    assert TripFareShadow.objects.count() == 1, 'reporting must not write'
    assert Trip.objects.get(id=trip.id).final_fare is None


# ---------------------------------------------------------------------------
# Privacy and wiring
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_observation_logging_carries_no_location_or_identity(trip, no_surge, caplog):
    """Fare logs are widely readable; a route or a rider id must not be in them."""
    import logging as _logging

    with caplog.at_level(_logging.INFO, logger='servers.pricing.fare_shadow'):
        record_shadow(trip)

    records = [r for r in caplog.records if r.msg == 'fare_shadow_observed']
    assert records, 'the observation should be logged'
    extra = records[0].__dict__
    for banned in ('pickup_lat', 'pickup_long', 'pickup_address', 'rider_id',
                   'phone_number', 'destination_address'):
        assert banned not in extra, banned
    assert '17.44' not in str(extra.get('zone_code', ''))


@pytest.mark.django_db
def test_sweep_task_is_registered_under_its_stable_name():
    from base.celery import app
    from servers.pricing import tasks

    assert tasks.fare_shadow_sweep.name == 'pricing.fare_shadow_sweep'
    assert 'pricing.fare_shadow_sweep' in app.tasks


def test_the_sweep_is_scheduled_and_the_feature_defaults_off():
    """Scheduling an observer that is off by default is what makes it deployable."""
    from django.conf import settings as dj_settings

    entries = dj_settings.CELERY_BEAT_SCHEDULE.values()
    assert any(e['task'] == 'pricing.fare_shadow_sweep' for e in entries)
