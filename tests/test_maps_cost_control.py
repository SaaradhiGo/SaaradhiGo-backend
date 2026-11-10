"""Maps cost control (owner, 9 Nov): one route per journey, the ETA worked out on our server,
every call metered, a budget guard. A fake Google answers with a real straight route between the
points it is asked for, so distances and the polyline are honest.
"""
import math
from types import SimpleNamespace
from unittest import mock

import pytest

from servers.ride import maps_budget, maps_meter, route_cache, route_eta, eta, utils

PICKUP = (17.4400, 78.3800)
DROP = (17.5400, 78.3800)          # ~11.1 km due north


def _enc_value(v):
    v = ~(v << 1) if v < 0 else v << 1
    out = ''
    while v >= 0x20:
        out += chr((0x20 | (v & 0x1F)) + 63)
        v >>= 5
    return out + chr(v + 63)


def encode(points):
    out, plat, plng = '', 0, 0
    for lat, lng in points:
        la, ln = round(lat * 1e5), round(lng * 1e5)
        out += _enc_value(la - plat) + _enc_value(ln - plng)
        plat, plng = la, ln
    return out


def metres(a, b):
    return route_cache._metres(*a, *b)


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
    """computeRoutes: a straight road at 22 km/h between the requested points."""

    def __init__(self):
        self.routes = 0

    def post(self, url, **kw):
        assert 'computeRoutes' in url, url
        self.routes += 1
        body = kw['json']
        o = body['origin']['location']['latLng']
        d = body['destination']['location']['latLng']
        a, b = (o['latitude'], o['longitude']), (d['latitude'], d['longitude'])
        pts = [(a[0] + (b[0] - a[0]) * i / 20, a[1] + (b[1] - a[1]) * i / 20) for i in range(21)]
        dist = metres(a, b)
        return _Resp({'routes': [{'distanceMeters': int(dist), 'duration': f'{int(dist / (22 / 3.6))}s',
                                  'polyline': {'encodedPolyline': encode(pts)}}]})


@pytest.fixture
def env(settings):
    settings.GOOGLE_MAPS_API_KEY = 'test-key'
    settings.GOOGLE_ROUTING_API_VERSION = 'new'
    settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                                   'LOCATION': 'maps-cost'}}
    from django.core.cache import cache
    cache.clear()
    store, now = {}, {'t': 1_800_000_000.0}
    google = FakeGoogle()
    with mock.patch('servers.redis_client.get_cached_map_data', side_effect=store.get), \
            mock.patch('servers.redis_client.cache_map_data',
                       side_effect=lambda k, d, ttl_seconds: store.__setitem__(k, d) or True), \
            mock.patch('requests.post', side_effect=google.post), \
            mock.patch.object(route_cache, 'clock', lambda: now['t']), \
            mock.patch.object(route_eta, 'clock', lambda: now['t']):
        yield SimpleNamespace(google=google, now=now, store=store)


def quote(o=PICKUP, d=DROP):
    return utils.validate_distance(None, None, *o, *d)


def north(point, m):
    return point[0] + m / 111320.0, point[1]


