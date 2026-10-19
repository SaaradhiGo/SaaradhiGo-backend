"""Quoting a ride must not buy the same Distance Matrix answer four times.

THE DEFECT
`estimate_fare` recomputes distance server-side on every quote, which is
correct -- trusting the client's distance was the original fare-tampering
vector. But the website quotes EVERY vehicle category at once:

    Promise.all(service.vehicle_types.map((v) => quoteFare({...})))

Four categories, four `estimate-fare` calls, four trips through
`get_google_maps_distance`, and no cache. Four identical, separately
billed Distance Matrix requests for the same two points -- every time a
rider taps "Show fares", including on bookings they never complete.

The road distance between two fixed points does not change across those
four calls, so this caches the answer on the coordinate pair. Duration
drifts with traffic, hence a short TTL rather than a long one.

The fare is unaffected: the same inputs produce the same distance, so the
same fare. This removes requests, not arithmetic.
"""

from unittest.mock import patch

import pytest

from servers.ride import utils


class _Resp:
    """A Routes API computeRoutes reply.

    The legacy Distance Matrix endpoint cannot be enabled on a Google Cloud
    project created after Google's cut-off, so the fare path now calls Routes.
    What these tests assert -- one billable call per quote, no cache
    poisoning, a bounded timeout -- is unchanged; only the wire shape is.
    """

    status_code = 200
    text = ''

    def __init__(self, km, minutes):
        self._km = km
        self._minutes = minutes

    def json(self):
        return {
            'routes': [{
                'distanceMeters': int(self._km * 1000),
                'duration': f'{int(self._minutes * 60)}s',
            }],
        }


@pytest.fixture
def _memory_cache():
    """Stand in for Redis, so the test asserts on behaviour not on a server."""
    store = {}

    def get(key):
        return store.get(key)

    def put(key, data, ttl_seconds):
        store[key] = data
        return True

    with patch('servers.redis_client.get_cached_map_data', side_effect=get), \
         patch('servers.redis_client.cache_map_data', side_effect=put):
        yield store


PICKUP = (16.5062, 80.6480)
DEST = (16.5190, 80.6305)


@pytest.mark.django_db
def test_four_categories_buy_one_distance_matrix_call(settings, _memory_cache):
    settings.GOOGLE_MAPS_API_KEY = 'test-key'

    with patch('servers.ride.utils.requests.post',
               return_value=_Resp(2.4, 7.0)) as http:
        results = [
            utils.get_google_maps_distance(*PICKUP, *DEST)
            for _ in range(4)  # auto, hatchback, sedan, suv
        ]

    assert http.call_count == 1, (
        f'quoting four categories made {http.call_count} billable Distance '
        f'Matrix calls for one origin/destination pair'
    )
    assert results == [(2.4, 7.0)] * 4, 'every category must get the same distance'


@pytest.mark.django_db
def test_a_different_destination_is_a_different_call(settings, _memory_cache):
    settings.GOOGLE_MAPS_API_KEY = 'test-key'

    with patch('servers.ride.utils.requests.post',
               return_value=_Resp(2.4, 7.0)) as http:
        utils.get_google_maps_distance(*PICKUP, *DEST)
        utils.get_google_maps_distance(*PICKUP, 16.6000, 80.7000)

    assert http.call_count == 2, 'a different route must not reuse a cached answer'


@pytest.mark.django_db
def test_nudging_the_pin_within_a_metre_reuses_the_answer(settings, _memory_cache):
    """5dp is ~1.1m. A map pin settling must not re-buy the route."""
    settings.GOOGLE_MAPS_API_KEY = 'test-key'

    with patch('servers.ride.utils.requests.post',
               return_value=_Resp(2.4, 7.0)) as http:
        utils.get_google_maps_distance(16.50620, 80.64800, *DEST)
        utils.get_google_maps_distance(16.506201, 80.648001, *DEST)

    assert http.call_count == 1


@pytest.mark.django_db
def test_a_dead_cache_still_answers(settings):
    """The cache is advisory. A Redis outage must not fail a quote."""
    settings.GOOGLE_MAPS_API_KEY = 'test-key'

    with patch('servers.redis_client.get_cached_map_data',
               side_effect=RuntimeError('redis down')), \
         patch('servers.redis_client.cache_map_data',
               side_effect=RuntimeError('redis down')), \
         patch('servers.ride.utils.requests.post', return_value=_Resp(2.4, 7.0)):
        assert utils.get_google_maps_distance(*PICKUP, *DEST) == (2.4, 7.0)


@pytest.mark.django_db
def test_nothing_is_cached_when_google_fails(settings, _memory_cache):
    """A failure must not be remembered as if it were an answer."""
    settings.GOOGLE_MAPS_API_KEY = 'test-key'

    class _Bad:
        status_code = 500
        text = 'upstream error'

        def json(self):
            return {}

    with patch('servers.ride.utils.requests.post', return_value=_Bad()):
        assert utils.get_google_maps_distance(*PICKUP, *DEST) == (None, None)

    assert _memory_cache == {}


@pytest.mark.django_db
def test_no_key_means_no_request_and_no_cache(settings, _memory_cache):
    settings.GOOGLE_MAPS_API_KEY = ''

    with patch('servers.ride.utils.requests.post') as http:
        assert utils.get_google_maps_distance(*PICKUP, *DEST) == (None, None)

    assert http.call_count == 0
    assert _memory_cache == {}
