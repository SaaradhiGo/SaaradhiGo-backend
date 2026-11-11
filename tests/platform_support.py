"""Shared helpers for the Platform Settings tests (module 15). Not a test file."""

from datetime import datetime, timezone
from io import StringIO

from django.core.management import call_command

from servers.admin_access.models import GeoNode
from servers.driver.models import VehicleType
from servers.payments.models import PayoutCountryConfig
from servers.pricing.models import RateCard
from test_safety_module import seed_config
from test_sos_support_zones_matrix import _zone, operator

LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)
BASE = '/api/v1/platform/admin'
APPROVALS = '/api/v1/approvals/admin/requests'


def post(client, path, body=None):
    return client.post(path, body or {}, format='json')


def put(client, path, body=None):
    return client.put(path, body or {}, format='json')


def data(response):
    return response.json().get('data', response.json())


def error_code(response):
    return response.json()['error']['code']


def two_super_admins(geo):
    maker, maker_user = operator('super_admin', geo)
    checker, checker_user = operator('super_admin', geo)
    return (maker, maker_user), (checker, checker_user)


def approve(client, request_id, reason='looks right'):
    return post(client, f'{APPROVALS}/{request_id}/approve/', {'reason': reason})


def ready_up(geo, settings, code='IN-TG-WGL', *, country='IN'):
    """Make every BLOCKING readiness item of city `code` green.

    zone, fare card for each vehicle class, commission (the autouse default rule), safety coverage,
    payout configuration, approval policies, payment gateway and a live SMS provider.
    """
    from conftest import seed_actor

    node = geo[code] if isinstance(geo, dict) else geo
    node = GeoNode.objects.get(pk=node.pk)
    zone = _zone(node.code, node)
    if not VehicleType.objects.exists():
        VehicleType.objects.create(type='sedan')
    for vt in VehicleType.objects.all():
        RateCard.objects.get_or_create(
            zone=zone, vehicle_type=vt,
            defaults=dict(base_fare=30, per_km_fare=12, per_min_fare=2, min_fare=50,
                          effective_from=LONG_AGO))
    from servers.safety.models import SafetyCountryConfig

    if not SafetyCountryConfig.objects.filter(country=country, is_active=True).exists():
        seed_config(country, min_safety_officers=1)
    operator('safety_officer', geo if isinstance(geo, dict) else {node.code: node},
             scope_code=node.code)
    if not PayoutCountryConfig.objects.filter(country=country, is_active=True).exists():
        PayoutCountryConfig.objects.create(
            country=country, fee_percent=2, minimum_withdrawal_minor=50000, is_active=True)
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY = 'app-id-test', 'secret-test'
    settings.PAYMENT_GATEWAY = 'cashfree'
    settings.NOTIFY_LIVE_SEND = True
    settings.MSG91_AUTH_KEY = 'msg91-test'
    return node
