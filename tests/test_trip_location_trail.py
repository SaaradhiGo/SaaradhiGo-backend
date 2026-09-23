"""Durable GPS trail: schema ownership, sampling policy, and privacy.

Driver location was Redis-only, so a completed trip left no route behind and
the platform could not compute actual distance, reconcile a fare, replay a
disputed route, or support an SOS or insurance investigation.

These tests cover the substrate landed here: the model and the sampling policy.
The stream-drain writer is deliberately NOT wired into the live location path
yet — see the branch notes — so there is no test here asserting that a live
ping produces a row. What is tested is every rule that writer will obey.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone

import servers.redis_client as rc
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.location_trail import (
    MAX_ACCURACY_METRES, MIN_DISTANCE_METRES, MIN_INTERVAL_SECONDS,
    Candidate, Rejected, _stream_id_to_datetime, accuracy_is_acceptable,
    haversine_metres, parse_coordinate, sample, should_keep, trip_is_collecting,
)
from servers.ride.models import Trip, TripLocationPoint, TripStatus

User = get_user_model()

LAT = Decimal('17.4450000')
LNG = Decimal('78.3800000')

# Earlier than BASE_MS (the synthetic stream-id epoch used below) and earlier
# than any real ping a test appends, so the trip's collection window is open for
# every event in this file.
ACCEPTED_AT = datetime(2025, 1, 1, tzinfo=dt_timezone.utc)


def _ms_ago(minutes=0, seconds=0):
    """A stream id timestamp relative to now.

    Tests about the late-arrival window need real recent times: the window is a
    query against `completed_at`, so a synthetic epoch a year in the past would
    be excluded for the right reason and prove nothing.
    """
    return int((timezone.now() - timedelta(minutes=minutes, seconds=seconds))
               .timestamp() * 1000)


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
    # Accepted before any synthetic ping below. The writer assigns a ping to the
    # trip whose collection window contains the moment it was recorded, and
    # `requested_at` is auto-set to now while the fabricated stream ids sit in
    # the past, so an explicit acceptance time is what makes these fixtures
    # represent a real trip rather than a time-travelling one.
    t.accepted_at = ACCEPTED_AT
    t.save(update_fields=['driver_id', 'accepted_at'])
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
def test_reprocessing_a_stream_event_is_refused_by_the_database(trip, driver):
    """The writer's idempotency key.

    Celery retries and Redis consumer groups are both at-least-once, so the
    same stream entry can arrive twice. A duplicate point would inflate actual
    distance and therefore the fare, so the database refuses it.
    """
    at = timezone.now()
    TripLocationPoint.objects.create(
        trip=trip, driver=driver, latitude=LAT, longitude=LNG,
        recorded_at=at, sequence=1, source_event_id='1758623456789-0',
    )
    with pytest.raises(IntegrityError):
        TripLocationPoint.objects.create(
            trip=trip, driver=driver, latitude=LAT, longitude=LNG,
            recorded_at=at, sequence=2, source_event_id='1758623456789-0',
        )


@pytest.mark.django_db
def test_backfilled_rows_without_an_event_id_coexist(trip, driver):
    """Uniqueness must not block reconstructed history.

    PostgreSQL allows unlimited NULLs in a unique index, so rows with no
    originating stream event need no partial predicate to coexist.
    """
    at = timezone.now()
    for i in range(3):
        TripLocationPoint.objects.create(
            trip=trip, driver=driver, latitude=LAT, longitude=LNG,
            recorded_at=at + timedelta(seconds=i), sequence=i,
            source=TripLocationPoint.SOURCE_BACKFILL, source_event_id=None,
        )
    assert TripLocationPoint.objects.filter(trip=trip).count() == 3


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


# ---------------------------------------------------------------------------
# The durable writer
# ---------------------------------------------------------------------------

def _event(entry_id, driver_id, lat=LAT, lng=LNG):
    """One raw Redis stream entry, exactly as read_location_events returns it."""
    return (entry_id, {'driver_id': str(driver_id), 'lat': str(lat), 'lng': str(lng)})


def _eid(ms, seq=0):
    return f'{ms}-{seq}'


BASE_MS = 1_758_600_000_000


@pytest.mark.django_db
def test_writer_stores_points_for_an_active_trip(trip, driver):
    from servers.ride.location_trail import persist_location_events

    events = [_event(_eid(BASE_MS), driver.id)]
    handled, stats = persist_location_events(events)

    assert handled == [_eid(BASE_MS)]
    assert stats['stored'] == 1
    p = TripLocationPoint.objects.get(trip=trip)
    assert p.driver_id == driver.id
    assert p.source == TripLocationPoint.SOURCE_DRIVER_WS
    assert p.source_event_id == _eid(BASE_MS)


@pytest.mark.django_db
def test_writer_stores_nothing_when_driver_has_no_active_trip(trip, driver):
    """Off-trip driver location is never persisted.

    The events are still HANDLED (so they get acknowledged), otherwise the
    consumer-group backlog would grow without limit for every idle driver.
    """
    from servers.ride.location_trail import persist_location_events

    trip.status_id = _status('completed')
    trip.save(update_fields=['status_id'])

    handled, stats = persist_location_events([_event(_eid(BASE_MS), driver.id)])

    assert handled == [_eid(BASE_MS)], 'must still be acked, or the backlog grows forever'
    assert stats['stored'] == 0
    assert stats['no_active_trip'] == 1
    assert TripLocationPoint.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize('status_code,stored', [
    ('requested', 0), ('accepted', 1), ('reached', 1),
    ('in_progress', 1), ('completed', 0), ('cancelled', 0),
])
def test_writer_collection_window_matches_durable_status(trip, driver, status_code, stored):
    """Collection starts at accepted and stops the instant the trip is terminal."""
    from servers.ride.location_trail import persist_location_events

    trip.status_id = _status(status_code)
    trip.save(update_fields=['status_id'])

    _handled, stats = persist_location_events([_event(_eid(BASE_MS), driver.id)])
    assert stats['stored'] == stored, f'{status_code} should store {stored}'


@pytest.mark.django_db
def test_writer_applies_sampling_across_a_batch(trip, driver):
    """A stationary driver in one batch must yield one row, not many."""
    from servers.ride.location_trail import persist_location_events

    events = [_event(_eid(BASE_MS + 10_000 * i), driver.id) for i in range(20)]
    _handled, stats = persist_location_events(events)

    assert stats['received'] == 20
    assert stats['stored'] == 1
    assert stats['sampled_out'] == 19
    assert TripLocationPoint.objects.filter(trip=trip).count() == 1


@pytest.mark.django_db
def test_writer_sampling_continues_across_batches(trip, driver):
    """The threshold must not reset per batch.

    Without loading the last stored point, each batch would treat its first
    event as the trip's first point and a stationary driver would gain one row
    per batch forever.
    """
    from servers.ride.location_trail import persist_location_events

    persist_location_events([_event(_eid(BASE_MS), driver.id)])
    _handled, stats = persist_location_events([_event(_eid(BASE_MS + 10_000), driver.id)])

    assert stats['stored'] == 0, 'second batch re-stored a stationary point'
    assert TripLocationPoint.objects.filter(trip=trip).count() == 1


@pytest.mark.django_db
def test_writer_stores_a_moving_driver(trip, driver):
    from servers.ride.location_trail import persist_location_events

    events = []
    for i in range(6):
        lat = Decimal('17.4450000') + Decimal('0.001') * i
        events.append(_event(_eid(BASE_MS + 10_000 * i), driver.id, lat=lat))
    _handled, stats = persist_location_events(events)

    assert stats['stored'] == 6
    seqs = list(
        TripLocationPoint.objects.filter(trip=trip)
        .order_by('recorded_at').values_list('source_event_id', flat=True)
    )
    assert seqs == [e[0] for e in events], 'points must be replayable in order'


# --- idempotency / at-least-once -------------------------------------------

@pytest.mark.django_db
def test_reprocessing_the_same_batch_creates_no_duplicates(trip, driver):
    """The core durability property: at-least-once delivery is safe.

    Simulates a worker that committed rows then died before acknowledging, so
    Redis redelivers the identical entries.
    """
    from servers.ride.location_trail import persist_location_events

    events = [
        _event(_eid(BASE_MS + 10_000 * i), driver.id,
               lat=Decimal('17.4450000') + Decimal('0.001') * i)
        for i in range(4)
    ]
    persist_location_events(events)
    first_count = TripLocationPoint.objects.filter(trip=trip).count()
    assert first_count == 4

    persist_location_events(events)          # redelivery
    persist_location_events(events)          # and again

    assert TripLocationPoint.objects.filter(trip=trip).count() == first_count, \
        'duplicate points would inflate actual distance and therefore the fare'


@pytest.mark.django_db
def test_partial_batch_overlap_stores_only_the_new_events(trip, driver):
    from servers.ride.location_trail import persist_location_events

    mk = lambda i: _event(_eid(BASE_MS + 10_000 * i), driver.id,  # noqa: E731
                          lat=Decimal('17.4450000') + Decimal('0.001') * i)
    persist_location_events([mk(0), mk(1)])
    persist_location_events([mk(1), mk(2)])      # 1 overlaps

    ids = set(
        TripLocationPoint.objects.filter(trip=trip)
        .values_list('source_event_id', flat=True)
    )
    assert ids == {_eid(BASE_MS), _eid(BASE_MS + 10_000), _eid(BASE_MS + 20_000)}


# --- malformed input -------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('fields', [
    {'driver_id': 'abc', 'lat': '17.44', 'lng': '78.38'},
    {'driver_id': '1', 'lat': 'nope', 'lng': '78.38'},
    {'driver_id': '1', 'lat': '0', 'lng': '0'},
    {'driver_id': '1', 'lat': '91', 'lng': '78.38'},
    {'driver_id': '1'},
])
def test_writer_drops_malformed_events_but_still_handles_them(trip, driver, fields):
    from servers.ride.location_trail import persist_location_events

    handled, stats = persist_location_events([(_eid(BASE_MS), fields)])
    assert handled == [_eid(BASE_MS)], 'malformed events must be acked, not retried forever'
    assert stats['stored'] == 0
    assert stats['invalid'] == 1
    assert TripLocationPoint.objects.count() == 0


@pytest.mark.django_db
def test_writer_drops_an_unparseable_stream_id(trip, driver):
    from servers.ride.location_trail import persist_location_events

    handled, stats = persist_location_events([
        ('not-a-stream-id', {'driver_id': str(driver.id), 'lat': str(LAT), 'lng': str(LNG)}),
    ])
    assert handled == ['not-a-stream-id']
    assert stats['invalid'] == 1


@pytest.mark.django_db
def test_writer_handles_an_empty_batch(db):
    from servers.ride.location_trail import persist_location_events

    handled, stats = persist_location_events([])
    assert handled == []
    assert stats['received'] == 0


# --- feature flag / drain orchestration ------------------------------------

@pytest.mark.django_db
def test_drain_is_a_noop_while_the_flag_is_off(settings, driver):
    """Default-off means the writer can ship without changing behaviour."""
    from servers.ride.location_trail import drain_location_stream

    settings.GPS_TRAIL_ENABLED = False
    assert drain_location_stream() == {'enabled': False}


@pytest.mark.django_db
def test_drain_reads_acks_and_stores_when_enabled(settings, trip, driver):
    from servers.ride import location_trail as lt

    settings.GPS_TRAIL_ENABLED = True
    settings.GPS_TRAIL_BATCH_SIZE = 10
    settings.GPS_TRAIL_MAX_EVENTS_PER_RUN = 10

    events = [_event(_eid(BASE_MS), driver.id)]
    acked = []

    with mock.patch.object(rc, 'ensure_location_stream_group', return_value=True), \
            mock.patch.object(rc, 'read_location_events', side_effect=[events, []]), \
            mock.patch.object(rc, 'ack_location_events',
                              side_effect=lambda ids: acked.extend(ids) or len(ids)):
        out = lt.drain_location_stream()

    assert out['enabled'] is True
    assert out['stored'] == 1
    assert acked == [_eid(BASE_MS)], 'entries must be acked only after rows commit'
    assert TripLocationPoint.objects.filter(trip=trip).count() == 1


@pytest.mark.django_db
def test_drain_respects_the_per_run_event_budget(settings, trip, driver):
    """A backlog must not turn one tick into unbounded work."""
    from servers.ride import location_trail as lt

    settings.GPS_TRAIL_ENABLED = True
    settings.GPS_TRAIL_BATCH_SIZE = 2
    settings.GPS_TRAIL_MAX_EVENTS_PER_RUN = 4

    calls = {'n': 0}

    def _read(count, consumer='worker-1', block_ms=None):
        calls['n'] += 1
        base = BASE_MS + calls['n'] * 100_000
        return [_event(_eid(base + i), driver.id) for i in range(count)]

    with mock.patch.object(rc, 'ensure_location_stream_group', return_value=True), \
            mock.patch.object(rc, 'read_location_events', side_effect=_read), \
            mock.patch.object(rc, 'ack_location_events', return_value=1):
        out = lt.drain_location_stream()

    assert out['received'] == 4, 'budget must cap total events consumed'


@pytest.mark.django_db
def test_drain_survives_redis_being_unavailable(settings, trip, driver):
    """GPS persistence failure must not raise into anything upstream."""
    from servers.ride import location_trail as lt

    settings.GPS_TRAIL_ENABLED = True
    with mock.patch.object(rc, 'ensure_location_stream_group', return_value=False), \
            mock.patch.object(rc, 'read_location_events', return_value=[]):
        out = lt.drain_location_stream()
    assert out['received'] == 0
    assert TripLocationPoint.objects.count() == 0


# --- isolation from the live path ------------------------------------------

def test_live_location_path_does_not_depend_on_the_trail():
    """The hot path must not import or call the durable writer.

    update_driver_location does the Redis XADD; persistence happens later in a
    Celery task. If the writer were ever called inline, a database or Redis
    problem in persistence would surface as a failed driver location ping and
    could stall dispatch.
    """
    import inspect

    from servers.driver import utils as driver_utils

    src = inspect.getsource(driver_utils)
    assert 'location_trail' not in src
    assert 'TripLocationPoint' not in src


def test_consumer_holds_no_trail_logic():
    """Architecture rule: new domain behaviour does not go into consumers.py."""
    import inspect

    from servers import consumers

    src = inspect.getsource(consumers)
    assert 'TripLocationPoint' not in src
    assert 'location_trail' not in src
    assert 'persist_location_events' not in src


def test_trail_task_is_idempotent_and_late_acking():
    """Negative control on the task's delivery posture.

    acks_late is only safe because persistence is idempotent on
    (trip, source_event_id); if it were flipped off, a worker death would lose
    the batch instead of repeating it.
    """
    import servers.ride.tasks  # noqa: F401  (registers the task)
    from base.celery import app

    t = app.tasks['ride.persist_location_trail']
    assert t.acks_late is True


def test_task_is_a_thin_adapter():
    """The task must delegate, not implement.

    Asserted against the compiled code object rather than the source text, so a
    docstring that mentions a name cannot make this pass or fail spuriously.
    """
    from servers.ride import tasks

    # shared_task wraps the function in a Task proxy; `run` is the original.
    names = set(tasks.persist_location_trail.run.__code__.co_names)
    assert 'drain_location_stream' in names, 'task should delegate to the service'
    assert 'bulk_create' not in names, 'persistence logic belongs in the service'
    assert 'TripLocationPoint' not in names, 'task should not touch the model'


# --- privacy ---------------------------------------------------------------

def test_no_public_or_authenticated_endpoint_exposes_the_trail():
    """The trail must not be reachable over HTTP at all in this change."""
    import servers.ride.urls as ride_urls
    import servers.driver.urls as driver_urls
    import servers.rider.urls as rider_urls

    for mod in (ride_urls, driver_urls, rider_urls):
        src = str([getattr(p, 'name', '') for p in mod.urlpatterns])
        assert 'location_point' not in src
        assert 'trail' not in src.lower()


def test_trail_is_not_registered_in_django_admin():
    """Deliberate: no browsable full-fleet location history by default."""
    from django.contrib import admin as dj_admin
    assert TripLocationPoint not in dj_admin.site._registry


def test_celery_task_args_cannot_leak_coordinates():
    """The trail task takes no arguments, so nothing to leak.

    Celery logs task args, and the redaction filter now replaces them with
    '***' -- but the stronger guarantee is that this task carries no payload.
    """
    import inspect

    from servers.ride import tasks

    sig = inspect.signature(tasks.persist_location_trail)
    params = [p for p in sig.parameters if p != 'self']
    assert params == [], f'trail task should take no args, got {params}'


def test_writer_logs_counts_not_positions():
    import inspect

    from servers.ride import location_trail

    src = inspect.getsource(location_trail.drain_location_stream)
    assert '_log(' in src
    assert 'latitude' not in src
    assert 'longitude' not in src


# ---------------------------------------------------------------------------
# End-to-end against a REAL Redis stream and consumer group
# ---------------------------------------------------------------------------
#
# The mocked tests above prove the policy. These prove the plumbing: consumer
# group creation, XREADGROUP claiming, XACK, and the pending-entry behaviour
# that makes at-least-once safe. Mocks cannot show any of that.

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


def _xadd(driver_id, lat, lng, ms=None):
    """Append a ping. `ms` sets an explicit stream id so tests can space events
    in time -- the sampler's 5s interval filter is driven by the stream id, and
    events appended in the same instant are correctly collapsed to one."""
    client = rc._stream_client()
    kwargs = {'maxlen': 100000}
    if ms is not None:
        kwargs['id'] = f'{ms}-0'
    return client.xadd(
        rc.LOCATION_STREAM,
        {'driver_id': str(driver_id), 'lat': str(lat), 'lng': str(lng)},
        **kwargs,
    )


@pytest.mark.django_db
def test_end_to_end_real_stream_to_durable_rows(settings, real_stream, trip, driver):
    """The whole pipeline: XADD -> consumer group -> sample -> rows -> XACK."""
    from servers.ride.location_trail import drain_location_stream

    settings.GPS_TRAIL_ENABLED = True
    settings.GPS_TRAIL_BATCH_SIZE = 100
    settings.GPS_TRAIL_MAX_EVENTS_PER_RUN = 100

    # A driver moving away from the pickup, ~111m per step.
    for i in range(5):
        _xadd(driver.id, Decimal('17.4450000') + Decimal('0.001') * i, LNG,
              ms=BASE_MS + 10_000 * i)

    out = drain_location_stream(consumer='itest')

    assert out['enabled'] is True
    assert out['received'] == 5
    assert out['stored'] == 5, out
    assert out['acked'] == 5, 'entries must be acknowledged after the rows commit'
    assert TripLocationPoint.objects.filter(trip=trip).count() == 5

    # Every row carries the originating stream id, which is the idempotency key.
    ids = set(
        TripLocationPoint.objects.filter(trip=trip)
        .values_list('source_event_id', flat=True)
    )
    assert all('-' in i for i in ids)

    # Nothing left pending, and a second drain finds nothing to do.
    assert rc.location_stream_pending() == 0
    again = drain_location_stream(consumer='itest')
    assert again['received'] == 0
    assert TripLocationPoint.objects.filter(trip=trip).count() == 5


@pytest.mark.django_db
def test_unacked_entries_are_redelivered_and_stored_once(settings, real_stream, trip, driver):
    """Worker dies after committing rows but before XACK.

    Redis redelivers the entries to the next reader; the unique constraint means
    the re-processing stores nothing new. This is the at-least-once guarantee
    the design depends on.
    """
    from servers.ride.location_trail import (
        drain_location_stream, persist_location_events,
    )

    settings.GPS_TRAIL_ENABLED = True
    settings.GPS_TRAIL_BATCH_SIZE = 100
    settings.GPS_TRAIL_MAX_EVENTS_PER_RUN = 100

    for i in range(3):
        _xadd(driver.id, Decimal('17.4450000') + Decimal('0.002') * i, LNG,
              ms=BASE_MS + 20_000 * i)

    # Claim and persist, but deliberately do NOT ack -- the crash.
    events = rc.read_location_events(count=10, consumer='crashy')
    assert len(events) == 3
    persist_location_events(events)
    assert TripLocationPoint.objects.filter(trip=trip).count() == 3
    assert rc.location_stream_pending() == 3, 'entries should still be pending'

    # Another worker reclaims the abandoned entries.
    claimed = rc._stream_client().xautoclaim(
        rc.LOCATION_STREAM, rc.LOCATION_STREAM_GROUP, 'recovery',
        min_idle_time=0, start_id='0',
    )
    reclaimed = claimed[1] if isinstance(claimed, (list, tuple)) else []
    assert reclaimed, 'redelivery should hand the entries to the new consumer'

    redelivered = []
    for entry_id, fields in reclaimed:
        eid = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
        clean = {(k.decode() if isinstance(k, bytes) else k):
                 (v.decode() if isinstance(v, bytes) else v)
                 for k, v in fields.items()}
        redelivered.append((eid, clean))

    handled, stats = persist_location_events(redelivered)
    rc.ack_location_events(handled)

    assert TripLocationPoint.objects.filter(trip=trip).count() == 3, \
        'redelivery must not duplicate points'
    assert stats['stored'] == 0 or TripLocationPoint.objects.filter(trip=trip).count() == 3


@pytest.mark.django_db
def test_off_trip_events_are_acked_so_the_backlog_drains(settings, real_stream, trip, driver):
    """An idle driver's pings must not accumulate as pending entries forever."""
    from servers.ride.location_trail import drain_location_stream

    settings.GPS_TRAIL_ENABLED = True
    trip.status_id = _status('completed')
    trip.save(update_fields=['status_id'])

    for i in range(4):
        _xadd(driver.id, LAT, LNG, ms=BASE_MS + 10_000 * i)

    out = drain_location_stream(consumer='itest')
    assert out['stored'] == 0
    assert out['no_active_trip'] == 4
    assert out['acked'] == 4
    assert rc.location_stream_pending() == 0, 'idle-driver pings must not pile up'
    assert TripLocationPoint.objects.count() == 0


