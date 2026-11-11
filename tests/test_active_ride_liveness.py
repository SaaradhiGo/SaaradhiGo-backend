"""An abandoned in-progress ride must be recoverable without an engineer.

QA trip 42: the trip reached `in_progress`, both clients disappeared, and supply
was lost permanently. The rider could not cancel a started ride (correct policy),
`auto_cancel_trip` only covers unaccepted trips, `driver:active_trip:<id>` has no
TTL, and `add_driver_location` removes a driver from the geo index whenever that
key is set -- so reconnecting and pinging could never restore the driver to
dispatch. Recovery took an engineer opening the driver's socket and sending
`complete`.

The tests here are organised around what the fix must and must NOT do.

MUST:
  * keep durable evidence of when a trip was last alive, cheaply;
  * repair Redis from PostgreSQL so a returning driver recovers by themselves;
  * flag quiet trips so operations can see them.

MUST NOT:
  * cancel or complete anything;
  * treat silence as abandonment;
  * charge anybody.

The negative controls carry as much weight as the positives. A detector that
cancels a legitimate long ride would be worse than the bug it replaces: it would
abandon a rider mid-journey.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride import liveness
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def ride():
    rider_u = User.objects.create_user(phone_number='+919522000001', role='rider')
    Rider.objects.create(user_id=rider_u)
    driver_u = User.objects.create_user(phone_number='+919622000001', role='driver')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    driver = Driver.objects.create(user_id=driver_u, approved=True, status='online')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vt, vehicle_number='TS09EE3333',
    )
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])

    now = timezone.now()
    trip = Trip.objects.create(
        user_id=rider_u, driver_id=driver, status_id=_status('in_progress'),
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.4550000'), destination_long=Decimal('78.3900000'),
        estimated_fare=Decimal('150.00'), payment_method='cash',
        accepted_at=now, started_at=now,
    )
    return {'trip': trip, 'driver': driver, 'rider': rider_u}


def _silence(trip, seconds):
    """Make the trip look as though nothing has been heard for `seconds`.

    `requested_at` has to move too. It is auto_now_add, so a freshly created test
    trip carries a NOW value, and `last_known_activity` takes the newest of all the
    lifecycle timestamps -- which made a deliberately-silenced trip classify as
    HEALTHY. A genuinely quiet ride has an old requested_at as well, so the helper
    was the thing that was unrealistic, not the classifier.
    """
    past = timezone.now() - timezone.timedelta(seconds=seconds)
    Trip.objects.filter(id=trip.id).update(
        last_driver_activity_at=past, started_at=past, accepted_at=past,
        reached_at=past, requested_at=past,
    )
    trip.refresh_from_db()
    return trip


# ---------------------------------------------------------------------------
# Durable evidence, cheaply
# ---------------------------------------------------------------------------

def test_driver_activity_is_recorded(ride):
    trip = ride['trip']
    assert trip.last_driver_activity_at is None

    assert liveness.record_driver_activity(trip.id) is True

    trip.refresh_from_db()
    assert trip.last_driver_activity_at is not None


def test_activity_writes_are_coalesced(ride, settings):
    """12 pings a minute must not mean 12 UPDATEs a minute.

    At 50 concurrent rides an uncoalesced write would be 600 write transactions a
    minute for information nobody needs to the second.
    """
    settings.TRIP_LIVENESS_WRITE_INTERVAL_SECONDS = 60
    trip = ride['trip']

    assert liveness.record_driver_activity(trip.id) is True
    written = [liveness.record_driver_activity(trip.id) for _ in range(11)]

    assert written == [False] * 11, (
        'a second write inside the interval was allowed; write amplification is '
        'unbounded'
    )


def test_a_write_is_allowed_once_the_interval_has_passed(ride, settings):
    settings.TRIP_LIVENESS_WRITE_INTERVAL_SECONDS = 60
    trip = _silence(ride['trip'], 120)

    assert liveness.record_driver_activity(trip.id) is True


def test_recording_activity_never_touches_status_or_money(ride):
    trip = ride['trip']
    before = (trip.status_id_id, trip.estimated_fare, trip.final_fare,
              trip.completed_at, trip.cancelled_at)

    liveness.record_driver_activity(trip.id)

    trip.refresh_from_db()
    assert (trip.status_id_id, trip.estimated_fare, trip.final_fare,
            trip.completed_at, trip.cancelled_at) == before


def test_a_liveness_write_failure_never_breaks_the_gps_path(ride):
    """A missed write costs a minute of resolution. A raised exception costs the
    trip's telemetry, because it happens inside the GPS frame handler."""
    with mock.patch('servers.ride.models.Trip.objects') as objects:
        objects.filter.side_effect = RuntimeError('database gone')
        assert liveness.record_driver_activity(ride['trip'].id) is False


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_a_live_ride_is_healthy(ride, settings):
    settings.TRIP_STALE_AFTER_SECONDS = 600
    liveness.record_driver_activity(ride['trip'].id)
    ride['trip'].refresh_from_db()

    classification, silent, _ = liveness.classify(ride['trip'])

    assert classification == liveness.HEALTHY
    assert silent < 600


