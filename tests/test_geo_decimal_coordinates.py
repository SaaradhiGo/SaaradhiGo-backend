"""Geo queries must accept the coordinate type the models actually store.

`Trip.pickup_lat` and `pickup_long` are `DecimalField`, so any code path that
reads a trip and asks Redis a geospatial question hands it a `Decimal`. redis-py
rejects `Decimal` outright:

    Invalid input of type: 'Decimal'. Convert to a bytes, string, int or float first.

`nearby_drivers` and `count_nearby_active_riders` both validated their inputs --
which converts internally and throws the converted value away -- and then passed
the originals to `geosearch`. `nearby_drivers` is the driver search dispatch
depends on; it happens to be safe today only because `ride.dispatch` wraps its
arguments in `float()` at the call site. `count_nearby_active_riders` feeds the
surge multiplier and had no such caller-side protection, so surge demand silently
counted zero (the exception is caught and logged) for any Decimal caller.

These tests pin the coercion at the boundary, where it cannot be forgotten.
"""

from decimal import Decimal

import pytest

import servers.redis_client as rc

LAT = Decimal('17.4450000')
LNG = Decimal('78.3800000')


@pytest.fixture
def live_redis():
    if rc.redis_client is None:
        pytest.skip('Redis not available')
    try:
        rc.redis_client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip('Redis not usable')
    return rc.redis_client


@pytest.fixture
def indexed_driver(live_redis):
    """One driver in the geo index, so `geosearch` actually runs.

    Without a populated index the key scan finds nothing and the search is never
    called, so the test would pass whether or not the coercion exists. Seeding
    one driver is what gives this test a real negative control.
    """
    key = rc.geo_key_for('sedan')
    live_redis.geoadd(key, [float(LNG), float(LAT), 'driver:900001'])
    live_redis.setex('driver_heartbeat:900001', 120, '1')
    yield key
    try:
        live_redis.zrem(key, 'driver:900001')
        live_redis.delete('driver_heartbeat:900001')
    except Exception:  # noqa: BLE001
        pass


def test_nearby_drivers_accepts_decimal_coordinates(live_redis, indexed_driver):
    """Returns a list, not None. None is this function's failure signal."""
    result = rc.nearby_drivers(lng=LNG, lat=LAT, radius=1000, count=10,
                               vehicle_type='sedan')
    assert result is not None, 'Decimal coordinates must not fail the query'
    assert isinstance(result, list)


def test_counting_nearby_riders_accepts_decimal_coordinates(live_redis, caplog):
    """A caught-and-logged exception is what made this invisible.

    The function returns 0 on error, and 0 is also a legitimate answer, so the
    only way to tell them apart is to assert nothing was logged as an error.
    """
    import logging

    with caplog.at_level(logging.ERROR, logger='servers.redis_client'):
        count = rc.count_nearby_active_riders(LNG, LAT, radius=3000)

    assert count == 0 or count > 0
    failures = [r for r in caplog.records
                if 'Failed to count nearby riders' in r.getMessage()]
    assert not failures, failures[0].getMessage() if failures else ''


def test_surge_lookup_survives_decimal_coordinates_from_a_trip_row(live_redis, caplog):
    """The real caller: pricing reads coordinates off a Trip and asks for surge."""
    import logging

    from servers.pricing.services import compute_surge_multiplier

    with caplog.at_level(logging.ERROR, logger='servers.redis_client'):
        multiplier = compute_surge_multiplier(
            LAT, LNG, cap=Decimal('1.50'), record_demand=False,
        )

    assert multiplier >= Decimal('1.00')
    assert not [r for r in caplog.records if 'Invalid input of type' in r.getMessage()]


def test_invalid_coordinates_are_still_rejected(live_redis):
    """Coercion must not become acceptance of nonsense."""
    assert rc.count_nearby_active_riders('not-a-number', LAT) == 0
    assert rc.count_nearby_active_riders(Decimal('999'), LAT) == 0
    assert rc.nearby_drivers(lng=Decimal('999'), lat=LAT) is None


def test_geo_queries_do_not_log_the_query_point(live_redis, indexed_driver, caplog):
    """A pickup point must not reach the logs, at any level.

    The debug line in `nearby_drivers` used to print `lng=` and `lat=`. It is
    DEBUG, so it is silent at the production default of WARNING -- but an engineer
    debugging dispatch raises the level, which is precisely when every rider's
    pickup location would start being emitted. The structured PII filter cannot
    save this one: it redacts `extra` keys and matches tokens like OTPs and
    phone numbers, not free-text coordinates in a message body.
    """
    import logging

    with caplog.at_level(logging.DEBUG, logger='servers.redis_client'):
        rc.nearby_drivers(lng=LNG, lat=LAT, radius=1000, count=10,
                          vehicle_type='sedan')
        rc.count_nearby_active_riders(LNG, LAT, radius=3000)

    emitted = ' | '.join(r.getMessage() for r in caplog.records)
    assert '17.445' not in emitted, emitted
    assert '78.38' not in emitted, emitted
    for token in ('lng=', 'lat=', 'longitude=', 'latitude='):
        assert token not in emitted, (token, emitted)