@pytest.mark.django_db
def test_consumer_group_survives_being_created_twice(real_stream):
    """ensure_location_stream_group must be idempotent (BUSYGROUP)."""
    assert rc.ensure_location_stream_group() is True
    assert rc.ensure_location_stream_group() is True


@pytest.mark.django_db
def test_live_geo_update_still_works_while_trail_is_enabled(settings, real_stream, trip, driver):
    """The live path must be unaffected by the trail existing.

    update_driver_location does the XADD and returns; it must not wait on, or
    fail because of, durable persistence.
    """
    from servers.driver.utils import update_driver_location

    settings.GPS_TRAIL_ENABLED = True
    resp = update_driver_location(driver_id=driver.id, lng=float(LNG), lat=float(LAT))
    assert getattr(resp, 'status_code', 200) == 200
    # The ping is in the stream, and nothing has been persisted yet.
    assert rc._stream_client().xlen(rc.LOCATION_STREAM) >= 1
    assert TripLocationPoint.objects.count() == 0, \
        'persistence must be deferred, not inline on the hot path'


def test_stream_helpers_use_the_same_redis_db_that_writes_the_stream():
    """Regression: the writer read db 2 while the pings went to db 3.

    servers/driver/utils.py builds its own client against REDIS_URL + '/3' and
    XADDs there; servers/redis_client.py's own client is db 2. Reading the
    stream through the db-2 client returns an empty result with no error, so the
    durable trail would have silently persisted nothing in production. Caught
    only because this suite talks to a real Redis.
    """
    from servers.driver import utils as driver_utils

    client = rc._stream_client()
    if client is None or driver_utils.redis_client is None:
        pytest.skip('Redis not available')

    assert client is driver_utils.redis_client, \
        'stream helpers must borrow the exact client that writes the stream'
    assert client.connection_pool.connection_kwargs.get('db') == 3
    assert rc.redis_client.connection_pool.connection_kwargs.get('db') == 2, \
        'geo client is a different db -- that is the whole point of this test'


