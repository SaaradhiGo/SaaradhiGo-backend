"""17 Nov, production round 1 (PROD on Railway before provider keys): Book says "launching soon",
sign-in codes and email flows say "not set up yet" instead of pretending, drivers can apply before
sign-up opens, /healthz reports the scheduler, and the first Super Admin is invited, never given a
password."""
from io import StringIO

import pytest
from django.core import mail
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

from servers.admin_access.models import CITY, AdminAssignment, GeoNode
from servers.platform_ops.markets import booking_gate_for_zone
from servers.platform_ops.models import DriverApplication
from tests.conftest import grant_legacy_equivalent, seed_admin_access

NO_MAIL = dict(IS_PRODUCTION=True, EMAIL_BACKEND='', DEFAULT_FROM_EMAIL='')


def test_booking_closed_answers_launching_soon_everywhere(settings):
    settings.BOOKING_OPEN = False
    ok, code, message = booking_gate_for_zone(None)
    assert (ok, code) == (False, 'CITY_NOT_LIVE') and message
    settings.BOOKING_OPEN = True
    assert booking_gate_for_zone(None)[0] is True


@pytest.mark.django_db
def test_production_without_sms_refuses_the_code_instead_of_claiming_it_was_sent(settings):
    settings.IS_PRODUCTION = True
    settings.SMS_DIRECT_PROVIDERS = {}
    settings.SNS_LIVE_CONFIRMED = False
    settings.TEST_PHONE_NUMBERS = {}
    r = APIClient().post('/api/v1/auth/otp/', {'phone_number': '+919812345670', 'role': 'rider'}, format='json')
    assert r.status_code == 503, r.content
    assert r.json()['error']['code'] == 'SMS_NOT_CONFIGURED'


@pytest.mark.django_db
def test_qa_still_sends_codes_through_its_usual_path(settings):
    from integrations.sms.direct import production_sms_missing
    settings.IS_PRODUCTION = False
    assert production_sms_missing('+919812345670') is False
    settings.IS_PRODUCTION = True
    settings.SMS_DIRECT_PROVIDERS = {'IN': 'msg91'}
    assert production_sms_missing('+919812345670') is False


@pytest.mark.django_db
@pytest.mark.parametrize('path,body', [
    ('/api/v1/auth/email/register/', {'email': 'a@example.com', 'password': 'x', 'role': 'rider'}),
    ('/api/v1/auth/password/forgot/', {'email': 'a@example.com'}),
])
def test_production_without_mail_says_so(settings, path, body):
    for k, v in NO_MAIL.items():
        setattr(settings, k, v)
    r = APIClient().post(path, body, format='json')
    assert r.status_code == 503, r.content
    assert r.json()['error']['code'] == 'EMAIL_NOT_CONFIGURED'


@pytest.mark.django_db
def test_driver_can_apply_before_sign_up_opens_once_per_number_and_city():
    seed_admin_access()
    from django.core.cache import cache
    cache.clear()  # the throttle's counters, shared with other anonymous tests in one run
    body = {'full_name': 'Ravi Kumar', 'phone_number': '98123 45670', 'city': 'IN-TG-HYD', 'vehicle_type': 'auto'}
    c = APIClient()
    assert c.post('/api/v1/platform/driver-applications/', body, format='json').status_code == 201
    assert c.post('/api/v1/platform/driver-applications/', body, format='json').status_code == 201
    row = DriverApplication.objects.get()
    assert row.phone_number == '+919812345670' and row.city.code == 'IN-TG-HYD'
    bad = c.post('/api/v1/platform/driver-applications/', {**body, 'city': 'NOPE'}, format='json')
    assert bad.status_code == 400 and 'city' in bad.content.decode()


@pytest.mark.django_db
def test_operators_see_applications_and_the_public_does_not(auth_client_admin):
    seed_admin_access()
    DriverApplication.objects.create(full_name='A B', phone_number='+919812345670', vehicle_type='car',
                                     city=GeoNode.objects.get(kind=CITY, code='IN-TG-HYD'))
    assert APIClient().get('/api/v1/platform/admin/driver-applications/').status_code == 401
    client, user = auth_client_admin
    grant_legacy_equivalent(user, 'super')
    r = client.get('/api/v1/platform/admin/driver-applications/')
    assert r.status_code == 200, r.content
    assert r.json()['data']['count'] == 1


@pytest.mark.django_db
def test_healthz_reports_the_scheduler():
    r = APIClient().get('/healthz')
    assert r.json()['scheduler'] in ('never', 'ok', 'stale')


@pytest.mark.django_db
def test_first_super_admin_is_invited_never_given_a_password(settings):
    seed_admin_access()
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    settings.DEFAULT_FROM_EMAIL = 'no-reply@example.com'
    AdminAssignment.objects.filter(role__code='super_admin').update(is_active=False)
    call_command('invite_super_admin', email='owner@example.com', name='Owner', stdout=StringIO())
    a = AdminAssignment.objects.get(admin__email='owner@example.com', is_active=True)
    assert a.role.code == 'super_admin' and a.scope.kind == 'global'
    assert not a.admin.has_usable_password()
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ['owner@example.com']
    with pytest.raises(CommandError):
        call_command('invite_super_admin', email='second@example.com', stdout=StringIO())


@pytest.mark.django_db
def test_without_mail_the_invite_waits_and_says_so(settings):
    seed_admin_access()
    settings.EMAIL_BACKEND = 'django.core.mail.backends.dummy.EmailBackend'
    AdminAssignment.objects.filter(role__code='super_admin').update(is_active=False)
    out = StringIO()
    call_command('invite_super_admin', email='owner@example.com', stdout=out)
    assert 'Invite NOT sent' in out.getvalue() and len(mail.outbox) == 0


@pytest.mark.django_db
def test_prod_first_setup_seeds_invites_the_owner_and_leaves_no_other_operator(settings):
    from django.contrib.auth import get_user_model
    from servers.admin_access.models import ACTIVE
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    settings.DEFAULT_FROM_EMAIL = 'no-reply@example.com'
    U = get_user_model()
    U.objects.all().delete()
    GeoNode.objects.all().delete()
    out = StringIO()
    call_command('prod_first_setup', owner_email='owner@example.com', owner_name='Owner', stdout=out)
    live = set(GeoNode.objects.filter(kind=CITY, status=ACTIVE).values_list('code', flat=True))
    assert live == {'IN-TG-HYD'}
    active_ops = list(U.objects.filter(is_active=True, is_staff=True).values_list('email', flat=True))
    assert active_ops == ['owner@example.com']
    assert AdminAssignment.objects.get(is_active=True).admin.email == 'owner@example.com'
    assert len(mail.outbox) == 1
    with pytest.raises(CommandError):
        call_command('prod_first_setup', owner_email='x@example.com', stdout=StringIO())


@pytest.mark.django_db
def test_an_operator_reveals_an_applicants_number_and_it_is_logged(auth_client_admin):
    from servers.admin_audit.models import AdminAuditLog
    seed_admin_access()
    a = DriverApplication.objects.create(full_name='A B', phone_number='+919812345670', vehicle_type='car',
                                         city=GeoNode.objects.get(kind=CITY, code='IN-TG-HYD'))
    client, user = auth_client_admin
    grant_legacy_equivalent(user, 'super')
    before = AdminAuditLog.objects.count()
    r = client.post('/api/v1/admin-access/reveal/', {'target_type': 'driver_application', 'target_id': a.id,
                                                    'fields': ['phone_number']}, format='json')
    assert r.status_code == 200, r.content
    assert '+919812345670' in r.content.decode()
    assert AdminAuditLog.objects.count() > before
