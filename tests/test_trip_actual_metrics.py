"""Actual trip distance and duration, derived from the durable GPS trail.

OBSERVE ONLY. The platform has never known how far a trip actually went: the
fare is an up-front estimate and `final_fare` is never written. Before anything
can meter a rider, the inputs to metering have to exist and be trustworthy.
These tests are what makes them trustworthy.

Two properties get the most attention, because they are the ones that would
quietly corrupt a future fare:

* **The journey boundary.** Distance and time must cover the passenger's
  journey, not the driver's drive to the pickup. The boundary is
  `started_at -> completed_at`, which the lifecycle proves: `in_progress` is the
  only transition gated on the rider's pickup OTP. A test asserts approach
  points are excluded, because getting this wrong overcharges every rider by the
  length of the driver's approach.
* **Nothing here touches money.** `record_actuals` writes at most two columns.
  A test proves a concurrent `final_fare` write survives it, which is the
  guarantee that keeps this phase observational.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.actual_metrics import (
    compute_metrics, journey_points, journey_window, record_actuals,
)
from servers.ride.models import Trip, TripLocationPoint, TripStatus

User = get_user_model()

LAT = Decimal('17.4450000')
LNG = Decimal('78.3800000')

# One thousandth of a degree of latitude is ~111.19 m anywhere on Earth. Every
# expected distance below is built from that, so the numbers are checkable by
# hand rather than copied from a previous run.
STEP_DEG = Decimal('0.001')
STEP_M = 111.19


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def rider(db):
    return User.objects.create_user(phone_number='+919700000101', role='rider')


@pytest.fixture
def driver(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    u = User.objects.create_user(phone_number='+919800000101', role='driver')
    d = Driver.objects.create(user_id=u, approved=True)
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vt, vehicle_number='TS09GP0101')
    return d


@pytest.fixture
def base_time():
    return timezone.now() - timedelta(hours=1)


@pytest.fixture
def trip(db, rider, driver, base_time):
    """A completed trip: accepted, then a 10-minute passenger journey."""
    t = Trip.objects.create(
        user_id=rider, status_id=_status('completed'),
        pickup_lat=LAT, pickup_long=LNG,
        destination_lat=Decimal('17.4500000'), destination_long=Decimal('78.4000000'),
        pickup_address='P', destination_address='D',
        estimated_fare=Decimal('150.00'),
    )
    t.driver_id = driver
    t.accepted_at = base_time
    t.reached_at = base_time + timedelta(minutes=4)
    t.started_at = base_time + timedelta(minutes=5)
    t.completed_at = base_time + timedelta(minutes=15)
    t.save(update_fields=['driver_id', 'accepted_at', 'reached_at',
                          'started_at', 'completed_at'])
    return t


_seq = [0]


def _point(trip, driver, at, lat=None, lng=LNG):
    """Store one trail point. Distinct source_event_id per row by construction."""
    _seq[0] += 1
    return TripLocationPoint.objects.create(
        trip=trip, driver_id=driver.id,
        latitude=LAT if lat is None else Decimal(str(lat)),
        longitude=Decimal(str(lng)),
        recorded_at=at, sequence=_seq[0],
        source=TripLocationPoint.SOURCE_DRIVER_WS,
        source_event_id='test-{}'.format(_seq[0]),
    )


def _straight_line(trip, driver, start_at, count, interval_s=30):
    """`count` points heading north, one STEP_DEG apart, `interval_s` apart."""
    out = []
    for i in range(count):
        out.append(_point(
            trip, driver,
            at=start_at + timedelta(seconds=interval_s * i),
            lat=LAT + STEP_DEG * i,
        ))
    return out


# ---------------------------------------------------------------------------
# The journey boundary -- the property that protects riders from overcharging
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_journey_window_is_started_to_completed(trip):
    """Not accepted_at. The driver's approach to the pickup is not the journey."""
    start, end = journey_window(trip)
    assert start == trip.started_at
    assert end == trip.completed_at
    assert start != trip.accepted_at
    assert start != trip.reached_at