# ---------------------------------------------------------------------------
# Events are matched to the trip that was live WHEN THEY WERE RECORDED
# ---------------------------------------------------------------------------
#
# The drain is periodic. So by the time it runs, the pings from the end of a
# journey belong to a trip that has already completed. Matching on the driver's
# status at drain time discarded exactly those pings, which made every measured
# distance short by the final stretch -- systematically, silently, and worse the
# further the drain fell behind. These tests pin the fix.

@pytest.mark.django_db
def test_pings_recorded_before_completion_are_stored_after_completion(settings, trip, driver):
    """The regression: a completed trip must still absorb its own trail."""
    from servers.ride.location_trail import persist_location_events

    settings.GPS_TRAIL_ENABLED = True

    # Recorded across the last few minutes of a journey.
    events = [_event(_eid(_ms_ago(minutes=10 - i)), driver.id,
                     lat=Decimal('17.4450000') + Decimal('0.001') * i)
              for i in range(4)]

    # The trip ends after those pings were recorded, but before the drain runs.
    trip.status_id = _status('completed')
    trip.completed_at = timezone.now() - timedelta(minutes=5)
    trip.save(update_fields=['status_id', 'completed_at'])

    _handled, stats = persist_location_events(events)
    assert stats['stored'] == 4, stats
    assert stats['no_active_trip'] == 0
    assert TripLocationPoint.objects.filter(trip=trip).count() == 4