def test_a_quiet_ride_is_stale_not_abandoned(ride, settings):
    settings.TRIP_STALE_AFTER_SECONDS = 600
    settings.TRIP_OPERATOR_ATTENTION_AFTER_SECONDS = 1800
    trip = _silence(ride['trip'], 900)

    classification, silent, reason = liveness.classify(trip)

    assert classification == liveness.STALE
    assert 880 < silent < 920
    assert reason == 'driver_silent'


def test_a_long_silence_escalates_to_a_person(ride, settings):
    settings.TRIP_STALE_AFTER_SECONDS = 600
    settings.TRIP_OPERATOR_ATTENTION_AFTER_SECONDS = 1800
    trip = _silence(ride['trip'], 3600)

    classification, _, reason = liveness.classify(trip)

    assert classification == liveness.OPERATOR_ATTENTION
    assert reason == 'driver_silent_long'


def test_classification_falls_back_to_lifecycle_timestamps(ride, settings):
    """A trip that has never had an activity write must not look infinitely stale.

    Every trip created before this feature shipped is in exactly that position.
    """
    settings.TRIP_STALE_AFTER_SECONDS = 600
    trip = ride['trip']
    assert trip.last_driver_activity_at is None

    classification, silent, _ = liveness.classify(trip)

    assert classification == liveness.HEALTHY, (
        'a freshly started trip with no activity write yet was classified '
        f'{classification}'
    )
    assert silent is not None


# ---------------------------------------------------------------------------
# The negative controls: silence is not abandonment
# ---------------------------------------------------------------------------

def test_the_detector_never_changes_trip_status(ride, settings):
    """The single most important assertion in this file.

    A legitimate ride can cross a tunnel, sit in a basement, or run for hours.
    Cancelling it would abandon a rider mid-journey -- worse than the bug.
    """
    settings.TRIP_STALE_AFTER_SECONDS = 60
    trip = _silence(ride['trip'], 7200)
    before = trip.status_id.status_code

    liveness.flag_stale_trips()

    trip.refresh_from_db()
    assert trip.status_id.status_code == before == 'in_progress'
    assert trip.cancelled_at is None
    assert trip.completed_at is None


def test_the_detector_never_touches_money(ride, settings):
    settings.TRIP_STALE_AFTER_SECONDS = 60
    trip = _silence(ride['trip'], 7200)
    before = (trip.estimated_fare, trip.final_fare, trip.payment_status)

    liveness.flag_stale_trips()

    trip.refresh_from_db()
    assert (trip.estimated_fare, trip.final_fare, trip.payment_status) == before
    assert trip.final_fare is None, 'final_fare must remain non-authoritative'


def test_a_completed_trip_is_never_flagged(ride, settings):
    settings.TRIP_STALE_AFTER_SECONDS = 60
    trip = _silence(ride['trip'], 7200)
    Trip.objects.filter(id=trip.id).update(
        status_id=_status('completed'), completed_at=timezone.now())

    summary = liveness.flag_stale_trips()

    trip.refresh_from_db()
    assert trip.stale_flagged_at is None
    assert summary['flagged'] == 0


def test_a_requested_trip_is_not_the_detectors_business(ride, settings):
    """`auto_cancel_trip` owns unaccepted trips. Two mechanisms on one trip would
    be two answers to the same question."""
    settings.TRIP_STALE_AFTER_SECONDS = 60
    trip = _silence(ride['trip'], 7200)
    Trip.objects.filter(id=trip.id).update(
        status_id=_status('requested'), driver_id=None)

    liveness.flag_stale_trips()

    trip.refresh_from_db()
    assert trip.stale_flagged_at is None


# ---------------------------------------------------------------------------
# Detection and recovery
# ---------------------------------------------------------------------------

def test_a_quiet_ride_gets_flagged_with_a_reason(ride, settings):
    settings.TRIP_STALE_AFTER_SECONDS = 60
    trip = _silence(ride['trip'], 1200)

    summary = liveness.flag_stale_trips()

    trip.refresh_from_db()
    assert summary['flagged'] == 1
    assert trip.stale_flagged_at is not None
    assert trip.stale_reason


def test_flagging_is_idempotent(ride, settings):
    """A Celery redelivery after a worker death must not re-alert."""
    settings.TRIP_STALE_AFTER_SECONDS = 60
    trip = _silence(ride['trip'], 1200)

    first = liveness.flag_stale_trips()
    flagged_at = Trip.objects.get(id=trip.id).stale_flagged_at
    second = liveness.flag_stale_trips()

    assert first['flagged'] == 1
    assert second['flagged'] == 0
    assert second['already_flagged'] == 1
    assert Trip.objects.get(id=trip.id).stale_flagged_at == flagged_at


