"""Data requests (/auth/admin/data-requests/): log, export, erasure through a checker, refuse."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from conftest import seed_admin_access
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.approvals import service
from servers.approvals.models import REQUESTED
from servers.auth_user.models import DataRequest

pytestmark = pytest.mark.django_db

User = get_user_model()
API = '/api/v1/auth/admin/data-requests/'
REASON = 'Verified by OTP on the registered number.'

_seq = iter(range(1000))


def person(role='rider', **extra):
    phone = f'+9197733{next(_seq):05d}'
    return User.objects.create_user(phone_number=phone, username=phone, role=role,
                                    full_name=f'{role} {phone[-3:]}', **extra)


def admin(code, scope):
    u = person('admin', is_staff=True, email=f'a{next(_seq)}@saaradhigo.test')
    AdminAssignment.objects.create(admin=u, role=AdminRole.objects.get(code=code), scope=scope)
    return u


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
    return admin('super_admin', geo['GLOBAL'])


def log(c, subject, kind='export'):
    r = c.post(API, {'subject': f'R-{subject.pk}', 'kind': kind, 'channel': 'email',
                     'reference': 'privacy@ mail 12'}, format='json')
    assert r.status_code == 201, r.content
    return r.json()['data']


def test_only_privacy_manage(geo):
    c = client(admin('city_ops_manager', geo['IN-TG-HYD']))
    assert c.get(API).status_code == 403


def test_export_completes_the_request(owner):
    rider = person()
    c = client(owner)
    row = log(c, rider)
    assert row['status'] == 'open' and row['subject_ref'] == f'R-{rider.pk}'
    assert row['subject_phone'] != rider.phone_number
    r = c.post(f'{API}{row["id"]}/export/', {}, format='json')
    assert r.status_code == 200
    assert r.json()['data']['export']['user'] and r.json()['data']['request']['status'] == 'completed'
    assert c.get(API).json()['data']['open'] == 0


def test_erasure_waits_for_a_second_super_admin(owner, geo):
    rider = person()
    c = client(owner)
    row = log(c, rider, 'erasure')
    assert c.post(f'{API}{row["id"]}/erase/', {'reason': 'short'},
                  format='json').status_code == 400
    r = c.post(f'{API}{row["id"]}/erase/', {'reason': REASON}, format='json')
    assert r.status_code == 200 and r.json()['data']['status'] == 'waiting_approval'
    dr = DataRequest.objects.get(pk=row['id'])
    assert dr.approval_request.status == REQUESTED
    rider.refresh_from_db()
    assert rider.full_name  # nothing erased yet
    service.approve(dr.approval_request, admin('super_admin', geo['GLOBAL']), reason='ok')
    dr.refresh_from_db()
    rider.refresh_from_db()
    assert dr.status == 'completed' and rider.full_name == ''


def test_rejected_erasure_reopens(owner, geo):
    row = log(client(owner), person(), 'erasure')
    client(owner).post(f'{API}{row["id"]}/erase/', {'reason': REASON}, format='json')
    dr = DataRequest.objects.get(pk=row['id'])
    service.reject(dr.approval_request, admin('super_admin', geo['GLOBAL']), 'not verified')
    dr.refresh_from_db()
    assert dr.status == 'open'


def test_refuse_and_operator_subjects(owner, geo):
    c = client(owner)
    row = log(c, person())
    r = c.post(f'{API}{row["id"]}/refuse/', {'reason': 'Could not verify the requester.'},
               format='json')
    assert r.status_code == 200 and r.json()['data']['status'] == 'refused'
    staff = admin('analyst', geo['IN-TG-HYD'])
    assert c.post(API, {'subject': f'R-{staff.pk}', 'kind': 'erasure', 'channel': 'email',
                        'reference': 'x'}, format='json').status_code == 400


def test_self_service_export_is_logged(db):
    rider = person()
    assert client(rider).get('/api/v1/auth/me/export/').status_code == 200
    row = DataRequest.objects.get(subject=rider)
    assert (row.kind, row.channel, row.status) == ('export', 'app', 'completed')
