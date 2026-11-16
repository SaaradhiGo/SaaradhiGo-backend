"""The console's market switch (Global / India / United States) on every list it drives.

The console sends the selection as `?scope=<node code>`. Before this, several staff
lists ignored it: with United States selected, Staff and roles still listed the
Hyderabad staff. Each test here asks one of those endpoints for US, for India and for
Global, and checks that a Hyderabad row and an Austin row land on the right side.

Global means every market: it narrows nothing, which is what no `?scope=` means too.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from conftest import seed_admin_access
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.auth_user.models import DataRequest
from servers.pricing.commission_models import CommissionRule
from servers.pricing.models import ServiceZone

pytestmark = pytest.mark.django_db

User = get_user_model()
_seq = iter(range(1000))


def make_admin(email):
    phone = f'+9197733{next(_seq):05d}'
    return User.objects.create_user(phone_number=phone, username=phone, email=email,
                                    full_name=email.split('@')[0], role='admin', is_staff=True)


def assign(user, role_code, scope):
    return AdminAssignment.objects.create(
        admin=user, role=AdminRole.objects.get(code=role_code), scope=scope)


def client_for(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


def box(lon0, lat0, lon1, lat1):
    return {'type': 'Polygon',
            'coordinates': [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}


@pytest.fixture
def geo(db):
    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def owner(geo):
    """A Super Admin at Global: holds every market, so every switch position is allowed."""
    u = make_admin('owner@saaradhigo.test')
    assign(u, 'super_admin', geo['GLOBAL'])
    return u


@pytest.fixture
def api(owner):
    c = client_for(owner)

    def get(path, scope=None, **params):
        if scope:
            params['scope'] = scope
        r = c.get(path, params)
        assert r.status_code == 200, r.content
        body = r.json()
        # The zone viewset answers in DRF's own shape; the function views use the envelope.
        return body['data'] if isinstance(body, dict) and 'data' in body else body
    return get


# ---------------------------------------------------------------------------
# Staff and roles
# ---------------------------------------------------------------------------

def test_staff_list_follows_the_market(api, geo, owner):
    hyd = make_admin('hyd-agent@saaradhigo.test')
    assign(hyd, 'support_agent', geo['IN-TG-HYD'])
    aus = make_admin('aus-agent@saaradhigo.test')
    assign(aus, 'support_agent', geo['US-TX-AUS'])

    def names(scope=None):
        # Emails are masked in the list; `name` is the local part (see make_admin).
        return {p['name'] for p in api('/api/v1/admin-access/staff/', scope)['results']}

    us = names('US')
    assert 'aus-agent' in us and 'hyd-agent' not in us
    # A Global role acts in every market, so its holder is listed under each.
    assert 'owner' in us
    india = names('IN')
    assert 'hyd-agent' in india and 'aus-agent' not in india
    assert {'hyd-agent', 'aus-agent'} <= names('GLOBAL')
    assert {'hyd-agent', 'aus-agent'} <= names()


def test_staff_list_refuses_a_place_that_does_not_exist(owner):
    r = client_for(owner).get('/api/v1/admin-access/staff/', {'scope': 'XX-NOWHERE'})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_audit_log_follows_the_market(api, geo):
    for code in ('IN-TG-HYD', 'US-TX-AUS'):
        AdminAuditLog.objects.create(actor_label='Asha', action='market_switch_probe',
                                     target_type='zone', target_id=code, after={'scope': code})

    def targets(scope=None):
        rows = api('/api/v1/admin-audit/admin/entries/', scope,
                   action='market_switch_probe', verify='0')['results']
        return {r['target_id'] for r in rows}

    assert targets('US') == {'US-TX-AUS'}
    assert targets('IN') == {'IN-TG-HYD'}
    assert targets('GLOBAL') == targets() == {'IN-TG-HYD', 'US-TX-AUS'}


# ---------------------------------------------------------------------------
# Data requests
# ---------------------------------------------------------------------------

def test_data_requests_follow_the_market(api, geo):
    now = timezone.now()
    for phone, country in (('+919811100001', 'IN'), ('+15125550101', 'US')):
        subject = User.objects.create_user(phone_number=phone, username=phone, role='rider')
        DataRequest.objects.create(kind=DataRequest.EXPORT, subject=subject, subject_role='rider',
                                   country=country, channel=DataRequest.EMAIL, reference=country,
                                   received_at=now, due_at=now + timedelta(days=30))

    def countries(scope=None):
        data = api('/api/v1/auth/admin/data-requests/', scope)
        return {r['country'] for r in data['results']}, data['open']

    assert countries('US') == ({'US'}, 1)
    assert countries('IN') == ({'IN'}, 1)
    assert countries('GLOBAL') == countries() == ({'IN', 'US'}, 2)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

def test_approvals_follow_the_market(api, geo):
    for code, country, currency in (('IN-TG-HYD', 'IN', 'INR'), ('US-TX-AUS', 'US', 'USD')):
        ApprovalRequest.objects.create(
            action_key='support.refund', country=country, scope_node=geo[code], amount=100,
            currency=currency, target_type='trip', target_id=code, request_reason='probe',
            maker_id=1, maker_label='Support', idempotency_key=f'market-switch-{code}',
            expires_at=timezone.now() + timedelta(days=1))

    def targets(scope=None):
        return {r['target_id'] for r in api('/api/v1/approvals/admin/requests/', scope)['results']}

    assert targets('US') == {'US-TX-AUS'}
    assert targets('IN') == {'IN-TG-HYD'}
    assert targets('GLOBAL') == targets() == {'IN-TG-HYD', 'US-TX-AUS'}


# ---------------------------------------------------------------------------
# Zones, commission
# ---------------------------------------------------------------------------

@pytest.fixture
def zones(geo):
    for code, node, country, polygon in (
            ('MS-HYD', 'IN-TG-HYD', 'IN', box(78.30, 17.20, 78.60, 17.60)),
            ('MS-AUS', 'US-TX-AUS', 'US', box(-97.95, 30.10, -97.55, 30.50))):
        ServiceZone.objects.update_or_create(code=code, defaults={
            'name': code, 'zone_type': 'city', 'city': code, 'country': country,
            'polygon_geojson': polygon, 'is_active': True, 'priority': 60,
            'geo_node': geo[node]})


def test_zone_list_follows_the_market(api, zones):
    def codes(scope=None):
        data = api('/api/v1/pricing/admin/zones/', scope, limit=500)
        rows = data['results'] if isinstance(data, dict) and 'results' in data else data
        return {z['code'] for z in rows if z['code'].startswith('MS-')}

    assert codes('US') == {'MS-AUS'}
    assert codes('IN') == {'MS-HYD'}
    assert codes('GLOBAL') == codes() == {'MS-HYD', 'MS-AUS'}


def test_commission_rules_and_matrix_follow_the_market(api, geo):
    for country in ('IN', 'US'):
        CommissionRule.objects.create(country=country, geo_node=geo[country], percent=Decimal('20.00'),
                                      status='active', reason='probe')

    def rule_countries(scope=None):
        return {r['country'] for r in api('/api/v1/pricing/admin/commission/rules/', scope)['results']}

    def matrix_countries(scope=None):
        return {d['country'] for d in api('/api/v1/pricing/admin/commission/matrix/', scope)['country_defaults']}

    assert rule_countries('US') == matrix_countries('US') == {'US'}
    assert rule_countries('IN') == matrix_countries('IN') == {'IN'}
    assert {'IN', 'US'} <= rule_countries('GLOBAL') and {'IN', 'US'} <= matrix_countries()


# ---------------------------------------------------------------------------
# Live map, countries and cities, reports
# ---------------------------------------------------------------------------

def test_live_map_snapshot_and_heatmap_follow_the_market(api, zones, monkeypatch):
    import servers.live_ops.snapshot as snapshot_module
    from servers.live_ops.geo import Geography

    Geography.reset()
    monkeypatch.setattr(snapshot_module, 'read_live_positions', lambda: None)
    try:
        def cities(path, scope=None):
            return {c['code'] for c in api(path, scope)['cities']}

        snap = '/api/v1/live-ops/admin/snapshot/'
        assert cities(snap, 'US') and all(c.startswith('US-') for c in cities(snap, 'US'))
        assert cities(snap, 'IN') and all(c.startswith('IN-') for c in cities(snap, 'IN'))
        assert {'IN-TG-HYD', 'US-TX-AUS'} <= cities(snap, 'GLOBAL')
        heat = api('/api/v1/live-ops/admin/heatmap/', 'US')
        assert heat is not None
    finally:
        Geography.reset()


def test_countries_and_cities_follow_the_market(api, geo):
    def codes(scope=None):
        return {c['code'] for c in api('/api/v1/platform/admin/markets/', scope)['countries']}

    assert codes('US') == {'US'}
    assert codes('IN') == {'IN'}
    assert {'IN', 'US'} <= codes('GLOBAL') and {'IN', 'US'} <= codes()


def test_report_catalogue_follows_the_market(api, geo):
    def cities(scope=None):
        return {c['code'] for c in api('/api/v1/reports/admin/catalogue/', scope)['cities']}

    assert cities('US') and all(c.startswith('US-') for c in cities('US'))
    assert cities('IN') and all(c.startswith('IN-') for c in cities('IN'))
    assert cities('GLOBAL') == cities()
