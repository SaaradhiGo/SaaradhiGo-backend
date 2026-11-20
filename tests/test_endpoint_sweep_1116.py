"""Every routed endpoint, called as the wrong person (security review, 16 Nov 2026).

Walks the URL conf, so a route added tomorrow is swept the day it lands. Three callers:
anonymous; another rider or driver holding the victim's ids; a console operator holding
every city-level permission, but only in Austin, holding Hyderabad's ids.
"""

import json
import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.test import APIClient

from finance_world import client_for, make_driver, make_rider
from reports_world import build_month
from servers.admin_access.models import AdminAssignment, AdminRole
from servers.admin_access.roles import ALL_PERMISSIONS, SUPER_ADMIN

pytestmark = pytest.mark.django_db

User = get_user_model()
METHODS = ('get', 'post', 'put', 'patch', 'delete')
REFUSED = {401, 403, 404, 405}

# Routes an anonymous caller may reach, each with its reason. Webhooks are separate:
# anonymous is their normal caller, and an unsigned body must be refused.
PUBLIC = {
    '$': 'service banner at the root, GET only (20 Nov)', 'healthz': 'liveness', 'version': 'build id', 'api/v1/config/': 'public config',
    'api/v1/app-config/': 'app version gate', 'api/v1/auth/': 'sign-in and recovery',
    'api/v1/ride/estimate-fare/': 'public fare estimate', 'api/v1/ride/maps/': 'map proxy, metered',
    'api/v1/pricing/zones/': 'service areas', 'api/v1/pricing/countries/': 'markets',
    'api/v1/pricing/fares/': 'public fare table', 'api/v1/pricing/vehicle-types/': 'catalogue',
    'api/v1/pricing/$': 'DRF API root', 'api/v1/pricing/<drf': 'DRF API root',
    'api/v1/sharing/public/': 'share link (token)', 'api/v1/sharing/sms/inbound/': 'SMS webhook',
    'api/v1/driver/driver/documents/view/': 'signed document ticket',
    'api/v1/platform/driver-applications/': 'pre-launch driver application (17 Nov, throttled)',
}
WEBHOOKS = ('webhook', 'webhooks/', 'sms/inbound')


def routes():
    out = []

    def walk(patterns, prefix=''):
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern) and 'format' not in str(p.pattern):
                out.append((prefix + str(p.pattern), p.callback))
    walk(get_resolver().url_patterns)
    return out


def methods_of(cb):
    actions = getattr(cb, 'actions', None)
    if actions:
        return sorted(actions)
    cls = getattr(cb, 'view_class', None) or getattr(cb, 'cls', None)
    if cls is None:
        return ['get', 'post']
    return [m for m in METHODS if hasattr(cls, m)]


def fill(path, ids):
    path = path.replace('^', '').replace('$', '')
    path = re.sub(r'\(\?P<(\w+)>[^)]*\)', lambda m: '<' + m.group(1) + '>', path)

    def sub(m):
        kind, name = m.group(1) or 'str', m.group(2)
        if name in ids:
            return str(ids[name])
        return '999999' if kind == 'int' or name == 'pk' else 'x'
    return '/' + re.sub(r'<(?:(\w+):)?(\w+)>', sub, path)


def call(client, method, url):
    return getattr(client, method)(url, data={}, format='json')


def is_public(path):
    return any(path.startswith(p.rstrip('$')) if not p.endswith('$') else path == p[:-1]
               for p in PUBLIC)


def is_webhook(path):
    return any(w in path for w in WEBHOOKS)


