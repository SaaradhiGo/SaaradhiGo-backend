"""Seats per vehicle class reach the rider (2026-11-06).

The rider app showed seats from a table of its own. The class table now holds
them, and both places a rider sees a class -- the public vehicle-types list and
the per-class quote -- carry the value, or None when a class has none set.
Display only: no fare depends on it.
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.driver.models import VehicleType
from servers.rider.models import Rider

pytestmark = pytest.mark.django_db

User = get_user_model()


def _zone_point():
    from servers.pricing.models import ServiceZone

    zone = ServiceZone.objects.filter(is_active=True).first()
    if zone is None:
        pytest.skip('no service zone in this database')
    ring = ((zone.polygon_geojson or {}).get('coordinates') or [[]])[0]
    if not ring:
        pytest.skip('zone has no polygon')
    return (sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring))


def test_vehicle_types_carry_seats_or_none():
    lat, lng = _zone_point()
    r = APIClient().get('/api/v1/pricing/vehicle-types/', {'lat': lat, 'lng': lng})
    offered = r.json()['data']['vehicle_types']
    if not offered:
        pytest.skip('no priced class in this database')

    first, *rest = offered
    VehicleType.objects.filter(type=first['type']).update(seats=5)
    for other in rest:
        VehicleType.objects.filter(type=other['type']).update(seats=None)

    data = APIClient().get(
        '/api/v1/pricing/vehicle-types/', {'lat': lat, 'lng': lng}).json()['data']
    by_type = {v['type']: v for v in data['vehicle_types']}
    assert by_type[first['type']]['seats'] == 5
    for other in rest:
        assert by_type[other['type']]['seats'] is None


def test_the_quote_carries_the_class_seats():
    user = User.objects.create_user(phone_number='+919566000061', role='rider')
    Rider.objects.create(user_id=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    VehicleType.objects.update_or_create(type='sedan', defaults={'seats': 4})

    resp = client.post('/api/v1/ride/estimate-fare/', {
        'pickup_lat': '17.4450', 'pickup_long': '78.3800',
        'destination_lat': '17.4660', 'destination_long': '78.3800',
        'distance_km': '2.4', 'duration_min': '8', 'vehicle_type': 'sedan',
    }, format='json')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    payload = body.get('data', body)
    assert payload['seats'] == 4


def test_the_migration_fills_known_classes_only():
    from importlib import import_module

    mod = import_module('servers.driver.migrations.0039_vehicletype_seats')
    assert mod.SEATS['auto'] == 3
    assert mod.SEATS['suv'] == 6
    assert 'orphan-category' not in mod.SEATS
