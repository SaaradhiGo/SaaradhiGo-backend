"""A whole trip costs at most 4 billable Google calls; the meter agrees."""

import threading
import time
from datetime import datetime, timezone
from unittest import mock

import pytest

from servers.ride import maps_meter, maps_utils, route_cache, utils
from servers.ride.views import AUTOCOMPLETE_CACHE_TTL_SECONDS

PICKUP = (17.3000, 78.3000)
DROP = (17.4900, 78.4900)
BILLABLE = ('places_details', 'routes_pro', 'routes_essentials', 'geocoding')


class _Resp:
    status_code = 200
    text = ''

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeGoogle:
    def __init__(self, route_delay=0.0):
        self.calls = []
        self.route_delay = route_delay
        self.lock = threading.Lock()

    def _log(self, url, **kw):
        with self.lock:
            self.calls.append((url, kw))

    def post(self, url, **kw):
        self._log(url, **kw)
        if 'computeRoutes' in url:
            time.sleep(self.route_delay)
            return _Resp({'routes': [{
                'distanceMeters': 24000, 'duration': '1800s',
                'polyline': {'encodedPolyline': '_p~iF~ps|U_ulLnnqC'}}]})
        return _Resp({'suggestions': [{'placePrediction': {
            'placeId': 'p1', 'text': {'text': 'Somewhere, City'}}}]})

    def get(self, url, **kw):
        self._log(url, **kw)
        return _Resp({'id': 'p1', 'displayName': {'text': 'X'},
                      'formattedAddress': 'X, City',
                      'location': {'latitude': 17.3, 'longitude': 78.3}})

    def urls(self, needle):
        return [c for c in self.calls if needle in c[0]]


@pytest.fixture
def env(settings):
    settings.GOOGLE_MAPS_API_KEY = 'test-key'
    settings.GOOGLE_PLACES_API_VERSION = 'new'
    settings.GOOGLE_ROUTING_API_VERSION = 'new'
    settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                                   'LOCATION': 'maps-budget'}}
    from django.core.cache import cache
    cache.clear()
    store = {}
    with mock.patch('servers.redis_client.get_cached_map_data', side_effect=store.get), \
            mock.patch('servers.redis_client.cache_map_data',
                       side_effect=lambda k, d, ttl_seconds: store.__setitem__(k, d) or True):
        yield store


def _today():
    return maps_meter.snapshot(1)['today']


@pytest.mark.django_db
def test_full_trip_makes_at_most_four_billable_calls(env):
    google = FakeGoogle(route_delay=0.3)
    with mock.patch('requests.get', side_effect=google.get), \
            mock.patch('requests.post', side_effect=google.post):
        # Two searches (pickup, drop): autocomplete under a session token, then details.
        for token in ('s-pick', 's-drop'):
            assert maps_utils.google_places_autocomplete('som', session_token=token)
            assert maps_utils.google_place_details('p1', session_token=token)

        # One quote fans out to six vehicle types at once.
        results, errors = [], []

        def quote():
            try:
                results.append(utils.validate_distance(None, None, *PICKUP, *DROP))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=quote) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        assert {r[1:3] for r in results} == {(24.0, 30.0)}

        # Accept stores the planned route: served from the same cached result.
        route, from_cache = route_cache.get_route(*PICKUP, *DROP)
        assert from_cache and route['polyline']

        # 300 driver pings and completion talk to Redis/DB only, never Google.
        from servers.driver import utils as driver_utils
        stream = mock.Mock()
        with mock.patch.object(driver_utils, 'redis_client', stream):
            for i in range(300):
                driver_utils.update_driver_location(1, 78.3 + i * 1e-5, 17.3)
        assert stream.xadd.call_count == 300

    meter = _today()
    billable = sum(meter[s] for s in BILLABLE)
    print(f'\n  outbound calls: {len(google.calls)}  billable: {billable}  meter: {meter}')
    assert billable <= 4
    assert len(google.urls('computeRoutes')) == 1
    assert meter['routes_pro'] == 1
    assert meter['places_details'] == 2
    assert meter['places_autocomplete_session'] == 2
    assert sum(meter.values()) == len(google.calls)


@pytest.mark.django_db
def test_lock_timeout_falls_back_to_one_call(env, monkeypatch):
    from django.core.cache import cache
    google = FakeGoogle()
    monkeypatch.setattr(route_cache, 'LOCK_WAIT_SECONDS', 0.2)
    monkeypatch.setattr(route_cache, 'POLL_SECONDS', 0.05)
    cache.add(route_cache.route_cache_key(*PICKUP, *DROP) + ':lock', 'other', 30)
    with mock.patch('requests.post', side_effect=google.post):
        route, from_cache = route_cache.get_route(*PICKUP, *DROP)
    assert route and not from_cache
    assert len(google.urls('computeRoutes')) == 1


@pytest.mark.django_db
def test_rounding_shares_one_entry(env):
    google = FakeGoogle()
    with mock.patch('requests.post', side_effect=google.post):
        route_cache.get_route(17.30001, 78.30001, *DROP)
        route_cache.get_route(17.29999, 78.29999, *DROP)
    assert len(google.urls('computeRoutes')) == 1


@pytest.mark.django_db
def test_routing_preference_sets_sku_and_body(env):
    google = FakeGoogle()
    with mock.patch('requests.post', side_effect=google.post):
        maps_utils.google_directions(*PICKUP, *DROP)
        maps_utils.google_directions(*PICKUP, *DROP, routing_preference='TRAFFIC_UNAWARE')
    prefs = [c[1]['json']['routingPreference'] for c in google.calls]
    assert prefs == ['TRAFFIC_AWARE', 'TRAFFIC_UNAWARE']
    meter = _today()
    assert meter['routes_pro'] == 1 and meter['routes_essentials'] == 1


@pytest.mark.django_db
def test_session_token_reaches_google(env):
    google = FakeGoogle()
    with mock.patch('requests.get', side_effect=google.get), \
            mock.patch('requests.post', side_effect=google.post):
        maps_utils.google_places_autocomplete('som', session_token='tok')
        maps_utils.google_place_details('p1', session_token='tok')
    assert google.urls('autocomplete')[0][1]['json']['sessionToken'] == 'tok'
    assert google.urls('places/p1')[0][1]['params']['sessionToken'] == 'tok'


@pytest.mark.django_db
def test_proxy_passes_query_session_token_and_short_ttl(env, rf):
    from servers.ride import views
    google = FakeGoogle()
    request = rf.post('/ride/maps/geocode/?session_token=tok', {'input': 'abc'},
                      content_type='application/json')
    with mock.patch('requests.post', side_effect=google.post), \
            mock.patch.object(views, 'check_maps_rate_limit', return_value=True), \
            mock.patch.object(views, 'cache_map_data') as put:
        resp = views.proxy_geocode(request)
    assert resp.status_code == 200
    assert google.calls[0][1]['json']['sessionToken'] == 'tok'
    assert put.call_args.kwargs['ttl_seconds'] == AUTOCOMPLETE_CACHE_TTL_SECONDS <= 3600


def test_meter_keys_and_ttl():
    from django.core.cache import cache
    with mock.patch.object(cache, 'add') as add, mock.patch.object(cache, 'incr') as incr:
        maps_meter.record('geocoding')
    day = datetime.now(timezone.utc).strftime('%Y%m%d')
    assert add.call_args.args == (f'maps:meter:{day}:geocoding', 0, 40 * 24 * 3600)
    assert incr.call_args.args == (f'maps:meter:{day}:geocoding',)