@pytest.mark.django_db
def test_approach_points_are_excluded_from_distance(trip, driver, base_time):
    """Points from before the rider boarded must not be billable.

    This is the single most expensive thing to get wrong: if approach points
    counted, every rider would pay for the driver's drive to them. The trail
    here contains a long approach and a short journey, and only the journey may
    appear.
    """
    # 20 points of approach: accepted_at -> started_at, far to the south.
    for i in range(20):
        _point(trip, driver, at=base_time + timedelta(seconds=10 * i),
               lat=Decimal('17.4000000') + STEP_DEG * i)

    # 4 points of actual journey.
    _straight_line(trip, driver, trip.started_at, 4)

    assert TripLocationPoint.objects.filter(trip=trip).count() == 24
    assert len(journey_points(trip)) == 4, 'only in-journey points may be considered'

    result = compute_metrics(trip)
    # 3 segments of ~111.19 m.
    expected_km = (3 * STEP_M) / 1000.0
    assert abs(float(result['distance_km']) - expected_km) < 0.01, result


@pytest.mark.django_db
def test_no_journey_window_when_the_rider_never_boarded(trip):
    """A trip with no started_at has no billable journey, and we say so."""
    trip.started_at = None
    trip.save(update_fields=['started_at'])

    assert journey_window(trip) == (None, None)
    assert journey_points(trip) == []

    result = compute_metrics(trip)
    assert result['ok'] is False
    assert result['reason'] == 'no_journey_window'
    assert result['distance_km'] is None
    assert result['duration_min'] is None


# ---------------------------------------------------------------------------
# Duration: from lifecycle timestamps, deliberately not from GPS
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_duration_comes_from_timestamps_not_from_gps_coverage(trip, driver):
    """A patchy trail must not shorten the trip.

    Summing GPS intervals would under-report duration exactly when coverage is
    worst -- the opposite of what a billing input should do. Here the trail
    covers one minute of a ten-minute journey; duration must still be 10.
    """
    _straight_line(trip, driver, trip.started_at, 3, interval_s=30)

    result = compute_metrics(trip)
    assert result['duration_min'] == Decimal('10.00')
    # ...and the poor coverage is reported rather than hidden.
    assert result['coverage_ratio'] < Decimal('0.20'), result


@pytest.mark.django_db
def test_good_coverage_is_reported_as_such(trip, driver):
    """Coverage exists so a fare policy can tell 10% evidence from 95%."""
    _straight_line(trip, driver, trip.started_at, 20, interval_s=30)  # 9.5 min
    result = compute_metrics(trip)
    assert result['coverage_ratio'] >= Decimal('0.90'), result
    assert result['gap_exceeds_threshold'] is False


# ---------------------------------------------------------------------------
# Distance: the sum, and the things excluded from it
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_distance_is_the_sum_of_segments(trip, driver):
    _straight_line(trip, driver, trip.started_at, 11)  # 10 segments
    result = compute_metrics(trip)
    expected_km = (10 * STEP_M) / 1000.0
    assert abs(float(result['distance_km']) - expected_km) < 0.02, result
    assert result['points'] == 11
    assert result['rejected_segments'] == 0


@pytest.mark.django_db
def test_an_impossible_jump_is_excluded_rather_than_summed(trip, driver):
    """One bad fix must not be allowed to dominate a trip's distance.

    A cold-start fix landing in the wrong cell, or two devices sharing a driver
    id, produces a segment implying hundreds of km/h. Summed naively it would
    add tens of kilometres to a 1 km trip.
    """
    at = trip.started_at
    _point(trip, driver, at=at, lat=LAT)
    _point(trip, driver, at=at + timedelta(seconds=30), lat=LAT + STEP_DEG)
    # ~55 km in 30 seconds == ~6600 km/h. Not a car.
    _point(trip, driver, at=at + timedelta(seconds=60), lat=LAT + Decimal('0.5'))

    result = compute_metrics(trip)
    assert result['rejected_segments'] == 1, result
    # Only the one good segment survives; the 55 km jump is gone.
    assert abs(float(result['distance_km']) - STEP_M / 1000.0) < 0.01, result


