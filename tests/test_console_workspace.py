"""Console Tier 1 (owner, 2026-11-03): rollups, places, needs action, usual range.

The rollups must count exactly what the overview counted from the trip table,
the place table must read a fixed number of queries whatever the city count,
and Needs action must show each kind only to the roles that hold it.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from servers.admin_access.models import GeoNode
from servers.command_centre import overview, rollups
from servers.command_centre.models import CityRollup
from test_command_centre import AUSTIN_POLYGON, WEEK, _no_cache_no_presence, world  # noqa: F401
from test_sos_support_zones_matrix import _zone, geo, operator  # noqa: F401

pytestmark = pytest.mark.django_db

PLACES = '/api/v1/command-centre/admin/places/'
NEEDS = '/api/v1/command-centre/admin/needs-action/'
PULSE = '/api/v1/command-centre/admin/pulse/'
VIEWING = '/api/v1/command-centre/admin/viewing/'


def _refresh(world):
    now = world['now']
    rollups.refresh(now - 2 * WEEK - timedelta(days=1), now)


class TestRollups:
    def test_the_rollups_count_what_the_overview_counts(self, geo, world):
        _refresh(world)
        hyd = geo['IN-TG-HYD']
        ctx = overview.city_contexts([hyd])
        now = world['now']
        bounds = {hyd.id: (now - timedelta(days=1), now)}
        direct = overview.measure_trips(ctx, bounds)[hyd.id]
        mine = rollups.figures(rollups.totals(bounds)[hyd.id])
        assert mine['requests'] == direct['requested'] == 10
        assert mine['unfulfilled'] == direct['unfulfilled'] == 2
        assert mine['completed'] == direct['completed'] == 6
        assert mine['eta_minutes'] == 10.0
        assert mine['collected_minor'] == {'INR': 210_000}
        assert mine['sos_raised'] == 1 and mine['tickets_raised'] == 1

    def test_refresh_is_idempotent(self, geo, world):
        _refresh(world)
        first = CityRollup.objects.count()
        _refresh(world)
        assert CityRollup.objects.count() == first

    def test_money_never_mixes_currencies(self, geo, world):
        _refresh(world)
        now = world['now']
        both = rollups.totals({geo['IN-TG-HYD'].id: (now - timedelta(days=1), now),
                               geo['US-TX-AUS'].id: (now - timedelta(days=1), now)})
        total = rollups.add(both[geo['IN-TG-HYD'].id], both[geo['US-TX-AUS'].id])
        assert total['money'] == {'INR': 210_000, 'USD': 4_000}


class TestPlaces:
    def test_global_is_one_row_per_country(self, geo, world):
        _refresh(world)
        client, _ = operator('super_admin', geo)
        data = client.get(f'{PLACES}?window=24h').json()['data']
        codes = {r['code'] for r in data['children'] + data['quiet']}
        assert {'IN', 'US'} <= codes
        india = next(r for r in data['children'] if r['code'] == 'IN')
        assert india['requests'] == 10 and india['sos_active'] == 1
        assert india['health'] == 'critical', 'an SOS nobody acknowledged'
        assert india['collected_minor'] == {'INR': 210_000}
        assert india['rates_reliable'] is False, '10 requests is not enough for a rate'
        assert {c['code'] for c in data['cities']} >= {'IN-TG-HYD', 'US-TX-AUS'}

    def test_drilling_down_lists_states_then_cities(self, geo, world):
        _refresh(world)
        client, _ = operator('super_admin', geo)
        states = client.get(f'{PLACES}?scope=IN&window=24h').json()['data']
        assert [r['code'] for r in states['children']] == ['IN-TG']
        assert states['breadcrumb'][-1]['code'] == 'IN'
        cities = client.get(f'{PLACES}?scope=IN-TG&window=24h').json()['data']
        assert cities['children'][0]['code'] == 'IN-TG-HYD'

    def test_money_is_withheld_without_finance(self, geo, world):
        _refresh(world)
        client, _ = operator('city_ops_manager', geo)
        data = client.get(f'{PLACES}?window=24h').json()['data']
        assert all('collected_minor' not in r for r in data['children'])

    def test_the_query_count_does_not_grow_with_cities(self, geo, world, settings):
        settings.COMMAND_CENTRE_PLACES_CACHE_SECONDS = 0
        _refresh(world)
        client, _ = operator('super_admin', geo)
        client.get(f'{PLACES}?window=today')  # warm the auth caches
        with CaptureQueriesContext(connection) as few:
            client.get(f'{PLACES}?window=today')
        state = geo['IN-TG']
        for i in range(12):
            city = GeoNode(kind='city', code=f'IN-TG-X{i:02d}', name=f'Extra {i}', parent=state,
                           status='active')
            city.save()
            _zone(f'IN-TG-X{i:02d}', city)
        with CaptureQueriesContext(connection) as many:
            client.get(f'{PLACES}?window=today')
        assert len(many.captured_queries) <= len(few.captured_queries) + 2, (
            len(few.captured_queries), len(many.captured_queries))


class TestNeedsAction:
    def test_safety_sees_the_sos_first(self, geo, world):
        client, _ = operator('safety_officer', geo)
        data = client.get(NEEDS).json()['data']
        first = data['items'][0]
        assert first['kind'] == 'sos' and first['severity'] == 'critical'
        assert first['ref'].startswith('SOS-') and first['link'].startswith('/safety-ops/incidents/')

    def test_an_analyst_sees_no_sos_and_no_money(self, geo, world):
        client, _ = operator('analyst', geo)
        data = client.get(NEEDS).json()['data']
        kinds = {i['kind'] for i in data['items']}
        assert 'sos' not in kinds and 'payout_failed' not in kinds and 'approval' not in kinds

    def test_stuck_trips_group_when_more_than_three(self, geo, world):
        from servers.safety.models import SafetyCountryConfig
        from servers.ride.models import Trip, TripStatus

        SafetyCountryConfig.objects.create(country='IN', version=1, is_active=True,
                                           stuck_no_location_minutes=10)
        st, _ = TripStatus.objects.get_or_create(status_code='in_progress')
        now = timezone.now()
        for _ in range(4):
            t = Trip.objects.get(pk=world['searching'].pk)
            t.pk = None
            t.status_id = st
            t.started_at = now - timedelta(minutes=40)
            t.last_driver_activity_at = now - timedelta(minutes=30)
            t.save()
        client, _ = operator('city_ops_manager', geo)
        items = client.get(NEEDS).json()['data']['items']
        grouped = [i for i in items if i['kind'] == 'stuck_trip']
        assert len(grouped) == 1 and grouped[0]['count'] == 4
        assert grouped[0]['link'] == '/trips?stuck=1&scope=IN-TG-HYD'

    def test_viewers_are_named(self, geo, world):
        a_client, _ = operator('safety_officer', geo)
        b_client, b = operator('safety_officer', geo)
        link = a_client.get(NEEDS).json()['data']['items'][0]['link']
        b_client.post(VIEWING, {'path': link}, format='json')
        item = a_client.get(NEEDS).json()['data']['items'][0]
        assert item['viewers'] == [b.full_name]


def test_usual_range_needs_three_weeks(geo, world):
    _refresh(world)
    client, _ = operator('super_admin', geo)
    data = client.get(f'{PULSE}?scope=IN-TG-HYD').json()['data']
    assert data['usual']['requests'] is None, 'one week of history is not a usual range'
    assert data['timezone'] == 'Asia/Kolkata'


def test_a_city_operator_lands_on_their_city(geo, world):
    _refresh(world)
    client, _ = operator('city_ops_manager', geo)
    data = client.get(f'{PLACES}?window=24h').json()['data']
    assert data['node']['code'] == 'IN-TG-HYD'
