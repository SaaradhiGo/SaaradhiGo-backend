"""How many billable Maps requests does one booking actually cost?

Measurement, not just assertion: each test prints the request count it
observed so the numbers in runbooks/maps-pricing-architecture.md can be
re-derived rather than trusted.

The two facts that decide SaaradhiGo's Maps bill:

  * Quoting fans out per vehicle category. The website does
    `Promise.all(vehicle_types.map(quoteFare))`, so a four-category market
    used to mean four identical Distance Matrix calls per quote.
  * Dispatch does not touch Maps at all. Candidate drivers come from Redis
    GEO (`GEOSEARCH` per vehicle-type key), so matching is free no matter
    how many drivers are evaluated or how many waves run.
"""

from unittest.mock import patch

import pytest

from servers.ride import utils

PICKUP = (16.5062, 80.6480)
DEST = (16.5190, 80.6305)
CATEGORIES = ('auto', 'hatchback', 'sedan', 'suv')


class _Resp:
    status_code = 200
    text = ''

    def json(self):
        return {
            'status': 'OK',
            'rows': [{'elements': [{
                'status': 'OK',
                'distance': {'value': 2400},
                'duration': {'value': 420},
            }]}],
        }


@pytest.fixture
def _cache():
    store = {}
    with patch('servers.redis_client.get_cached_map_data',
               side_effect=lambda k: store.get(k)), \
         patch('servers.redis_client.cache_map_data',
               side_effect=lambda k, d, ttl_seconds: store.setdefault(k, d)):
        yield store


@pytest.mark.django_db
def test_requests_per_quote_across_every_category(settings, _cache, capsys):
    settings.GOOGLE_MAPS_API_KEY = 'test-key'

    with patch('servers.ride.utils.requests.get', return_value=_Resp()) as http:
        for _ in CATEGORIES:
            utils.get_google_maps_distance(*PICKUP, *DEST)

    with capsys.disabled():
        print(f'\n  categories quoted            : {len(CATEGORIES)}')
        print(f'  billable Distance Matrix calls: {http.call_count}')
        print(f'  cache entries written         : {len(_cache)}')
    assert http.call_count == 1


@pytest.mark.django_db
def test_an_abandoned_booking_costs_one_quote_not_four(settings, _cache, capsys):
    """A rider who looks at fares and leaves still bought a route.

    That overhead is real and is why the model multiplies by booking
    ATTEMPTS, not completed rides -- but it is now one call, not four.
    """
    settings.GOOGLE_MAPS_API_KEY = 'test-key'

    with patch('servers.ride.utils.requests.get', return_value=_Resp()) as http:
        for _ in CATEGORIES:
            utils.get_google_maps_distance(*PICKUP, *DEST)
        # rider abandons; no booking, no further Maps traffic

    with capsys.disabled():
        print(f'\n  abandoned booking cost: {http.call_count} Distance Matrix call')
    assert http.call_count == 1


@pytest.mark.django_db
def test_a_second_rider_on_the_same_route_pays_nothing(settings, _cache, capsys):
    settings.GOOGLE_MAPS_API_KEY = 'test-key'

    with patch('servers.ride.utils.requests.get', return_value=_Resp()) as http:
        utils.get_google_maps_distance(*PICKUP, *DEST)   # rider A
        first = http.call_count
        utils.get_google_maps_distance(*PICKUP, *DEST)   # rider B, same route
        total = http.call_count

    with capsys.disabled():
        print(f'\n  first rider: {first} call, second rider: {total - first} calls')
    assert total == 1


@pytest.mark.django_db
def test_dispatch_evaluates_drivers_without_any_maps_request(capsys):
    """Candidate discovery is Redis GEO. No Maps SKU is involved.

    This is the single most important cost property of the architecture:
    the Maps bill does not grow with driver density, wave count or
    reassignment attempts.
    """
    from servers.ride import dispatch

    with patch('servers.ride.utils.requests.get') as http:
        radii = dispatch.wave_plan()
        waves = len(radii)

    with capsys.disabled():
        print(f'\n  dispatch waves              : {waves} (radii {radii} m)')
        print(f'  Maps requests per dispatch  : {http.call_count}')
    assert http.call_count == 0


@pytest.mark.django_db
def test_a_timeout_falls_back_and_is_not_cached(settings, _cache, capsys):
    """A hung provider must not stall the quote or poison the cache."""
    settings.GOOGLE_MAPS_API_KEY = 'test-key'
    import requests as _requests

    with patch('servers.ride.utils.requests.get',
               side_effect=_requests.Timeout('timed out')) as http:
        result = utils.get_google_maps_distance(*PICKUP, *DEST)

    with capsys.disabled():
        print(f'\n  on timeout -> {result}, cache entries: {len(_cache)}')
    assert result == (None, None)
    assert _cache == {}, 'a timeout must never be remembered as an answer'
    assert http.call_count == 1, 'no silent retry storm against a hung provider'


@pytest.mark.django_db
def test_the_outbound_call_is_bounded_by_a_timeout(settings):
    """An unbounded Maps call would hold a request worker open indefinitely."""
    settings.GOOGLE_MAPS_API_KEY = 'test-key'

    with patch('servers.ride.utils.requests.get', return_value=_Resp()) as http:
        utils.get_google_maps_distance(*PICKUP, *DEST)

    timeout = http.call_args.kwargs.get('timeout')
    assert timeout is not None and timeout <= 10, (
        f'Distance Matrix called with timeout={timeout!r}'
    )


@pytest.mark.django_db
def test_no_api_key_means_no_billable_traffic_at_all(settings, _cache, capsys):
    """What QA costs today: nothing. Maps is unconfigured and nothing is sent."""
    settings.GOOGLE_MAPS_API_KEY = ''

    with patch('servers.ride.utils.requests.get') as http:
        result = utils.get_google_maps_distance(*PICKUP, *DEST)

    with capsys.disabled():
        print(f'\n  unconfigured -> {result}, outbound calls: {http.call_count}')
    assert http.call_count == 0
    assert result == (None, None)