@pytest.mark.django_db
def test_unknown_distance_is_not_zero_distance(trip, driver):
    """`None` and `0.00` are different facts and must stay different.

    No trail at all means we do not know how far the trip went. A stationary
    trip means we know, and the answer is zero. Collapsing them would let a
    metered fare silently bill a real journey as nothing, or vice versa.
    """
    # Nothing recorded: unknown.
    empty = compute_metrics(trip)
    assert empty['ok'] is False
    assert empty['reason'] == 'insufficient_points'
    assert empty['distance_km'] is None
    assert empty['duration_min'] == Decimal('10.00'), 'duration is still known'

    # A single point still cannot describe a path.
    _point(trip, driver, at=trip.started_at)
    assert compute_metrics(trip)['distance_km'] is None

    # Two points at the same place: genuinely zero.
    _point(trip, driver, at=trip.started_at + timedelta(seconds=30))
    stationary = compute_metrics(trip)
    assert stationary['ok'] is True
    assert stationary['distance_km'] == Decimal('0.00')


@pytest.mark.django_db
def test_a_long_gap_is_surfaced(trip, driver):
    """A straight line across a 5-minute hole under-reads the real route."""
    at = trip.started_at
    _point(trip, driver, at=at, lat=LAT)
    _point(trip, driver, at=at + timedelta(seconds=30), lat=LAT + STEP_DEG)
    _point(trip, driver, at=at + timedelta(minutes=6), lat=LAT + STEP_DEG * 2)

    result = compute_metrics(trip)
    assert result['max_gap_seconds'] > Decimal('300')
    assert result['gap_exceeds_threshold'] is True


@pytest.mark.django_db
def test_result_is_deterministic_when_timestamps_tie(trip, driver):
    """Two points can share a timestamp when a device flushes a buffer.

    Ordering has to be stable anyway, or the same trip yields different
    distances on different runs and no dispute can ever be settled.
    """
    at = trip.started_at
    _point(trip, driver, at=at, lat=LAT)
    _point(trip, driver, at=at + timedelta(seconds=30), lat=LAT + STEP_DEG)
    _point(trip, driver, at=at + timedelta(seconds=30), lat=LAT + STEP_DEG * 2)
    _point(trip, driver, at=at + timedelta(seconds=60), lat=LAT + STEP_DEG * 3)

    first = compute_metrics(trip)['distance_km']
    for _ in range(3):
        assert compute_metrics(trip)['distance_km'] == first


# ---------------------------------------------------------------------------
# OBSERVE ONLY -- the guarantee that this phase cannot change a fare
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_record_actuals_writes_only_the_two_actual_columns(trip, driver):
    """A concurrent money write must survive recording actuals.

    `record_actuals` uses a narrow `update_fields`. To prove that is real rather
    than incidental, `final_fare` is changed in the database behind the in-memory
    instance's back. A full save would clobber it back to NULL; a narrow save
    cannot touch it.
    """
    _straight_line(trip, driver, trip.started_at, 5)

    Trip.objects.filter(id=trip.id).update(
        final_fare=Decimal('199.00'), estimated_fare=Decimal('150.00'),
    )
    assert trip.final_fare is None, 'the in-memory instance is deliberately stale'

    result = record_actuals(trip)
    assert result['written'] is True

    fresh = Trip.objects.get(id=trip.id)
    assert fresh.final_fare == Decimal('199.00'), 'money must be untouched'
    assert fresh.estimated_fare == Decimal('150.00')
    assert fresh.actual_distance_km is not None
    assert fresh.actual_duration_min == Decimal('10.00')


