"""A Redis failure must never be reported as "this driver is free".

`get_driver_active_trip` used to catch every exception and return None. None is
also what it returns for a genuinely idle driver, so every caller read a Redis
outage as availability:

  * `add_driver_location` skipped the `zrem` that removes a busy driver from the
    geo index, leaving a driver who is already on a trip discoverable as nearby
    and available.
  * `find_nearby_drivers` reported status 'online' instead of 'busy'.
  * the admin driver listing showed a driver on a trip as online.

None of it logged above DEBUG, so the failure mode was invisible. It surfaced as
an unrelated-looking test failure inside the full PostgreSQL suite -- a `SET`
reporting success and an immediate `GET` in the same thread returning nothing,
"with no client error raised", as the CI workflow comment put it. There was a
client error. It was being swallowed.

The row lock in the trip-accept path is what actually stops a double assignment
from committing, so this was not a money defect. A driver being offered rides it
cannot take, and an operations console that disagrees with reality, are still
real failures.
"""

import pytest

from servers import redis_client as rc
from servers.redis_client import (
    DriverTripStateUnavailable,
    get_driver_active_trip,
    set_driver_active_trip,
)


class _BoomClient:
    """A Redis client whose reads fail the way a real one does under stress."""

    def __init__(self, exc=None):
        self.exc = exc or ConnectionError('connection pool exhausted')

    def get(self, key):
        raise self.exc

    def set(self, key, value):
        raise self.exc


class _CorruptClient:
    """Holds a value that is not a trip id."""

    def get(self, key):
        return b'not-a-trip-id'


class _FakeClient:
    def __init__(self, store=None, set_result=True):
        self.store = store or {}
        self.set_result = set_result

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value
        return self.set_result


@pytest.fixture
def swap_client(monkeypatch):
    def _swap(client):
        monkeypatch.setattr(rc, 'redis_client', client)
    return _swap


def test_a_read_failure_raises_instead_of_claiming_the_driver_is_free(swap_client):
    """The whole point. None must mean idle, never "we could not tell"."""
    swap_client(_BoomClient())

    with pytest.raises(DriverTripStateUnavailable):
        get_driver_active_trip(42)


def test_a_corrupt_value_raises_rather_than_reading_as_free(swap_client):
    """Something else wrote this key. Treating that as idle is the same bug."""
    swap_client(_CorruptClient())

    with pytest.raises(DriverTripStateUnavailable):
        get_driver_active_trip(42)


def test_an_idle_driver_still_reports_none(swap_client):
    """The ordinary case must be unchanged."""
    swap_client(_FakeClient())

    assert get_driver_active_trip(42) is None


def test_a_busy_driver_reports_its_trip_id(swap_client):
    swap_client(_FakeClient({f'{rc.ACTIVE_TRIP_PREFIX}42': b'77'}))

    assert get_driver_active_trip(42) == 77


def test_no_redis_at_all_is_still_none(swap_client):
    """Unit tests run without Redis and must not start raising."""
    swap_client(None)

    assert get_driver_active_trip(42) is None


def test_set_reports_failure_rather_than_always_claiming_success(swap_client):
    """It used to `return True` without looking at the result of set()."""
    swap_client(_FakeClient(set_result=None))

    assert set_driver_active_trip(42, 77) is False


def test_set_reports_success_when_redis_confirms(swap_client):
    client = _FakeClient()
    swap_client(client)

    assert set_driver_active_trip(42, 77) is True
    assert client.store[f'{rc.ACTIVE_TRIP_PREFIX}42'] == '77'


def test_set_swallows_nothing_silently_but_still_returns_false(swap_client):
    """A write failure is reported to the caller, not raised at it.

    Asymmetric with the read on purpose: a caller that cannot record busyness
    can retry on the next location tick, whereas a caller that cannot READ
    busyness must not be allowed to guess.
    """
    swap_client(_BoomClient())

    assert set_driver_active_trip(42, 77) is False


def test_add_driver_location_treats_unknown_state_as_busy(monkeypatch):
    """The call site that governs dispatch.

    A driver whose trip state cannot be read must be taken out of the geo index,
    not left in it. Left in, dispatch can offer a ride to a driver already on
    one.
    """
    removed = []

    class _Client:
        def setex(self, *a, **k):
            return True

        def get(self, key):
            raise ConnectionError('boom')

        def zrem(self, key, member):
            removed.append((key, member))
            return 1

        def zadd(self, *a, **k):
            return 1

        def set(self, *a, **k):
            return True

        def expire(self, *a, **k):
            return True

        def hset(self, *a, **k):
            return 1

    monkeypatch.setattr(rc, 'redis_client', _Client())
    monkeypatch.setattr(rc, 'update_driver_location', lambda **kw: None)
    # add_driver_location returns early when the driver has no vehicle type, and
    # this fake's get() raises, so the cached lookup would fall through to a DB
    # read for a driver that does not exist. Stubbed so the test reaches the
    # active-trip check it is actually about.
    monkeypatch.setattr(rc, 'get_driver_vehicle_type', lambda _driver_id: 'car')

    result = rc.add_driver_location(driver_id=7, lat=17.44, lng=78.38)

    assert removed, (
        'a driver whose trip state is unknown was left in the geo index, so '
        'dispatch can still offer them a ride'
    )
    assert result.get('success') is True