def test_the_driver_coming_back_clears_the_flag_by_itself(ride, settings):
    """RECOVERY sits before ESCALATION.

    A driver who reconnects needs no operator involvement at all, and the queue
    must not keep showing a ride that recovered.
    """
    settings.TRIP_STALE_AFTER_SECONDS = 60
    trip = _silence(ride['trip'], 1200)
    liveness.flag_stale_trips()
    assert Trip.objects.get(id=trip.id).stale_flagged_at is not None

    liveness.record_driver_activity(trip.id)

    trip.refresh_from_db()
    assert trip.stale_flagged_at is None
    assert trip.stale_reason == ''
    assert liveness.classify(trip)[0] == liveness.HEALTHY


# ---------------------------------------------------------------------------
# Reconciliation: PostgreSQL wins
# ---------------------------------------------------------------------------

class _FakeRedisState:
    """Just the active-trip key, in memory."""

    def __init__(self, value=None):
        self.value = value

    def get(self, key):
        return None if self.value is None else str(self.value).encode()

    def set(self, key, value):
        self.value = value
        return True

    def delete(self, *keys):
        self.value = None
        return 1


def test_a_stale_redis_key_is_cleared_when_the_database_says_the_trip_is_over(ride):
    """The trip-42 repair, and the reason a returning driver recovers alone."""
    trip, driver = ride['trip'], ride['driver']
    Trip.objects.filter(id=trip.id).update(
        status_id=_status('completed'), completed_at=timezone.now())
    fake = _FakeRedisState(value=trip.id)

    with mock.patch('servers.redis_client.redis_client', fake):
        outcome = liveness.reconcile_driver_active_trip(driver.id)

    assert outcome == 'repaired_cleared'
    assert fake.value is None, (
        'the stale active-trip key survived; the driver would stay out of the geo '
        'index and never be dispatched to again'
    )


def test_a_missing_redis_key_is_reconstructed_when_the_trip_is_live(ride):
    """A flushed or evicted Redis must not make a busy driver look free."""
    trip, driver = ride['trip'], ride['driver']
    fake = _FakeRedisState(value=None)

    with mock.patch('servers.redis_client.redis_client', fake):
        outcome = liveness.reconcile_driver_active_trip(driver.id)

    assert outcome == 'repaired_set'
    assert str(fake.value) == str(trip.id)


def test_redis_pointing_at_the_wrong_trip_is_corrected(ride):
    trip, driver = ride['trip'], ride['driver']
    fake = _FakeRedisState(value=999999)

    with mock.patch('servers.redis_client.redis_client', fake):
        outcome = liveness.reconcile_driver_active_trip(driver.id)

    assert outcome == 'repaired_set'
    assert str(fake.value) == str(trip.id)


def test_agreement_needs_no_repair(ride):
    trip, driver = ride['trip'], ride['driver']
    fake = _FakeRedisState(value=trip.id)

    with mock.patch('servers.redis_client.redis_client', fake):
        assert liveness.reconcile_driver_active_trip(driver.id) == 'ok'
    assert str(fake.value) == str(trip.id)


def test_reconciliation_never_raises_when_redis_is_unreadable(ride):
    """It runs on a driver's socket connect. A Redis problem must not stop a
    driver coming online."""
    class _Dead:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise ConnectionError('refused')
            return boom

    with mock.patch('servers.redis_client.redis_client', _Dead()):
        assert liveness.reconcile_driver_active_trip(ride['driver'].id) == 'unavailable'


def test_reconciliation_changes_no_trip_state(ride):
    trip, driver = ride['trip'], ride['driver']
    before = (trip.status_id_id, trip.completed_at, trip.cancelled_at,
              trip.estimated_fare, trip.final_fare)
    fake = _FakeRedisState(value=999999)

    with mock.patch('servers.redis_client.redis_client', fake):
        liveness.reconcile_driver_active_trip(driver.id)

    trip.refresh_from_db()
    assert (trip.status_id_id, trip.completed_at, trip.cancelled_at,
            trip.estimated_fare, trip.final_fare) == before


# ---------------------------------------------------------------------------
# Redis/DB disagreement shows up in classification
# ---------------------------------------------------------------------------

def test_a_terminal_trip_with_a_live_redis_key_is_recovery_required(ride):
    trip = ride['trip']
    Trip.objects.filter(id=trip.id).update(status_id=_status('completed'))
    trip.refresh_from_db()

    classification, _, reason = liveness.classify_with_redis_view(
        trip, redis_says_active=True)

    assert classification == liveness.RECOVERY_REQUIRED
    assert reason == 'redis_active_trip_but_db_terminal'


def test_an_active_trip_with_a_live_redis_key_is_just_classified_normally(ride):
    liveness.record_driver_activity(ride['trip'].id)
    ride['trip'].refresh_from_db()

    classification, _, _ = liveness.classify_with_redis_view(
        ride['trip'], redis_says_active=True)

    assert classification == liveness.HEALTHY
