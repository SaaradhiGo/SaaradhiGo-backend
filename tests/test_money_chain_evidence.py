"""The evidence chain, end to end, against real infrastructure.

The stated goal of this work is a trustworthy chain:

    trip lifecycle -> durable location evidence -> actual metrics
        -> canonical pricing -> (final fare) -> payment -> commission

Each link has unit tests of its own. Those prove the rules. This file proves the
**joins**, which is where chains actually break: a ping written to one Redis
database and read from another, a boundary computed from the wrong timestamp, a
shadow quote that never sees the points because the drain had not run yet. A
silent wrong-database defect in the trail writer was caught earlier by exactly
this kind of test and by nothing else.

So these run against a real Redis stream with a real consumer group and, when
marked, a real PostgreSQL. They start from a driver's raw GPS pings and end at a
recorded comparison of what the rider was quoted against what metering would have
charged -- and they assert, at the end of that chain, that **nothing monetary
moved**. That last assertion is the point of the whole file: the chain is proven
before any of it is allowed to touch a fare.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

import servers.redis_client as rc
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.pricing.models import TripFareShadow
from servers.ride.models import Trip, TripLocationPoint, TripStatus

User = get_user_model()

# A short run along a single meridian: 0.001 deg of latitude is ~111.19 m, so
# ten steps is ~1.11 km and the expected distance is checkable by hand.
LAT0 = Decimal('17.4450000')
LNG = Decimal('78.3800000')
STEP = Decimal('0.001')
STEP_M = 111.19


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def vehicle_type(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return vt


@pytest.fixture
def rider(db):
    return User.objects.create_user(phone_number='+919700000401', role='rider')


@pytest.fixture
def driver(db, vehicle_type):
    u = User.objects.create_user(phone_number='+919800000401', role='driver')
    d = Driver.objects.create(user_id=u, approved=True)
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vehicle_type,
                           vehicle_number='TS09GP0401')
    return d


@pytest.fixture
def real_stream():
    """A clean consumer group on the real location stream (Redis db 3)."""
    client = rc._stream_client()
    if client is None:
        pytest.skip('Redis not available')
    try:
        client.delete(rc.LOCATION_STREAM)
    except Exception:  # noqa: BLE001
        pytest.skip('Redis not usable')
    rc.ensure_location_stream_group()
    yield client
    try:
        client.delete(rc.LOCATION_STREAM)
    except Exception:  # noqa: BLE001
        pass


def _ping(driver_id, lat, ms):
    """One driver location ping, at an explicit stream time."""
    return rc._stream_client().xadd(
        rc.LOCATION_STREAM,
        {'driver_id': str(driver_id), 'lat': str(lat), 'lng': str(LNG)},
        id=f'{ms}-0', maxlen=100000,
    )


@pytest.fixture
def in_progress_trip(db, rider, driver, vehicle_type):
    """A trip with the rider aboard: OTP verified, journey under way."""
    t = Trip.objects.create(
        user_id=rider, status_id=_status('in_progress'),
        requested_vehicle_type=vehicle_type,
        pickup_lat=LAT0, pickup_long=LNG,
        destination_lat=LAT0 + STEP * 10, destination_long=LNG,
        pickup_address='P', destination_address='D',
        estimated_distance_km=Decimal('1.00'),
        estimated_duration_min=Decimal('5.00'),
        estimated_fare=Decimal('80.00'),
        payment_method='online',
    )
    t.driver_id = driver
    t.accepted_at = timezone.now() - timedelta(minutes=20)
    t.reached_at = timezone.now() - timedelta(minutes=16)
    t.started_at = timezone.now() - timedelta(minutes=15)
    t.save(update_fields=['driver_id', 'accepted_at', 'reached_at', 'started_at'])
    return t


def _drive_and_complete(trip, driver, steps=11, spacing_s=30):
    """Emit pings across the journey window, then complete the trip.

    Pings are stamped inside `started_at .. now` so they fall in the journey
    window the metrics module derives from the lifecycle -- the same window a
    real driver's pings would land in.
    """
    base_ms = int(trip.started_at.timestamp() * 1000)
    for i in range(steps):
        _ping(driver.id, LAT0 + STEP * i, base_ms + i * spacing_s * 1000)

    trip.status_id = _status('completed')
    trip.completed_at = trip.started_at + timedelta(seconds=(steps - 1) * spacing_s)
    trip.save(update_fields=['status_id', 'completed_at'])
    return trip


# ---------------------------------------------------------------------------
# The full chain
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_pings_become_a_measured_journey_and_a_recorded_comparison(
    settings, real_stream, in_progress_trip, driver,
):
    """Raw GPS pings -> durable rows -> actuals -> shadow comparison.

    Every step here is the real code path: the real Redis stream and consumer
    group, the real drain task logic, the real metrics derivation and the real
    canonical fare function. Nothing in between is mocked, because the joins are
    what this test exists to check.
    """
    from servers.pricing.fare_shadow import record_shadow
    from servers.ride.actual_metrics import record_actuals
    from servers.ride.location_trail import drain_location_stream

    settings.GPS_TRAIL_ENABLED = True
    settings.FARE_SHADOW_ENABLED = True

    trip = _drive_and_complete(in_progress_trip, driver, steps=11, spacing_s=30)

    # Link 1: the pings the driver's app sent are still only in Redis.
    assert TripLocationPoint.objects.filter(trip=trip).count() == 0
    assert rc._stream_client().xlen(rc.LOCATION_STREAM) == 11

    # Link 2: the drain turns them into durable evidence. The trip is already
    # completed, which is realistic -- the drain runs on a schedule and the last
    # points always land after the trip ends.
    drained = drain_location_stream(consumer='chain-test')
    assert drained['received'] == 11, drained
    stored = TripLocationPoint.objects.filter(trip=trip).count()
    assert stored >= 2, drained

    # Link 3: actual metrics, derived from those rows and the lifecycle.
    metrics = record_actuals(trip)
    assert metrics['ok'] is True, metrics
    trip.refresh_from_db()
    assert trip.actual_duration_min == Decimal('5.00'), 'started_at -> completed_at'

    # ~1.11 km driven. The sampler drops points that are too close together, so
    # the measured distance is at or below the true path length and never above
    # it -- under-reading is the safe direction for a billing input.
    expected_km = Decimal(str(round((10 * STEP_M) / 1000.0, 2)))
    assert trip.actual_distance_km is not None
    assert Decimal('0.20') <= trip.actual_distance_km <= expected_km + Decimal('0.05'), (
        trip.actual_distance_km, expected_km, drained,
    )

    # Link 4: the shadow prices the measured journey through the canonical fare
    # function and records the comparison.
    out = record_shadow(trip)
    assert out['written'] is True
    row = TripFareShadow.objects.get(trip=trip)
    assert row.status == 'computed', row.status
    assert row.shadow_actual is not None
    assert row.actual_distance_km == trip.actual_distance_km
    # The shadow counts the points inside the billable journey window, which can
    # be one fewer than the rows stored: a stream id is millisecond-truncated, so
    # the ping emitted at the instant of `started_at` can land a fraction before
    # it. Excluding it is correct -- the window is the authority, not the ping.
    from servers.ride.actual_metrics import journey_points
    assert row.trail_points == len(journey_points(trip))
    assert stored - 1 <= row.trail_points <= stored
    assert row.coverage_ratio > Decimal('0.50'), 'a full trail should read as covered'

    # And the whole point: after all of that, no money has moved.
    trip.refresh_from_db()
    assert trip.final_fare is None, 'the chain is observation only'
    assert trip.estimated_fare == Decimal('80.00'), 'the quote is untouched'
    assert trip.payment_status in (None, '', 'pending')
    assert not trip.payments.exists()
    from servers.rider.models import WalletTransaction
    assert not WalletTransaction.objects.filter(user_id=driver.user_id).exists()


@pytest.mark.django_db(transaction=True)
def test_the_drivers_approach_never_reaches_the_measured_distance(
    settings, real_stream, in_progress_trip, driver,
):
    """Pings from before the rider boarded must not survive the chain.

    This is the expensive failure mode: if approach pings were billed, every
    rider would pay for the driver's drive to them. The unit tests assert the
    window; this asserts it end to end, through the real writer, where the
    pings arrive interleaved rather than pre-sorted.
    """
    from servers.ride.actual_metrics import journey_points
    from servers.ride.location_trail import drain_location_stream

    settings.GPS_TRAIL_ENABLED = True

    trip = in_progress_trip
    # The approach: well before started_at, and far to the south.
    approach_base = int((trip.accepted_at).timestamp() * 1000)
    for i in range(6):
        _ping(driver.id, Decimal('17.4000000') + STEP * i, approach_base + i * 30_000)

    _drive_and_complete(trip, driver, steps=6, spacing_s=30)

    drain_location_stream(consumer='chain-test')

    all_points = TripLocationPoint.objects.filter(trip=trip).count()
    in_journey = journey_points(trip)
    assert all_points > len(in_journey), 'the approach was stored but must not be billed'
    for point in in_journey:
        assert point.recorded_at >= trip.started_at
        assert point.recorded_at <= trip.completed_at


@pytest.mark.django_db(transaction=True)
def test_a_trip_with_no_telemetry_reaches_the_end_of_the_chain_honestly(
    settings, real_stream, in_progress_trip, driver,
):
    """No pings at all: the chain must say "unknown", not "zero".

    A metered fare built on a silent zero would bill a real journey as nothing.
    The shadow records the trip with a reason so the analysis keeps an honest
    denominator instead of quietly shrinking its sample.
    """
    from servers.pricing.fare_shadow import record_shadow
    from servers.ride.actual_metrics import record_actuals
    from servers.ride.location_trail import drain_location_stream

    settings.GPS_TRAIL_ENABLED = True
    settings.FARE_SHADOW_ENABLED = True

    trip = in_progress_trip
    trip.status_id = _status('completed')
    trip.completed_at = trip.started_at + timedelta(minutes=5)
    trip.save(update_fields=['status_id', 'completed_at'])

    assert drain_location_stream(consumer='chain-test')['received'] == 0

    record_actuals(trip)
    trip.refresh_from_db()
    assert trip.actual_distance_km is None, 'unknown distance must stay unknown'
    assert trip.actual_duration_min == Decimal('5.00'), 'duration is still known'

    record_shadow(trip)
    row = TripFareShadow.objects.get(trip=trip)
    assert row.status == 'no_actuals'
    assert row.metric_delta is None
    assert trip.final_fare is None


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_the_chain_holds_on_real_postgresql(settings, real_stream, in_progress_trip,
                                            driver):
    """The same chain against PostgreSQL, where the constraints are real.

    SQLite does not enforce the trail's unique constraint the way PostgreSQL
    does, and the idempotency of the writer depends on it: a redelivered stream
    entry must be dropped by the database, not by application luck. Running the
    drain twice here proves that on the engine production uses.
    """
    from servers.ride.location_trail import drain_location_stream

    settings.GPS_TRAIL_ENABLED = True

    trip = _drive_and_complete(in_progress_trip, driver, steps=8, spacing_s=30)

    events = rc.read_location_events(count=50, consumer='crashy')
    assert len(events) == 8

    from servers.ride.location_trail import persist_location_events
    persist_location_events(events)
    first_count = TripLocationPoint.objects.filter(trip=trip).count()
    assert first_count >= 2

    # Redelivery of the very same entries, as happens when a worker dies before
    # acknowledging. The unique constraint must absorb it.
    persist_location_events(events)
    assert TripLocationPoint.objects.filter(trip=trip).count() == first_count

    rc.ack_location_events([e[0] for e in events])
    assert drain_location_stream(consumer='chain-test')['received'] == 0
