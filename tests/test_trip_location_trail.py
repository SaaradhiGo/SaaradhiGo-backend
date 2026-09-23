"""Durable GPS trail: schema ownership, sampling policy, and privacy.

Driver location was Redis-only, so a completed trip left no route behind and
the platform could not compute actual distance, reconcile a fare, replay a
disputed route, or support an SOS or insurance investigation.

These tests cover the substrate landed here: the model and the sampling policy.
The stream-drain writer is deliberately NOT wired into the live location path
yet — see the branch notes — so there is no test here asserting that a live
ping produces a row. What is tested is every rule that writer will obey.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.location_trail import (
    MAX_ACCURACY_METRES, MIN_DISTANCE_METRES, MIN_INTERVAL_SECONDS,
    Candidate, Rejected, accuracy_is_acceptable, haversine_metres,
    parse_coordinate, sample, should_keep, trip_is_collecting,
)
from servers.ride.models import Trip, TripLocationPoint, TripStatus

User = get_user_model()

LAT = Decimal('17.4450000')
LNG = Decimal('78.3800000')


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def rider(db):
    return User.objects.create_user(phone_number='+919700000001', role='rider')


@pytest.fixture
def driver(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    u = User.objects.create_user(phone_number='+919800000001', role='driver')
    d = Driver.objects.create(user_id=u, approved=True)
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vt, vehicle_number='TS09GP0001')
    return d


@pytest.fixture
def trip(db, rider, driver):
    t = Trip.objects.create(
        user_id=rider, status_id=_status('in_progress'),
        pickup_lat=LAT, pickup_long=LNG,
        destination_lat=Decimal('17.4500000'), destination_long=Decimal('78.4000000'),
        pickup_address='P', destination_address='D',
    )
    t.driver_id = driver
    t.save(update_fields=['driver_id'])
    return t


def _cand(lat=LAT, lng=LNG, at=None, accuracy=None, final=False):
    return Candidate(
        latitude=Decimal(str(lat)), longitude=Decimal(str(lng)),
        recorded_at=at or timezone.now(), accuracy_m=accuracy, is_final=final,
    )


# ---------------------------------------------------------------------------
# Coordinate validation — malformed input must never reach the table
# ---------------------------------------------------------------------------

def test_valid_coordinates_parse_to_decimal():
    lat, lon = parse_coordinate('17.4450000', '78.3800000')
    assert (lat, lon) == (Decimal('17.4450000'), Decimal('78.3800000'))
    assert isinstance(lat, Decimal), 'floats would drift against Trip.pickup_lat'


@pytest.mark.parametrize('lat,lon,reason', [
    ('abc', '78.38', 'unparseable_coordinate'),
    (None, '78.38', 'unparseable_coordinate'),
    ('91.0', '78.38', 'latitude_out_of_range'),
    ('-91.0', '78.38', 'latitude_out_of_range'),
    ('17.44', '181.0', 'longitude_out_of_range'),
    ('17.44', '-181.0', 'longitude_out_of_range'),
    ('0', '0', 'null_island'),
])
def test_malformed_coordinates_are_rejected(lat, lon, reason):
    with pytest.raises(Rejected) as exc:
        parse_coordinate(lat, lon)
    assert exc.value.reason == reason


def test_null_island_is_treated_as_no_fix():
    """Exactly 0,0 is the classic 'no GPS fix' sentinel, not the Gulf of Guinea."""
    with pytest.raises(Rejected):
        parse_coordinate('0.0', '0.0')
    # A genuine near-zero coordinate is still fine.
    assert parse_coordinate('0.0001', '0.0001')


# ---------------------------------------------------------------------------
# Accuracy filtering
# ---------------------------------------------------------------------------

def test_poor_accuracy_is_dropped():
    assert accuracy_is_acceptable(MAX_ACCURACY_METRES - 1) is True
    assert accuracy_is_acceptable(MAX_ACCURACY_METRES + 1) is False
    keep, reason = should_keep(_cand(accuracy=500.0), last_kept=None)
    assert keep is False and reason == 'accuracy_too_poor'


def test_missing_accuracy_is_accepted():
    """Many Android builds omit accuracy; refusing those means no trail at all
    on those devices."""
    assert accuracy_is_acceptable(None) is True
    keep, _ = should_keep(_cand(accuracy=None), last_kept=None)
    assert keep is True


def test_garbage_accuracy_does_not_crash_the_sampler():
    assert accuracy_is_acceptable('not-a-number') is True


# ---------------------------------------------------------------------------
# Sampling: time and distance thresholds
# ---------------------------------------------------------------------------

def test_first_point_is_always_kept():
    keep, reason = should_keep(_cand(), last_kept=None)
    assert keep is True and reason == 'first_point'


def test_point_too_soon_after_the_last_is_dropped():
    t0 = timezone.now()
    first = _cand(at=t0)
    keep, reason = should_keep(_cand(at=t0 + timedelta(seconds=1)), last_kept=first)
    assert keep is False and reason == 'too_soon'


def test_stationary_driver_does_not_fill_the_table():
    """The case this policy exists for: idling at a pickup."""
    t0 = timezone.now()
    pings = [_cand(at=t0 + timedelta(seconds=10 * i)) for i in range(50)]
    kept, stats = sample(pings)
    assert stats['received'] == 50
    assert stats['kept'] == 1, 'a stationary driver must produce one point, not 50'
    assert stats['too_close'] == 49


def test_moving_driver_produces_a_trail():
    t0 = timezone.now()
    pings = []
    for i in range(10):
        # ~0.001 degree latitude is ~111m, comfortably over the threshold.
        pings.append(_cand(lat=Decimal('17.4450000') + Decimal('0.001') * i,
                           at=t0 + timedelta(seconds=10 * i)))
    kept, stats = sample(pings)
    assert stats['kept'] == 10, stats


def test_final_point_is_kept_regardless_of_thresholds():
    """Without this a short trip could reduce to one point and its distance
    would be unrecoverable."""
    t0 = timezone.now()
    first = _cand(at=t0)
    close_and_soon = _cand(at=t0 + timedelta(seconds=1), final=True)
    keep, reason = should_keep(close_and_soon, last_kept=first)
    assert keep is True and reason == 'final_point'


def test_sampler_respects_the_per_trip_cap():
    from servers.ride.location_trail import MAX_POINTS_PER_TRIP
    t0 = timezone.now()
    pings = [
        _cand(lat=Decimal('17.0000000') + Decimal('0.001') * i,
              at=t0 + timedelta(seconds=10 * i))
        for i in range(MAX_POINTS_PER_TRIP + 25)
    ]
    kept, stats = sample(pings)
    assert stats['kept'] == MAX_POINTS_PER_TRIP
    assert stats['over_trip_cap'] == 25


def test_backwards_device_clock_does_not_drop_a_real_move():
    """A phone whose clock jumps backwards is unreliable for ordering, but the
    position may be genuine, so distance still decides."""
    t0 = timezone.now()
    first = _cand(at=t0)
    moved_but_earlier = _cand(lat=Decimal('17.4550000'), at=t0 - timedelta(seconds=30))
    keep, _ = should_keep(moved_but_earlier, last_kept=first)
    assert keep is True


def test_thresholds_are_the_documented_values():
    """Negative control on the policy constants themselves."""
    assert MIN_INTERVAL_SECONDS == 5.0
    assert MIN_DISTANCE_METRES == 25.0
    assert MAX_ACCURACY_METRES == 50.0


def test_haversine_is_approximately_correct():
    # One degree of latitude is ~111km.
    d = haversine_metres(17.0, 78.0, 18.0, 78.0)
    assert 110_000 < d < 112_000, d
    assert haversine_metres(17.0, 78.0, 17.0, 78.0) == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Lifecycle: collect only while the trip is genuinely active, per PostgreSQL
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('status_code,expected', [
    ('requested', False),      # no driver committed yet
    ('accepted', True),
    ('reached', True),
    ('in_progress', True),
    ('completed', False),      # collection must stop at terminal
    ('cancelled', False),
])
def test_collection_window_follows_durable_trip_status(trip, status_code, expected):
    trip.status_id = _status(status_code)
    trip.save(update_fields=['status_id'])
    trip.refresh_from_db()
    assert trip_is_collecting(trip) is expected


@pytest.mark.django_db
def test_no_collection_without_an_assigned_driver(rider, db):
    """A trip with no driver has no driver track to record."""
    t = Trip.objects.create(
        user_id=rider, status_id=_status('requested'),
        pickup_lat=LAT, pickup_long=LNG,
        destination_lat=Decimal('17.45'), destination_long=Decimal('78.40'),
    )
    assert trip_is_collecting(t) is False


@pytest.mark.django_db
def test_collection_window_reuses_the_driver_active_status_set(trip):
    """Not a second definition of 'active'.

    If these drifted apart, a trip could be collecting a trail while the driver
    was considered free, or vice versa.
    """
    from servers.ride.models import DRIVER_ACTIVE_TRIP_STATUSES
    for code in DRIVER_ACTIVE_TRIP_STATUSES:
        trip.status_id = _status(code)
        trip.save(update_fields=['status_id'])
        trip.refresh_from_db()
        assert trip_is_collecting(trip) is True, code


# ---------------------------------------------------------------------------
# Model: ownership, ordering, duplicate protection
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_point_is_owned_by_both_trip_and_driver(trip, driver):
    p = TripLocationPoint.objects.create(
        trip=trip, driver=driver, latitude=LAT, longitude=LNG,
        recorded_at=timezone.now(), sequence=1,
    )
    assert p.trip_id == trip.id
    assert p.driver_id == driver.id
    assert p.received_at is not None, 'server clock must be stamped automatically'


@pytest.mark.django_db
def test_device_and_server_timestamps_are_both_retained(trip, driver):
    """They are kept separately precisely because they disagree: a phone clock
    can be minutes off, and anything financial or legal must use ours."""
    device_time = timezone.now() - timedelta(minutes=7)
    p = TripLocationPoint.objects.create(
        trip=trip, driver=driver, latitude=LAT, longitude=LNG,
        recorded_at=device_time, sequence=1,
    )
    p.refresh_from_db()
    assert p.recorded_at == device_time
    assert p.received_at > device_time


@pytest.mark.django_db
def test_exact_duplicate_point_is_refused_by_the_database(trip, driver):
    at = timezone.now()
    TripLocationPoint.objects.create(
        trip=trip, driver=driver, latitude=LAT, longitude=LNG,
        recorded_at=at, sequence=1,
    )
    with pytest.raises(IntegrityError):
        TripLocationPoint.objects.create(
            trip=trip, driver=driver, latitude=LAT, longitude=LNG,
            recorded_at=at, sequence=1,
        )


@pytest.mark.django_db
def test_trail_is_deleted_with_its_trip(trip, driver):
    TripLocationPoint.objects.create(
        trip=trip, driver=driver, latitude=LAT, longitude=LNG,
        recorded_at=timezone.now(), sequence=1,
    )
    trip_id = trip.id
    trip.delete()
    assert TripLocationPoint.objects.filter(trip_id=trip_id).count() == 0


@pytest.mark.django_db
def test_route_can_be_replayed_in_order(trip, driver):
    t0 = timezone.now()
    for i in range(5):
        TripLocationPoint.objects.create(
            trip=trip, driver=driver,
            latitude=Decimal('17.4450000') + Decimal('0.001') * i, longitude=LNG,
            recorded_at=t0 + timedelta(seconds=10 * i), sequence=i,
        )
    seq = list(
        TripLocationPoint.objects
        .filter(trip=trip).order_by('recorded_at').values_list('sequence', flat=True)
    )
    assert seq == [0, 1, 2, 3, 4], 'route replay must be stably ordered'


@pytest.mark.django_db
def test_indexes_support_the_intended_access_patterns():
    names = {i.name for i in TripLocationPoint._meta.indexes}
    assert 'triploc_trip_time_idx' in names, 'route replay / distance'
    assert 'triploc_driver_time_idx' in names, 'SOS / insurance: driver at time T'
    assert 'triploc_received_idx' in names, 'retention sweep by age'


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_no_rider_location_is_modelled():
    """Only the assigned driver's track is stored. There is deliberately no
    rider FK and no rider coordinate anywhere on this model."""
    fields = {f.name for f in TripLocationPoint._meta.get_fields()}
    assert 'rider' not in fields
    assert not any('rider' in f for f in fields)
    assert {'trip', 'driver', 'latitude', 'longitude'} <= fields


def test_module_does_not_log_coordinates():
    """Positions must never reach a log line. The redaction filter also covers
    lat/lng-shaped keys, but the rule here is simply not to log them."""
    import inspect

    from servers.ride import location_trail

    src = inspect.getsource(location_trail)
    for call in ('logger.info', 'logger.warning', 'logger.error', 'logger.debug'):
        for line in src.splitlines():
            if call in line:
                assert 'latitude' not in line and 'longitude' not in line, line


def test_coordinate_keys_are_redacted_by_the_logging_filter():
    """Belt and braces: even if someone logs a point later, the filter drops it."""
    import json
    import logging

    from base.logging_filters import JSONFormatter, PIIRedactionFilter

    rec = logging.LogRecord('t', logging.INFO, __file__, 1, 'trail', (), None)
    rec.latitude = '17.4450000'
    rec.longitude = '78.3800000'
    rec.trip_id = 7
    PIIRedactionFilter().filter(rec)
    out = json.loads(JSONFormatter().format(rec))
    assert out['latitude'] == '***'
    assert out['longitude'] == '***'
    assert out['trip_id'] == 7, 'trip_id is not PII and must survive'