# ---------------------------------------------------------------------------
# A4: one routing call per journey, reused
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestOneRoutePerJourney:
    def test_ten_quotes_in_30_minutes_with_reopen_keep_looking_and_class_switches(self, env):
        for i in range(10):            # every 3 minutes: reopen, retry, switch class, etc.
            quote()
            env.now['t'] += 170
        assert env.google.routes == 1

    def test_two_riders_same_journey(self, env):
        quote()
        quote()                        # a second rider: the key carries no rider or class
        assert env.google.routes == 1

    def test_price_moves_with_demand_and_no_extra_call(self, env):
        from servers.pricing.services import quote_fare

        km, minutes = quote()[1:3]
        with mock.patch('servers.pricing.services.compute_surge_multiplier',
                        return_value=__import__('decimal').Decimal('1.00')):
            low = quote_fare(km, minutes, 'sedan')['total_fare']
        km2, minutes2 = quote()[1:3]
        with mock.patch('servers.pricing.services.compute_surge_multiplier',
                        return_value=__import__('decimal').Decimal('1.30')):
            high = quote_fare(km2, minutes2, 'sedan')['total_fare']
        assert high > low and env.google.routes == 1

    def test_drop_moved_50m_no_call_500m_one_call(self, env):
        quote()
        quote(d=north(DROP, 50))
        assert env.google.routes == 1
        quote(d=north(DROP, 500))
        assert env.google.routes == 2

    def test_an_expired_route_is_fetched_again_and_the_quote_is_right(self, env):
        km = quote()[1]
        env.now['t'] += 31 * 60
        km2 = quote()[1]
        assert env.google.routes == 2 and km2 == km and abs(km - 11.1) < 0.2

    def test_cache_answers_are_metered_for_the_hit_rate(self, env):
        quote(); quote(); quote()
        summary = maps_budget.month_summary()
        assert summary['month']['route_cache_miss'] == 1
        assert summary['month']['route_cache_hit'] == 2
        assert summary['route_cache_hit_rate'] == pytest.approx(0.667, abs=0.001)


# ---------------------------------------------------------------------------
# A2: a 28-minute trip makes at most 4 routing calls
# ---------------------------------------------------------------------------

def _trip(driver_at):
    pickup_route, _ = route_cache.get_route(*driver_at, *PICKUP)         # the one pickup-leg call
    drop_route, _ = route_cache.get_route(*PICKUP, *DROP)                # cached at quote time
    return SimpleNamespace(
        id=4242, driver_id_id=7, zone=SimpleNamespace(country='IN'), status_id=SimpleNamespace(status_code='accepted'),
        pickup_lat=PICKUP[0], pickup_long=PICKUP[1], destination_lat=DROP[0], destination_long=DROP[1],
        pickup_route_polyline=pickup_route['polyline'], pickup_route_distance_m=int(pickup_route['distance_m']),
        pickup_route_duration_s=int(pickup_route['duration_s']), pickup_route_at=None, accepted_at=None,
        planned_route_polyline=drop_route['polyline'], planned_route_distance_m=int(drop_route['distance_m']),
        planned_route_duration_s=int(drop_route['duration_s']), started_at=None)


@pytest.mark.django_db
def test_simulated_28_minute_trip_makes_at_most_4_routing_calls(env):
    from datetime import datetime, timezone as tz

    quote()                                             # the rider's quote: call 1
    start = (PICKUP[0] - 2000 / 111320.0, PICKUP[1])    # driver 2 km south of the pickup
    trip = _trip(start)                                 # accept: pickup leg, call 2
    calls_after_accept = env.google.routes
    sync = lambda trip_id, leg, lat, lng, t_lat, t_lng, country='': route_eta.reroute_now(  # noqa: E731
        trip_id, leg, lat, lng, t_lat, t_lng, country)
    etas = []
    with mock.patch('servers.ride.tasks.reroute_trip.delay', side_effect=sync):
        # 5 minutes to the pickup along the route, every 15 s.
        for i in range(20):
            at = (start[0] + (PICKUP[0] - start[0]) * i / 20, start[1])
            etas.append(eta.compute(trip, driver_at=at))
            env.now['t'] += 15
        trip.status_id.status_code = 'in_progress'
        trip.started_at = datetime.fromtimestamp(env.now['t'], tz.utc)
        # 28 minutes to the drop (112 ticks); from tick 40 to 50 a 300 m detour to the east.
        for i in range(112):
            lat = PICKUP[0] + (DROP[0] - PICKUP[0]) * i / 112
            lng = PICKUP[1] + (300 / (111320.0 * math.cos(math.radians(lat))) if 40 <= i < 50 else 0)
            etas.append(eta.compute(trip, driver_at=(lat, lng)))
            env.now['t'] += 15
    total = env.google.routes
    print(f'\n  routing calls: quote+accept {calls_after_accept}, whole trip {total}')
    assert total <= 4
    assert sum(1 for e in etas if e and e['source'] == 'route') > 100
    drop_etas = [e['drop_eta_min'] for e in etas[20:] if e]
    assert drop_etas[0] >= 25 and drop_etas[-1] <= 2


