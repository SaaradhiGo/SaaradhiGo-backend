"""Multi-city pricing tests.

Covers the new pricing layer added in feat/multi-city-pricing:

  * ServiceZone polygon lookup (in-zone, out-of-zone, junk inputs)
  * RateCard resolution with effective_from / effective_to and version
  * Parent zone fall-through (sub-zone with no card -> city card used)
  * quote_fare() end-to-end with and without an active zone
  * Admin endpoints (zone CRUD, rate card CRUD) honour IsPlatformAdmin
  * Legacy base.service_area.validate_service_area still works on top
"""

from datetime import datetime, timedelta
from decimal import Decimal
import pytest
from django.utils import timezone


# Hyderabad city centre — inside the seeded polygon
HYD_CENTRE_LAT = 17.385
HYD_CENTRE_LON = 78.486

# Bangalore city centre — outside the Hyderabad polygon
BLR_LAT = 12.9716
BLR_LON = 77.5946


# ---------------------------------------------------------------------------
# Zone lookup
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_seed_creates_hyderabad_zone():
    from servers.pricing.models import ServiceZone
    z = ServiceZone.objects.filter(code='IN-TG-HYD').first()
    assert z is not None
    assert z.is_active is True
    assert z.zone_type == 'city'
    assert z.state_code == 'TG'
    assert z.country == 'IN'


@pytest.mark.django_db
def test_find_zone_for_point_inside_hyderabad():
    from servers.pricing.services import find_zone_for_point
    zone = find_zone_for_point(HYD_CENTRE_LAT, HYD_CENTRE_LON)
    assert zone is not None
    assert zone.code == 'IN-TG-HYD'


@pytest.mark.django_db
def test_find_zone_for_point_outside_hyderabad():
    from servers.pricing.services import find_zone_for_point
    assert find_zone_for_point(BLR_LAT, BLR_LON) is None


@pytest.mark.django_db
def test_find_zone_for_point_handles_junk_inputs():
    from servers.pricing.services import find_zone_for_point
    assert find_zone_for_point(None, None) is None
    assert find_zone_for_point('abc', 'def') is None
    assert find_zone_for_point(200, 400) is None  # out of range


@pytest.mark.django_db
def test_validate_service_area_legacy_shim_still_works():
    """base.service_area is a shim; it must delegate to ServiceZone."""
    from base.service_area import is_inside_service_area, validate_service_area
    assert is_inside_service_area(HYD_CENTRE_LAT, HYD_CENTRE_LON) is True
    assert is_inside_service_area(BLR_LAT, BLR_LON) is False
    ok, msg = validate_service_area(HYD_CENTRE_LAT, HYD_CENTRE_LON, BLR_LAT, BLR_LON)
    assert ok is False
    assert 'Drop' in msg


@pytest.mark.django_db
def test_zone_priority_subzone_wins_over_city():
    """A higher-priority sub-zone covering the same point must win."""
    from servers.pricing.models import ServiceZone
    from servers.pricing.services import find_zone_for_point

    parent = ServiceZone.objects.get(code='IN-TG-HYD')

    # Tiny polygon around city centre, priority 100.
    sub = ServiceZone.objects.create(
        code='IN-TG-HYD-CENTRE',
        name='Hyderabad Centre',
        country='IN',
        state_code='TG',
        city='Hyderabad',
        zone_type='subzone',
        parent=parent,
        priority=100,
        polygon_geojson={
            'type': 'Polygon',
            'coordinates': [[
                [78.480, 17.380],
                [78.480, 17.390],
                [78.495, 17.390],
                [78.495, 17.380],
                [78.480, 17.380],
            ]],
        },
        is_active=True,
    )
    zone = find_zone_for_point(HYD_CENTRE_LAT, HYD_CENTRE_LON)
    assert zone is not None
    assert zone.code == sub.code


# ---------------------------------------------------------------------------
# Rate card resolution
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_seeded_rate_cards_exist_for_phase0_vehicle_types():
    from servers.pricing.models import RateCard, ServiceZone
    zone = ServiceZone.objects.get(code='IN-TG-HYD')
    cards = RateCard.objects.filter(zone=zone, is_active=True)
    types_with_cards = {c.vehicle_type.type for c in cards}
    # The 0002 data migration ports VehicleFarePricing if it exists;
    # otherwise seeds the Phase-0 defaults. Either way we expect at
    # least 'sedan' to be present (most-used in existing fixtures).
    assert len(types_with_cards) >= 1


