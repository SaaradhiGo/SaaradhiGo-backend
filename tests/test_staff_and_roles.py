"""Staff and roles API (admin-access/staff/): Super Admin only, reasons, the floor, audit."""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from conftest import seed_admin_access
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_access.roles import ROLE_TEMPLATES
from servers.admin_audit.models import AdminAuditLog

pytestmark = pytest.mark.django_db

User = get_user_model()
API = '/api/v1/admin-access'
REASON = 'New city ops hire for Hyderabad.'

_seq = iter(range(1000))


def make_admin(email, **extra):
    phone = f'+9197722{next(_seq):05d}'
    extra.setdefault('role', 'admin')
    extra.setdefault('is_staff', True)
    return User.objects.create_user(phone_number=phone, username=phone, email=email,
                                    full_name=email.split('@')[0], **extra)


def assign(user, role_code, scope):
    return AdminAssignment.objects.create(
        admin=user, role=AdminRole.objects.get(code=role_code), scope=scope)


def client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


@pytest.fixture
def geo(db):
    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def owner(geo):
    u = make_admin('owner@saaradhigo.test')
    assign(u, 'super_admin', geo['GLOBAL'])
    return u


@pytest.fixture
def hire(geo):
    return make_admin('hire@saaradhigo.test')


def test_finance_template_can_check_finance_requests():
    finance = next(t for t in ROLE_TEMPLATES if t['code'] == 'finance')
    assert 'approval.check' in finance['permissions']


def test_list_masks_contact_and_shows_assignments(owner, hire, geo):
    assign(hire, 'city_ops_manager', geo['IN-TG-HYD'])
    r = client(owner).get(f'{API}/staff/')
    assert r.status_code == 200
    rows = {p['name']: p for p in r.json()['data']['results']}
    row = rows['hire']
    assert row['email'] != hire.email
    assert row['phone'] != hire.phone_number
    assert row['assignments'][0]['role'] == 'city_ops_manager'


def test_a_city_role_holder_cannot_open_staff(owner, hire, geo):
    assign(hire, 'city_ops_manager', geo['IN-TG-HYD'])
    assert client(hire).get(f'{API}/staff/').status_code == 403


def test_grant_needs_a_reason_and_is_audited(owner, hire, geo):
    c = client(owner)
    body = {'role': 'city_ops_manager', 'scope': 'IN-TG-HYD'}
    assert c.post(f'{API}/staff/{hire.pk}/assignments/', body, format='json').status_code == 400
    r = c.post(f'{API}/staff/{hire.pk}/assignments/', {**body, 'reason': REASON}, format='json')
    assert r.status_code == 201, r.content
    row = AdminAssignment.objects.get(admin=hire, is_active=True)
    assert row.granted_by == owner and row.reason == REASON
    assert AdminAuditLog.objects.filter(target_type='AdminAssignment',
                                        target_id=str(row.pk)).exists()
    again = c.post(f'{API}/staff/{hire.pk}/assignments/', {**body, 'reason': REASON},
                   format='json')
    assert again.status_code == 400


def test_super_admin_only_at_global(owner, hire):
    r = client(owner).post(f'{API}/staff/{hire.pk}/assignments/',
                           {'role': 'super_admin', 'scope': 'IN', 'reason': REASON},
                           format='json')
    assert r.status_code == 400
    assert not AdminAssignment.objects.filter(admin=hire).exists()


def test_cannot_change_own_access(owner):
    r = client(owner).post(f'{API}/staff/{owner.pk}/deactivate/', {'reason': REASON},
                           format='json')
    assert r.status_code == 403


def test_revoke_and_history(owner, hire, geo):
    row = assign(hire, 'city_ops_manager', geo['IN-TG-HYD'])
    c = client(owner)
    r = c.post(f'{API}/assignments/{row.pk}/revoke/', {'reason': REASON}, format='json')
    assert r.status_code == 200 and r.json()['data']['is_active'] is False
    detail = c.get(f'{API}/staff/{hire.pk}/').json()['data']
    assert any(h['role'] == 'city_ops_manager' for h in detail['history'])


def test_super_admin_floor_holds(owner, geo):
    second = make_admin('second@saaradhigo.test')
    assign(second, 'super_admin', geo['GLOBAL'])
    third = make_admin('third@saaradhigo.test')
    row = assign(third, 'super_admin', geo['GLOBAL'])
    c = client(owner)
    # Three Super Admins: removing one leaves two, which meets the floor.
    assert c.post(f'{API}/assignments/{row.pk}/revoke/', {'reason': REASON},
                  format='json').status_code == 200
    # Two left: the next one would cross it, as an assignment or an account.
    keep = AdminAssignment.objects.get(admin=second)
    assert c.post(f'{API}/assignments/{keep.pk}/revoke/', {'reason': REASON},
                  format='json').status_code == 409
    assert c.post(f'{API}/staff/{second.pk}/deactivate/', {'reason': REASON},
                  format='json').status_code == 409


def test_deactivate_and_reactivate(owner, hire):
    c = client(owner)
    r = c.post(f'{API}/staff/{hire.pk}/deactivate/', {'reason': REASON}, format='json')
    assert r.status_code == 200 and r.json()['data']['is_active'] is False
    assert AdminAuditLog.objects.filter(action='staff_deactivated',
                                        target_id=str(hire.pk)).exists()
    r = c.post(f'{API}/staff/{hire.pk}/reactivate/', {'reason': REASON}, format='json')
    assert r.status_code == 200 and r.json()['data']['is_active'] is True


def test_writes_need_a_cleared_second_factor(owner, hire):
    with mock.patch('base.ops_mfa.console_access_allowed', return_value=False):
        r = client(owner).post(f'{API}/staff/{hire.pk}/deactivate/', {'reason': REASON},
                               format='json')
    assert r.status_code == 401


def test_options_list_roles_and_places(owner):
    data = client(owner).get(f'{API}/staff/options/').json()['data']
    codes = {r['code'] for r in data['roles']}
    assert {'super_admin', 'finance'} <= codes
    assert next(r for r in data['roles'] if r['code'] == 'super_admin')['global_only']
    assert any(s['code'] == 'IN-TG-HYD' for s in data['scopes'])