@pytest.mark.django_db
def test_the_route_eta_counts_down_with_no_call(env):
    quote()
    trip = _trip((PICKUP[0] - 1000 / 111320.0, PICKUP[1]))
    before = env.google.routes
    first = eta.compute(trip, driver_at=(PICKUP[0] - 1000 / 111320.0, PICKUP[1]))
    later = eta.compute(trip, driver_at=(PICKUP[0] - 300 / 111320.0, PICKUP[1]))
    assert first['source'] == 'route' and first['pickup_eta_min'] == 3     # 1 km at 22 km/h
    assert later['pickup_eta_min'] == 1 and env.google.routes == before


# ---------------------------------------------------------------------------
# A1: metering per SKU, surface and trip; A6: the budget guard
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_calls_are_counted_per_surface_and_trip(env):
    with maps_meter.context(surface='rider_app', trip_id=99, country='IN'):
        route_cache.get_route(*PICKUP, *DROP)
    assert maps_meter.trip_counts(99) == {'route_cache_miss': 1, 'routes_pro': 1}
    from servers.ride.maps_models import MapsUsage
    assert set(MapsUsage.objects.values_list('surface', flat=True)) == {'rider_app'}


@pytest.mark.django_db
def test_the_web_reports_its_map_loads(client):
    r = client.post('/api/v1/ride/maps/meter/', {'sku': 'dynamic_maps', 'surface': 'rider_web'},
                    content_type='application/json')
    assert r.status_code == 200
    assert maps_budget.month_summary()['month']['dynamic_maps'] == 1
    bad = client.post('/api/v1/ride/maps/meter/', {'sku': 'routes_pro', 'surface': 'rider_web'},
                      content_type='application/json')
    assert bad.status_code == 400


@pytest.mark.django_db
def test_list_price_cost_and_the_free_allowance(db):
    from decimal import Decimal
    assert maps_budget.list_cost_usd({'routes_pro': 1, 'places_autocomplete_session': 3,
                                      'places_details': 1}) == Decimal('0.0235')
    # 9,999 calls already this month: 2 more pay for 1.
    assert maps_budget.cost_usd({'places_details': 2}, month_counts={'places_details': 9999}) \
        == Decimal('0.0050')


@pytest.mark.django_db
def test_budget_alerts_and_degrade_in_order(env):
    from servers.command_centre.models import MAPS_BUDGET, OperatorAlert
    from servers.ride.maps_models import MapsBudget, MapsSkuPrice

    MapsSkuPrice.objects.filter(sku='routes_pro').update(free_monthly_calls=0)
    MapsBudget.objects.create(country='IN', monthly_usd='0.10')
    for _ in range(8):
        maps_meter.record('routes_pro', country='IN')            # $0.08: 80%
    assert maps_budget.check_budgets()['IN'] == {'spent_usd': '0.0800', 'percent': 80, 'level': 0}
    assert OperatorAlert.objects.get(kind=MAPS_BUDGET).severity == 'warning'
    for _ in range(3):
        maps_meter.record('routes_pro', country='IN')            # $0.11: over
    assert maps_budget.check_budgets()['IN']['level'] == 3
    assert route_cache.ttl_seconds('IN') == 3600                 # longer cache
    from servers.platform_ops.app_config import _maps_block
    assert _maps_block('IN')['autocomplete_min_chars'] == 5
    quote()
    trip = _trip((PICKUP[0] - 1000 / 111320.0, PICKUP[1]))
    assert eta.compute(trip, driver_at=(PICKUP[0] - 1000 / 111320.0, PICKUP[1]))['source'] == 'straight_line'
    MapsBudget.objects.filter(country='IN').update(degrade_straight_line_eta=False)
    assert maps_budget.check_budgets()['IN']['level'] == 1       # a step stops the ones after it
