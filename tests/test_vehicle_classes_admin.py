"""Vehicle classes admin (/pricing/admin/vehicle-classes/): coverage in scope, Super Admin edits."""

from decimal import Decimal

import pytest

from finance_world import client_for, make_driver, operator, zone
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import VehicleType
from servers.pricing.models import RateCard

pytestmark = pytest.mark.django_db

API = '/api/v1/pricing/admin/vehicle-classes/'


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def world(geo):
    hyd = zone('IN-TG-HYD', geo['IN-TG-HYD'])
    zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
         timezone_name='America/Chicago')
    make_driver(geo, 'IN-TG-HYD', 'IN', currency='INR', calling='91')
    sedan = VehicleType.objects.get(type='sedan')
    RateCard.objects.create(zone=hyd, vehicle_type=sedan, base_fare=Decimal('30'),
                            per_km_fare=Decimal('12'), per_min_fare=Decimal('2'),
                            min_fare=Decimal('50'), commission_percent=Decimal('22.00'))
    VehicleType.objects.create(type='cargo_test', description='Unpriced')
    return hyd


@pytest.fixture
def super_admin(geo):
    client, user = operator('city_ops_manager', geo)
    AdminAssignment.objects.create(admin=user, role=AdminRole.objects.get(code='super_admin'),
                                   scope=geo['GLOBAL'])
    return client_for(user)


def by_code(client):
    r = client.get(API)
    assert r.status_code == 200, r.content
    return {row['code']: row for row in r.json()['data']['results']}, r.json()['data']['can_edit']


def test_city_ops_sees_coverage_in_its_city_only(world, geo):
    client, _ = operator('city_ops_manager', geo)
    rows, can_edit = by_code(client)
    assert rows['sedan']['zones_priced'] == 1 and rows['sedan']['zones_in_scope'] == 1
    assert rows['sedan']['active_vehicles'] == 1
    assert rows['cargo_test']['zones_priced'] == 0 and rows['cargo_test']['zones_missing'][0]['id'] == world.id
    assert can_edit is False
    assert client.post(API, {'code': 'bike', 'description': 'Two-wheeler'},
                       format='json').status_code == 403


def test_super_admin_adds_and_describes_a_class(world, super_admin):
    r = super_admin.post(API, {'code': 'suv_xl', 'description': 'Seven seats'}, format='json')
    assert r.status_code == 201, r.content
    pk = r.json()['data']['id']
    assert AdminAuditLog.objects.filter(action='vehicle_class_created', target_id=str(pk)).exists()
    assert super_admin.post(API, {'code': 'suv_xl', 'description': 'again'},
                            format='json').status_code == 400
    assert super_admin.post(API, {'code': 'Bad Code', 'description': 'x'},
                            format='json').status_code == 400
    r = super_admin.patch(f'{API}{pk}/', {'description': 'Seven seats, large boot'}, format='json')
    assert r.status_code == 200
    assert super_admin.patch(f'{API}{pk}/', {'code': 'suv', 'description': 'x'},
                             format='json').status_code == 400