@pytest.mark.django_db
def test_pings_recorded_after_the_trip_ended_are_not_stored(settings, trip, driver):
    """The driver's movements after dropping the rider are not part of the trip."""
    from servers.ride.location_trail import persist_location_events

    settings.GPS_TRAIL_ENABLED = True

    trip.status_id = _status('completed')
    trip.completed_at = timezone.now() - timedelta(minutes=10)
    trip.save(update_fields=['status_id', 'completed_at'])

    _handled, stats = persist_location_events(
        [_event(_eid(_ms_ago(minutes=2)), driver.id)]
    )
    assert stats['stored'] == 0
    assert stats['no_active_trip'] == 1


@pytest.mark.django_db
def test_pings_recorded_before_acceptance_are_not_stored(settings, trip, driver):
    """An idle driver's location is never persisted, even as backlog.

    Before this, a ping sitting in the stream from before the driver was assigned
    would have been attributed to whatever trip they later accepted -- storing a
    stranger's-eye view of the driver's private movements against a trip.
    """
    from servers.ride.location_trail import persist_location_events

    settings.GPS_TRAIL_ENABLED = True

    before_acceptance = int(
        (ACCEPTED_AT - timedelta(hours=1)).timestamp() * 1000
    )
    _handled, stats = persist_location_events(
        [_event(_eid(before_acceptance), driver.id)]
    )
    assert stats['stored'] == 0
    assert stats['no_active_trip'] == 1


