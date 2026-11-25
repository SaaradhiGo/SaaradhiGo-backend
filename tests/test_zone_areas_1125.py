"""Pickup points, restricted areas and a service area per class (owner ruling 25 Nov): nothing by
default; once a second person approves an area, the quote and the booking gate respect it."""
import pytest

from base.service_area import validate_service_area_detail
from servers.pricing import zone_areas
from servers.pricing.zone_area_models import ZoneArea
from test_finance_actions import approve_request, body_of, checker, error_code, post
from test_pricing_module import BASE, geo, world  # noqa: F401

pytestmark = pytest.mark.django_db
# A small square inside Hyderabad's test zone, and points inside and outside it.
SQUARE = {'type': 'Polygon', 'coordinates': [[[78.47, 17.38], [78.48, 17.38], [78.48, 17.39], [78.47, 17.39], [78.47, 17.38]]]}
INSIDE, GATE, OUTSIDE = (17.385, 78.475), (17.3852, 78.4752), (17.40, 78.49)


def add(world, **body):
    maker, _ = world.super
    r = post(maker, f'{BASE}/zones/{world.hyd.id}/areas/', {'name': 'Charminar square', 'polygon_geojson': SQUARE,
                                                            'reason': 'Festival crowding', **body})
    assert r.status_code == 202, r.content[:300]
    chk, _ = checker()
    assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
    return ZoneArea.objects.get(zone=world.hyd, is_active=True, kind=body['kind'])


class TestZoneAreas:
    def test_nothing_set_nothing_changes(self, world):
        assert zone_areas.check_points(*INSIDE, *OUTSIDE)[0] is True
        assert zone_areas.class_offered_at(world.hyd, 'sedan', *INSIDE) is True

    def test_a_restricted_area_refuses_pickup_and_drop_after_approval(self, world):
        maker, _ = world.super
        r = post(maker, f'{BASE}/zones/{world.hyd.id}/areas/preview/', {'kind': 'restricted', 'name': 'Charminar square',
                                                                       'polygon_geojson': SQUARE}, expect=200)
        assert 'Restricted area: Charminar square' in body_of(r)['changes'][0]['to']
        assert not ZoneArea.objects.exists()
        add(world, kind='restricted')
        ok, code, msg, _ = zone_areas.check_points(*INSIDE, *OUTSIDE)
        assert (ok, code) == (False, 'AREA_RESTRICTED') and 'Pickups' in msg
        assert zone_areas.check_points(*OUTSIDE, *INSIDE)[1] == 'AREA_RESTRICTED'

    def test_pickup_points_inside_an_area(self, world):
        add(world, kind='pickup', points=[{'name': 'Gate 3', 'lat': GATE[0], 'lng': GATE[1]}])
        ok, code, msg, extra = zone_areas.check_points(17.3875, 78.4775, *OUTSIDE)
        assert code == 'PICKUP_POINT_REQUIRED' and 'Gate 3' in msg and extra['pickup_points'][0]['name'] == 'Gate 3'
        assert zone_areas.check_points(*GATE, *OUTSIDE)[0] is True

    def test_a_class_area_limits_that_class_only(self, world):
        add(world, kind='class_area', vehicle_type='sedan')
        assert zone_areas.class_offered_at(world.hyd, 'sedan', *INSIDE) is True
        assert zone_areas.class_offered_at(world.hyd, 'sedan', *OUTSIDE) is False
        assert zone_areas.class_offered_at(world.hyd, 'auto', *OUTSIDE) is True

    def test_removal_is_also_approved(self, world):
        area = add(world, kind='restricted')
        maker, _ = world.super
        r = post(maker, f'{BASE}/zones/{world.hyd.id}/areas/', {'remove': area.id, 'reason': 'Festival over'}, expect=202)
        chk, _ = checker()
        approve_request(chk, body_of(r)['approval_request']['id'])
        area.refresh_from_db()
        assert area.is_active is False and zone_areas.check_points(*INSIDE, *OUTSIDE)[0] is True

    def test_bad_input_and_another_markets_class_are_refused(self, world):
        maker, _ = world.super
        r = post(maker, f'{BASE}/zones/{world.hyd.id}/areas/preview/', {'kind': 'class_area', 'name': 'XL only',
                                                                       'polygon_geojson': SQUARE, 'vehicle_type': 'xl'})
        assert r.status_code == 400 and error_code(r) == 'INVALID_CLASS'
        r = post(maker, f'{BASE}/zones/{world.hyd.id}/areas/preview/', {'kind': 'pickup', 'name': 'Airport',
                                                                       'polygon_geojson': SQUARE, 'points': []})
        assert r.status_code == 400 and error_code(r) == 'INVALID_POINTS'

    def test_the_shared_gate_uses_them(self, world):
        add(world, kind='restricted')
        ok, code, _ = validate_service_area_detail(*INSIDE, *OUTSIDE)
        assert (ok, code) == (False, 'AREA_RESTRICTED')
