"""The pickup OTP never lands in the Redis trip cache (AUDIT 27.76).

Accepting a trip used to write `otp=<plaintext>` into the `ride:<trip_id>` hash, so anyone
who could read Redis could start someone else's ride. Nothing reads the OTP from that hash:
the start-trip check compares against `Trip.otp` in the database. So the cache no longer
holds it at all, and `cache_trip` drops an `otp` field if one is ever passed again.
"""

from unittest import mock

import pytest

import servers.redis_client as rc
from servers.consumers import TripStatusConsumer
from test_driver_active_trip_invariant import (  # noqa: F401 - fixtures
    _isolate_redis, _make_trip, driver, rider, vehicle_type,
)

pytestmark = pytest.mark.django_db

KNOWN_OTP = '482913'


@pytest.fixture
def live_redis():
    if rc.redis_client is None:
        pytest.skip('needs Redis (REDIS_URL)')
    try:
        rc.redis_client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip('Redis is not reachable')
    return rc.redis_client


def _raw(name):
    # The consumer methods are wrapped in DatabaseSyncToAsync; take the sync function off the
    # class so the real code runs (see test_driver_active_trip_invariant.accept).
    return TripStatusConsumer.__dict__[name].func


def _consumer(trip, user):
    consumer = TripStatusConsumer()
    consumer.trip_id = trip.id
    consumer.user = user
    return consumer


def _accept_with_real_cache(trip, driver_user):
    """Accept through the real `_accept_trip` with the REAL `cache_trip` (only the
    unrelated side effects stubbed) and a known OTP, so the Redis contents can be checked."""
    with mock.patch('servers.consumers.generate_otp', return_value=KNOWN_OTP), \
            mock.patch.object(rc, 'set_driver_active_trip', return_value=True), \
            mock.patch.object(rc, 'remove_driver', return_value=True), \
            mock.patch('servers.ride.dispatch.dismiss_outstanding_offers', return_value=[]), \
            mock.patch('servers.auth_user.services.send_push_notification', return_value=True):
        return _raw('_accept_trip')(_consumer(trip, driver_user))


def _redis_values_for(client, trip_id):
    values = []
    for key in client.scan_iter(match=f'*{trip_id}*', count=500):
        kind = client.type(key)
        kind = kind.decode() if isinstance(kind, bytes) else kind
        if kind == 'hash':
            for field, value in client.hgetall(key).items():
                values.append((key, field, value))
        elif kind == 'string':
            values.append((key, None, client.get(key)))
    return values


class TestTheOtpIsNotCached:
    def test_accepting_a_trip_leaves_no_plaintext_otp_in_redis(
            self, live_redis, rider, vehicle_type, driver):
        trip = _make_trip(rider, vehicle_type, status='requested')
        live_redis.delete(f'{rc.RIDE_CACHE_PREFIX}{trip.id}')
        try:
            result = _accept_with_real_cache(trip, driver.user_id)
            assert result['success'] is True, result

            trip.refresh_from_db()
            assert trip.otp == KNOWN_OTP  # the DB keeps it: the rider's own API reads it there

            cached = live_redis.hgetall(f'{rc.RIDE_CACHE_PREFIX}{trip.id}')
            assert cached, 'the accept should still cache the trip'
            assert 'otp' not in {str(k) for k in cached}
            for key, field, value in _redis_values_for(live_redis, trip.id):
                assert KNOWN_OTP not in str(value), (key, field)
        finally:
            live_redis.delete(f'{rc.RIDE_CACHE_PREFIX}{trip.id}')

    def test_cache_trip_drops_an_otp_field(self, live_redis):
        key = f'{rc.RIDE_CACHE_PREFIX}otp-guard-test'
        live_redis.delete(key)
        try:
            assert rc.cache_trip('otp-guard-test', status='accepted', otp=KNOWN_OTP) is True
            assert live_redis.hgetall(key) == {'status': 'accepted'}
        finally:
            live_redis.delete(key)


class TestStartTripStillChecksTheOtp:
    """The verification path (`_update_trip_status('in_progress', otp_input=...)`) reads
    Trip.otp from the database, so it is unaffected; pinned here so it stays that way."""

    def _start(self, trip, driver, code):
        return _raw('_update_trip_status')(_consumer(trip, driver.user_id), 'in_progress',
                                           otp_input=code)

    def test_the_right_code_starts_the_trip(self, rider, vehicle_type, driver):
        trip = _make_trip(rider, vehicle_type, status='accepted', driver=driver)
        trip.otp = KNOWN_OTP
        trip.save(update_fields=['otp'])
        result = self._start(trip, driver, KNOWN_OTP)
        assert result['success'] is True, result
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress'

    def test_a_wrong_code_is_refused(self, rider, vehicle_type, driver):
        trip = _make_trip(rider, vehicle_type, status='accepted', driver=driver)
        trip.otp = KNOWN_OTP
        trip.save(update_fields=['otp'])
        result = self._start(trip, driver, '000000')
        assert result['success'] is False and result['reason'] == 'invalid_otp'
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'accepted'
