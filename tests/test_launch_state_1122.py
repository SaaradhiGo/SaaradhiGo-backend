"""What "live" means, in the console's words, and blockers that say who fixes them (22 Nov).

Owner brief: "Only cities whose launch gate is open count as live. On QA that means Hyderabad,
plus Treasure Valley shown as 'coming soon'. Vijayawada, Visakhapatnam and Warangal are 'not
launched'." And: "A live city can't have an unresolved blocker; show what it is and who fixes it,
or fix it."
"""

import pytest

from servers.admin_access import readiness
from servers.admin_access.models import ACTIVE, DRAFT, PAUSED, GeoNode
from servers.platform_ops.markets import launch_state
from test_sos_support_zones_matrix import geo  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db


def _zone_for(node, active=True):
    from servers.pricing.models import ServiceZone

    return ServiceZone.objects.create(
        code=f'Z-{node.code}', name=node.name, country=node.code[:2], zone_type='city',
        polygon_geojson={'type': 'Polygon', 'coordinates': [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        geo_node=node, is_active=active)


class TestLaunchState:
    def test_live_is_only_through_the_gate(self, geo):
        hyd = geo['IN-TG-HYD']
        GeoNode.objects.filter(pk=hyd.pk).update(status=ACTIVE)
        hyd.refresh_from_db()
        assert launch_state(hyd) == {'launch_state': 'live', 'launch_label': 'Live'}

    def test_a_draft_city_riders_can_see_is_coming_soon(self, geo):
        city = geo['US-TX-AUS']
        GeoNode.objects.filter(pk=city.pk).update(status=DRAFT)
        city.refresh_from_db()
        _zone_for(city)
        assert launch_state(city)['launch_label'] == 'Coming soon'

    def test_a_draft_city_with_no_active_zone_is_not_launched(self, geo):
        from servers.pricing.models import ServiceZone

        city = geo['IN-TG-WGL']
        GeoNode.objects.filter(pk=city.pk).update(status=DRAFT)
        ServiceZone.objects.filter(geo_node=city).update(is_active=False)
        city.refresh_from_db()
        assert launch_state(city)['launch_label'] == 'Not launched'

    def test_paused(self, geo):
        city = geo['IN-TG-WGL']
        GeoNode.objects.filter(pk=city.pk).update(status=PAUSED)
        city.refresh_from_db()
        assert launch_state(city)['launch_label'] == 'Paused'


class TestBackToNotLaunched:
    def test_a_city_that_never_ran_a_trip_can_go_back_to_not_launched(self, geo):
        city = geo['IN-TG-WGL']
        GeoNode.objects.filter(pk=city.pk).update(status=ACTIVE)
        city.refresh_from_db()

        node, changed = readiness.set_city_status(city, DRAFT, actor=None,
                                                  reason='Opened by a QA seed; not launched')

        assert changed and node.status == DRAFT

    def test_a_reason_is_required(self, geo):
        with pytest.raises(ValueError):
            readiness.set_city_status(geo['IN-TG-WGL'], DRAFT, actor=None, reason='')


class TestBlockersSayWhoFixesThem:
    def test_every_item_has_an_owner(self, geo):
        items = readiness.city_readiness(geo['IN-TG-HYD'])
        assert items and all(i['owner'] for i in items)
        assert {i['key']: i['owner'] for i in items}['safety_coverage'] == 'Safety'

    def test_a_cash_city_without_a_gateway_has_a_warning_not_a_blocker(self, geo, settings):
        city = geo['IN-TG-HYD']
        _zone_for(city)
        item = next(i for i in readiness.city_readiness(city) if i['key'] == 'payment_gateway')
        if item['status'] == 'ok':
            pytest.skip('a gateway is configured in this test settings')
        assert item['blocking'] is False and item['status'] == 'warning'
        assert 'Cash only' in item['detail'] and item['owner'] == 'Engineering'