@pytest.mark.django_db
def test_a_long_finished_trip_stops_absorbing_pings(settings, trip, driver):
    """The late-arrival window is bounded, or a stale stream rewrites history."""
    from servers.ride.location_trail import persist_location_events

    settings.GPS_TRAIL_ENABLED = True
    settings.GPS_TRAIL_LATE_ARRIVAL_MINUTES = 60

    long_ago = timezone.now() - timedelta(days=3)
    trip.status_id = _status('completed')
    trip.completed_at = long_ago
    trip.save(update_fields=['status_id', 'completed_at'])

    ms = int((long_ago - timedelta(minutes=5)).timestamp() * 1000)
    _handled, stats = persist_location_events([_event(_eid(ms), driver.id)])
    assert stats['stored'] == 0, 'a trip finished days ago is outside the window'
    assert stats['no_active_trip'] == 1


@pytest.mark.django_db
def test_a_straggler_lands_on_the_right_trip_when_the_next_one_has_started(
    settings, trip, driver, rider,
):
    """A driver on their next trip must not have the previous trail merged in.

    This is the case that makes time-based matching necessary rather than merely
    tidy: one drain can carry the end of one journey and the start of the next.
    """
    from servers.ride.location_trail import persist_location_events

    settings.GPS_TRAIL_ENABLED = True

    # First trip ends ten minutes ago.
    trip.status_id = _status('completed')
    trip.completed_at = timezone.now() - timedelta(minutes=10)
    trip.save(update_fields=['status_id', 'completed_at'])

    # Second trip is accepted two minutes later and is still in progress.
    second = Trip.objects.create(
        user_id=rider, status_id=_status('in_progress'),
        pickup_lat=LAT, pickup_long=LNG,
        destination_lat=LAT, destination_long=LNG,
    )
    second.driver_id = driver
    second.accepted_at = timezone.now() - timedelta(minutes=8)
    second.save(update_fields=['driver_id', 'accepted_at'])

    _handled, stats = persist_location_events([
        _event(_eid(_ms_ago(minutes=12)), driver.id),        # first trip
        _event(_eid(_ms_ago(minutes=6)), driver.id,
               lat=Decimal('17.4500000')),                   # second trip
    ])
    assert stats['stored'] == 2, stats
    assert TripLocationPoint.objects.filter(trip=trip).count() == 1
    assert TripLocationPoint.objects.filter(trip=second).count() == 1


@pytest.mark.django_db
def test_a_cancelled_trip_keeps_the_trail_it_already_produced(settings, trip, driver):
    """Cancellations are disputed too, and the approach is the evidence."""
    from servers.ride.location_trail import persist_location_events

    settings.GPS_TRAIL_ENABLED = True

    trip.status_id = _status('cancelled')
    trip.cancelled_at = timezone.now() - timedelta(minutes=5)
    trip.save(update_fields=['status_id', 'cancelled_at'])

    _handled, stats = persist_location_events(
        [_event(_eid(_ms_ago(minutes=10)), driver.id)]
    )
    assert stats['stored'] == 1, stats
