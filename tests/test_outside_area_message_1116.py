"""Launch round 3, item 2: a drop just outside the service area names the area.

A drop past Treasure Valley's outline said "check your connection" in the apps. The server now
answers OUT_OF_SERVICE_AREA with "This place is outside our service area (Treasure Valley)."
"""
import pytest

from base.service_area import (
    OUT_OF_SERVICE_AREA, area_name_near, outside_message, validate_service_area_detail,
)
from servers.pricing.models import ServiceZone

TV = [[-116.66, 43.52], [-116.66, 43.65], [-116.4, 43.7], [-116.1, 43.72], [-116.05, 43.6],
      [-116.15, 43.52], [-116.33, 43.44], [-116.5, 43.45], [-116.66, 43.52]]
BOISE = (43.615, -116.20)
CALDWELL = (43.664, -116.687)  # College of Idaho: inside the rectangle, outside the outline


@pytest.fixture
def tv(db):
    zone, _ = ServiceZone.objects.update_or_create(code='US-ID-TV', defaults=dict(
        name='Treasure Valley', country='US', state_code='ID', city='Boise',
        zone_type='city', priority=10, is_active=True, parent=None,
        polygon_geojson={'type': 'Polygon', 'coordinates': [TV]}))
    return zone


def test_drop_outside_names_the_pickup_area(tv):
    ok, code, message = validate_service_area_detail(*BOISE, *CALDWELL)
    assert (ok, code) == (False, OUT_OF_SERVICE_AREA)
    assert message == 'This place is outside our service area (Treasure Valley).'


def test_pickup_outside_names_the_nearest_area(tv):
    assert area_name_near(*CALDWELL) == 'Treasure Valley'


def test_a_sub_zone_reports_its_top_area(tv):
    ServiceZone.objects.create(
        code='US-ID-TV-BOI', name='Boise Airport', country='US', state_code='ID', city='Boise',
        zone_type='airport', priority=100, is_active=True, parent=tv,
        polygon_geojson={'type': 'Polygon', 'coordinates': [[
            [-116.24, 43.55], [-116.24, 43.58], [-116.2, 43.58], [-116.2, 43.55], [-116.24, 43.55]]]})
    assert area_name_near(43.565, -116.22) == 'Treasure Valley'


def test_without_a_name_the_message_still_reads():
    assert outside_message('') == 'This place is outside our service area.'


def test_quote_endpoint_uses_the_message(tv, auth_client_rider):
    client, _ = auth_client_rider
    r = client.post('/api/v1/pricing/quote/', {
        'pickup_lat': BOISE[0], 'pickup_long': BOISE[1],
        'destination_lat': CALDWELL[0], 'destination_long': CALDWELL[1], 'vehicle_type': 'sedan',
    }, format='json')
    assert r.status_code == 400, r.content[:300]
    err = r.json()['error']
    assert err['code'] == OUT_OF_SERVICE_AREA
    assert err['message'] == 'This place is outside our service area (Treasure Valley).'
