"""The dashboard's "today": per country in its own currency, per live city what needs action, and no
test trip in any figure (owner, 22 Nov, items 3 and 11)."""

import pytest

from base.test_data import real_trips
from servers.admin_access.models import GeoNode
from servers.ride.models import Trip
from test_command_centre import world  # noqa: F401 - fixture
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

TODAY = '/api/v1/command-centre/admin/today/'


def _live(*codes):
    GeoNode.objects.filter(code__in=codes).update(status='active')


class TestTestTripsNeverCount:
    def test_a_seed_trip_is_left_out(self, geo, world):
        trip = Trip.objects.first()
        before = real_trips(Trip.objects.all()).count()
        Trip.objects.filter(pk=trip.pk).update(is_seed=True)
        assert real_trips(Trip.objects.all()).count() == before - 1

    def test_a_test_riders_trips_are_left_out(self, geo, world):
        rider = world['rider']
        theirs = Trip.objects.filter(user_id=rider).count()
        before = real_trips(Trip.objects.all()).count()
        type(rider).objects.filter(pk=rider.pk).update(is_test_account=True)
        assert theirs and real_trips(Trip.objects.all()).count() == before - theirs

    def test_a_test_drivers_trips_are_left_out(self, geo, world):
        user = world['hyd_driver'].user_id
        theirs = Trip.objects.filter(driver_id=world['hyd_driver']).count()
        before = real_trips(Trip.objects.all()).count()
        type(user).objects.filter(pk=user.pk).update(is_test_account=True)
        assert theirs and real_trips(Trip.objects.all()).count() == before - theirs


class TestToday:
    def test_one_tile_per_country_in_its_own_currency(self, geo, world):
        _live('IN-TG-HYD', 'US-TX-AUS')
        sa, _ = operator('super_admin', geo)

        data = sa.get(TODAY).json()['data']

        by = {c['code']: c for c in data['countries']}
        assert by['IN']['currency'] == 'INR' and by['US']['currency'] == 'USD'
        assert 'total' not in data and 'revenue_minor' not in data  # never one figure across countries
        assert {'requests', 'completed', 'completion_rate', 'sos_open', 'payouts_waiting',
                'revenue_minor'} <= set(by['IN'])

    def test_only_live_cities_list_their_actions_with_links(self, geo, world):
        _live('IN-TG-HYD')
        GeoNode.objects.filter(code='US-TX-AUS').update(status='draft')
        sa, _ = operator('super_admin', geo)

        data = sa.get(TODAY).json()['data']

        assert [c['code'] for c in data['cities']] == ['IN-TG-HYD']
        hyd = data['cities'][0]
        keys = {a['key'] for a in hyd['actions']}
        assert {'sos', 'payouts', 'kyc', 'stuck'} <= keys
        assert all(a['link'].startswith('/ops/') and 'scope=IN-TG-HYD' in a['link'] for a in hyd['actions'])
        sos = next(a for a in hyd['actions'] if a['key'] == 'sos')
        assert sos['count'] >= 1 and data['all_clear'] is False

    def test_money_is_left_out_without_finance_read(self, geo, world):
        _live('IN-TG-HYD')
        ops, _ = operator('city_ops_manager', geo)

        data = ops.get(TODAY).json()['data']

        assert data['money'] is False
        assert all(c['revenue_minor'] is None for c in data['countries'])
        assert all(a['key'] != 'payouts' for c in data['cities'] for a in c['actions'])

    def test_a_city_role_sees_only_its_city(self, geo, world):
        _live('IN-TG-HYD', 'US-TX-AUS')
        ops, _ = operator('city_ops_manager', geo)

        data = ops.get(TODAY).json()['data']

        assert [c['code'] for c in data['countries']] == ['IN']
        assert [c['code'] for c in data['cities']] == ['IN-TG-HYD']