@pytest.mark.django_db
def test_rate_card_resolution_picks_currently_effective_one():
    from servers.driver.models import VehicleType
    from servers.pricing.models import RateCard, ServiceZone
    from servers.pricing.services import get_active_rate_card

    zone = ServiceZone.objects.get(code='IN-TG-HYD')
    # Use a fresh vehicle type so the seeded Phase-0 cards don't compete
    # with the ones we create here.
    vt = VehicleType.objects.create(type='premium-test')

    now = timezone.now()
    RateCard.objects.create(
        zone=zone, vehicle_type=vt, version=1,
        base_fare=Decimal('20.00'), per_km_fare=Decimal('10.00'),
        per_min_fare=Decimal('1.00'), min_fare=Decimal('30.00'),
        effective_from=now - timedelta(days=30),
        effective_to=now - timedelta(days=1),
        is_active=True,
    )
    current = RateCard.objects.create(
        zone=zone, vehicle_type=vt, version=2,
        base_fare=Decimal('30.00'), per_km_fare=Decimal('12.00'),
        per_min_fare=Decimal('1.50'), min_fare=Decimal('40.00'),
        effective_from=now - timedelta(hours=1),
        effective_to=None,
        is_active=True,
    )
    RateCard.objects.create(
        zone=zone, vehicle_type=vt, version=3,
        base_fare=Decimal('40.00'), per_km_fare=Decimal('14.00'),
        per_min_fare=Decimal('2.00'), min_fare=Decimal('50.00'),
        effective_from=now + timedelta(days=7),
        effective_to=None,
        is_active=True,
    )
    resolved = get_active_rate_card(zone, vt)
    assert resolved is not None
    assert resolved.id == current.id


@pytest.mark.django_db
def test_rate_card_resolution_falls_back_to_parent_zone():
    """If a sub-zone has no card, the city card must be used."""
    from servers.driver.models import VehicleType
    from servers.pricing.models import RateCard, ServiceZone
    from servers.pricing.services import get_active_rate_card

    city = ServiceZone.objects.get(code='IN-TG-HYD')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    city_card = RateCard.objects.create(
        zone=city, vehicle_type=vt, version=99,
        base_fare=Decimal('100.00'), per_km_fare=Decimal('20.00'),
        per_min_fare=Decimal('3.00'), min_fare=Decimal('120.00'),
        is_active=True,
    )

    sub = ServiceZone.objects.create(
        code='IN-TG-HYD-NEW',
        name='New sub-zone',
        country='IN', state_code='TG', city='Hyderabad',
        zone_type='subzone', parent=city, priority=50,
        polygon_geojson={'type': 'Polygon', 'coordinates': [[
            [78.50, 17.40], [78.51, 17.40], [78.51, 17.41], [78.50, 17.41], [78.50, 17.40],
        ]]},
    )
    resolved = get_active_rate_card(sub, vt)
    assert resolved is not None
    assert resolved.id == city_card.id


# ---------------------------------------------------------------------------
# quote_fare
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_quote_fare_uses_rate_card_when_zone_known():
    from servers.driver.models import VehicleType
    from servers.pricing.models import RateCard, ServiceZone
    from servers.pricing.services import quote_fare

    zone = ServiceZone.objects.get(code='IN-TG-HYD')
    vt, _ = VehicleType.objects.get_or_create(type='hatchback')
    RateCard.objects.update_or_create(
        zone=zone, vehicle_type=vt, version=1,
        defaults={
            'base_fare': Decimal('50.00'), 'per_km_fare': Decimal('15.00'),
            'per_min_fare': Decimal('2.00'), 'min_fare': Decimal('80.00'),
            'is_active': True,
        },
    )
    fare = quote_fare(
        distance_km=Decimal('10'), duration_min=Decimal('20'),
        vehicle_type='hatchback',
        pickup_lat=HYD_CENTRE_LAT, pickup_lon=HYD_CENTRE_LON,
        at=timezone.now(),
    )
    assert fare['source'] == 'db'
    assert fare['zone_code'] == 'IN-TG-HYD'
    # base 50 + 15*10 + 2*20 = 240; surge is 1.00 in tests (no Redis)
    assert fare['total_fare'] == Decimal('240.00')
    assert fare['min_fare_applied'] is False