@pytest.fixture
def world(db):
    from conftest import seed_admin_access
    from servers.admin_access.models import GeoNode

    seed_admin_access()
    geo = {n.code: n for n in GeoNode.objects.all()}
    w = build_month(geo)
    w.attacker = make_rider('ATK', '91')
    w.attacker_driver = make_driver(geo, 'IN-TG-HYD', 'ATK', currency='INR', calling='91')
    role = AdminRole.objects.create(
        code='sweep_all_city', name='Sweep every city permission',
        permissions=sorted(p for p in ALL_PERMISSIONS))
    op = User.objects.create_user(phone_number='+15550100999', username='+15550100999',
                                  email='sweep.aus@saaradhigo.test', full_name='Sweep Operator',
                                  role='admin', is_staff=True)
    AdminAssignment.objects.create(admin=op, role=role, scope=geo['US-TX-AUS'])
    w.austin_op = op
    victim = w.in_riders[0]
    trip = w.trips.hyd[0]
    w.ids = {'trip_id': trip.id, 'ticket_id': w.ticket.id, 'sos_id': w.sos.id,
             'incident_id': w.incident.id, 'flag_id': w.flag.id,
             'driver_id': w.in_driver.id, 'rider_id': victim.id, 'user_id': victim.id,
             'pk': trip.id, 'vehicle_id': w.in_driver.active_vehicle_id,
             'promotion_id': w.campaign.id, 'campaign_id': w.campaign.id}
    from servers.approvals.models import ApprovalRequest
    from servers.driver.models import WithdrawalRequest
    wd = WithdrawalRequest.objects.create(driver=w.in_driver, amount_minor=70000,
                                          currency='INR', status='pending')
    req = ApprovalRequest.objects.create(
        action_key='finance.payout', country='IN', amount=1, currency='INR',
        target_type='withdrawal', target_id=str(wd.id), maker_id=0, maker_label='sweep',
        maker_role='system', payload={'withdrawal_ids': [wd.id]}, idempotency_key='sweep-1')
    w.ids.update(payout_id=wd.id, withdrawal_id=wd.id, request_id=req.id)
    w.canaries = [victim.phone_number, victim.full_name, w.in_driver.user_id.phone_number,
                  w.in_driver.user_id.full_name, w.in_driver.active_vehicle.vehicle_number]
    return w


def sweep(client, ids, include, canaries=()):
    """[(method, url, status, leaked canary or '')] for every non-refused response."""
    rows = []
    for path, cb in routes():
        if not include(path):
            continue
        url = fill(path, ids)
        for m in methods_of(cb):
            r = call(client, m, url)
            body = getattr(r, 'content', b'').decode('utf-8', 'replace')
            leak = next((c for c in canaries if c and c in body), '')
            if r.status_code not in REFUSED or leak:
                rows.append((m.upper(), url, r.status_code, leak))
    return rows


def test_anonymous_reaches_only_public_routes(world):
    rows = sweep(APIClient(), world.ids, lambda p: True, world.canaries)
    bad = [r for r in rows if r[2] >= 500 or r[3] or (
        not is_public(r[1].lstrip('/')) and not (is_webhook(r[1]) and r[2] in (400, 401, 403)))]
    print(json.dumps(bad, indent=0))
    assert not bad


def test_another_rider_or_driver_gets_nothing(world):
    admin = re.compile(r'admin|/command-centre/|/live-ops/|/reports/|/approvals/|/fraud/|/safety/|'
                       r'/search/|/platform/(?!launch-interest)|/pricing/(?!zones|countries|fares|'
                       r'vehicle-types|quote)')
    out = {}
    for who, user in (('rider', world.attacker), ('driver', world.attacker_driver.user_id)):
        rows = sweep(client_for(user), world.ids, lambda p: True, world.canaries)
        out[who] = [r for r in rows if r[1] != '/api/v1/pricing/' and (r[2] >= 500 or r[3] or (
            admin.search(r[1]) and r[2] < 400) or (re.search(r'/\d+/', r[1]) and r[2] < 400))]
    print(json.dumps(out, indent=0))
    assert not out['rider'] and not out['driver']


def test_an_austin_operator_cannot_open_hyderabad_records(world):
    rows = sweep(client_for(world.austin_op), world.ids, lambda p: True, world.canaries)
    # Riders belong to no city by design (scoping.RIDERS_ARE_GLOBAL); the list is gated on pii.view.
    bad = [r for r in rows if r[1] != '/api/v1/auth/admin/users/' and (
        r[2] >= 500 or r[3] or (re.search(r'/\d+/', r[1]) and r[2] < 400))]
    print(json.dumps(bad, indent=0))
    assert not bad


def test_a_hyderabad_operator_still_reaches_the_trip(world):
    from servers.admin_access.models import GeoNode
    AdminAssignment.objects.filter(admin=world.austin_op).update(
        scope=GeoNode.objects.get(code='IN-TG-HYD'))
    c = client_for(world.austin_op)
    trip = world.trips.hyd[0].id
    assert c.get(f'/api/v1/ride/trip/{trip}/chat/').status_code == 200
    assert c.get(f'/api/v1/ride/trip/{trip}/details/').status_code == 200