@pytest.mark.django_db
def test_recording_is_idempotent_under_redelivery(trip, driver):
    """Celery is at-least-once, so a second run must be harmless."""
    _straight_line(trip, driver, trip.started_at, 5)

    first = record_actuals(trip)
    assert first['written'] is True
    distance = Trip.objects.get(id=trip.id).actual_distance_km

    second = record_actuals(Trip.objects.get(id=trip.id))
    assert second['written'] is False
    assert second['skipped'] == 'already_populated'
    assert Trip.objects.get(id=trip.id).actual_distance_km == distance


@pytest.mark.django_db
def test_force_allows_a_deliberate_recomputation(trip, driver):
    """Backfilling a trip whose trail arrived late needs an explicit opt-in."""
    _straight_line(trip, driver, trip.started_at, 3)
    record_actuals(trip)
    short = Trip.objects.get(id=trip.id).actual_distance_km

    # More of the trail drains in afterwards.
    _straight_line(trip, driver, trip.started_at + timedelta(minutes=2), 6)

    again = record_actuals(Trip.objects.get(id=trip.id), force=True)
    assert again['written'] is True
    assert Trip.objects.get(id=trip.id).actual_distance_km > short


@pytest.mark.django_db
def test_a_trip_with_no_trail_is_left_alone_rather_than_zeroed(trip):
    """Absence of evidence must not be recorded as a zero-kilometre trip."""
    result = record_actuals(trip)
    fresh = Trip.objects.get(id=trip.id)
    assert fresh.actual_distance_km is None, 'unknown distance stays unknown'
    assert fresh.actual_duration_min == Decimal('10.00')
    assert 'actual_distance_km' not in result['fields']


# ---------------------------------------------------------------------------
# The Celery adapter and its wiring
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_task_is_registered_under_its_stable_name():
    """The completion path enqueues by name; a rename would silently no-op.

    The import mirrors what the worker does through autodiscovery -- the name is
    what matters, not who imported the module.
    """
    from base.celery import app
    from servers.ride import tasks

    assert tasks.compute_trip_actuals.name == 'ride.compute_trip_actuals'
    assert 'ride.compute_trip_actuals' in app.tasks


@pytest.mark.django_db
def test_task_handles_a_deleted_trip_without_retrying_forever(trip):
    from servers.ride.tasks import compute_trip_actuals

    trip_id = trip.id
    trip.delete()
    out = compute_trip_actuals.run(trip_id)
    assert out == {'ok': False, 'reason': 'trip_missing'}


@pytest.mark.django_db
def test_task_returns_a_pii_free_summary(trip, driver):
    """The return value lands in Celery's logs, so it must carry no location."""
    from servers.ride.tasks import compute_trip_actuals

    _straight_line(trip, driver, trip.started_at, 5)
    out = compute_trip_actuals.run(trip.id)

    assert out['ok'] is True
    flat = str(out)
    assert '17.44' not in flat and '78.38' not in flat, flat
    for key in out:
        assert 'lat' not in key and 'lng' not in key and 'address' not in key


def test_completion_enqueues_the_task_out_of_band_and_delayed():
    """Behavioural, not textual: read what the completion code actually calls.

    The computation must be (a) enqueued on commit, so a slow or failing
    calculation cannot affect trip completion, and (b) delayed, because the
    trail drains on a schedule and the last points are not stored yet when the
    trip completes.
    """
    import types

    from servers.consumers import TripStatusConsumer

    fn = TripStatusConsumer.__dict__['_update_trip_status']
    code = getattr(fn, 'func', fn).__code__

    # The enqueue happens inside an on_commit lambda, so the call lives in a
    # nested code object. Walk them all rather than only the top frame.
    names, consts = set(), set()

    def walk(c):
        names.update(c.co_names)
        for const in c.co_consts:
            if isinstance(const, types.CodeType):
                walk(const)
            elif isinstance(const, str):
                consts.add(const)

    walk(code)

    assert 'compute_trip_actuals' in names
    assert 'on_commit' in names
    assert 'apply_async' in names, 'a countdown requires apply_async, not delay'
    assert 'TRIP_ACTUALS_DELAY_SECONDS' in consts