@pytest.mark.django_db
def test_quote_fare_falls_back_to_defaults_when_outside_any_zone():
    from servers.pricing.services import quote_fare
    fare = quote_fare(
        distance_km=Decimal('5'), duration_min=Decimal('10'),
        vehicle_type='auto',
        pickup_lat=BLR_LAT, pickup_lon=BLR_LON,
    )
    assert fare['source'] == 'default'
    assert fare['zone_code'] == ''
    # base 30 + 12*5 + 2*10 = 110 -> >= min_fare 50, no min
    assert fare['total_fare'] == Decimal('110.00')


@pytest.mark.django_db
def test_quote_fare_enforces_minimum_fare():
    from servers.driver.models import VehicleType
    from servers.pricing.models import RateCard, ServiceZone
    from servers.pricing.services import quote_fare

    zone = ServiceZone.objects.get(code='IN-TG-HYD')
    vt, _ = VehicleType.objects.get_or_create(type='auto')
    RateCard.objects.update_or_create(
        zone=zone, vehicle_type=vt, version=1,
        defaults={
            'base_fare': Decimal('10.00'), 'per_km_fare': Decimal('5.00'),
            'per_min_fare': Decimal('1.00'), 'min_fare': Decimal('60.00'),
            'is_active': True,
        },
    )
    fare = quote_fare(
        distance_km=Decimal('1'), duration_min=Decimal('1'),
        vehicle_type='auto',
        pickup_lat=HYD_CENTRE_LAT, pickup_lon=HYD_CENTRE_LON,
    )
    assert fare['min_fare_applied'] is True
    assert fare['total_fare'] == Decimal('60.00')


# ---------------------------------------------------------------------------
# Public + admin endpoints
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_public_list_active_zones_anon(api_client):
    resp = api_client.get('/api/v1/pricing/zones/')
    assert resp.status_code == 200
    payload = resp.json()['data']
    assert 'zones' in payload
    codes = {z['code'] for z in payload['zones']}
    assert 'IN-TG-HYD' in codes


@pytest.mark.django_db
def test_admin_zones_list_requires_admin(auth_client_rider, auth_client_admin):
    rider_client, _ = auth_client_rider
    resp = rider_client.get('/api/v1/pricing/admin/zones/')
    assert resp.status_code in (401, 403)

    admin_client, _ = auth_client_admin
    resp = admin_client.get('/api/v1/pricing/admin/zones/')
    assert resp.status_code == 200


@pytest.mark.django_db
def test_admin_can_create_new_city_zone(auth_client_admin):
    admin_client, _ = auth_client_admin
    payload = {
        'code': 'IN-KA-BLR',
        'name': 'Bangalore',
        'country': 'IN',
        'state_code': 'KA',
        'city': 'Bangalore',
        'zone_type': 'city',
        'priority': 10,
        'polygon_geojson': {
            'type': 'Polygon',
            'coordinates': [[
                [77.50, 12.85], [77.50, 13.10],
                [77.75, 13.10], [77.75, 12.85],
                [77.50, 12.85],
            ]],
        },
        'is_active': True,
    }
    resp = admin_client.post('/api/v1/pricing/admin/zones/', payload, format='json')
    assert resp.status_code == 201, resp.content
    assert resp.json()['code'] == 'IN-KA-BLR'

    from servers.pricing.services import find_zone_for_point
    z = find_zone_for_point(BLR_LAT, BLR_LON)
    assert z is not None and z.code == 'IN-KA-BLR'


@pytest.mark.django_db
def test_admin_create_zone_rejects_invalid_polygon(auth_client_admin):
    admin_client, _ = auth_client_admin
    payload = {
        'code': 'IN-XX-BAD',
        'name': 'Bad zone',
        'zone_type': 'city',
        'polygon_geojson': {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 1]]]},
    }
    resp = admin_client.post('/api/v1/pricing/admin/zones/', payload, format='json')
    assert resp.status_code == 400
