"""The rider's pickup code (Trip.otp) never reaches an admin (owner ruling, AUDIT 27.74).

Not shown to any admin role, Super Admin included. Support may re-send it to the rider but never see
it. The end-to-end journey found it in the admin trip detail and on the console trip page.

The guard walks every GET route in the admin namespace (the same walk as
`test_admin_namespace_is_gated`), fills every path parameter with the live trip's id, calls it as a
global Super Admin and asserts the code is nowhere in the body. A route added tomorrow is covered the
moment it is routed.
"""

import re
from decimal import Decimal

import pytest
from django.utils import timezone

from test_admin_namespace_is_gated import admin_routes, gates_for
from notifications_support import geo  # noqa: F401
from finance_world import zone
from test_finance_actions import checker

pytestmark = pytest.mark.django_db

CODE = '736194'
PARAM = re.compile(r'<(?:(?P<conv>\w+):)?(?P<name>\w+)>')


@pytest.fixture
def live_trip(geo, auth_client_rider, auth_client_driver):  # noqa: F811
    from servers.ride.models import Trip, TripStatus

    _, rider = auth_client_rider
    _, driver_user = auth_client_driver
    status_row, _ = TripStatus.objects.get_or_create(status_code='accepted')
    return Trip.objects.create(
        zone=zone('IN-TG-HYD', geo['IN-TG-HYD']), user_id=rider, driver_id=driver_user.driver, status_id=status_row,
        pickup_lat=Decimal('17.385'), pickup_long=Decimal('78.486'),
        destination_lat=Decimal('17.44'), destination_long=Decimal('78.38'),
        estimated_fare=Decimal('200.00'), otp=CODE, accepted_at=timezone.now(),
        payment_method='cash', payment_status='pending')


def _get_routes():
    out = []
    for route, callback in admin_routes():
        if 'GET' not in gates_for(callback):
            continue
        out.append(route)
    return sorted(set(out))


def _fill(route, value):
    def one(match):
        return 'x' if match.group('conv') in ('slug', 'str') and 'token' in match.group('name') \
            else str(value)
    return '/' + PARAM.sub(one, route).lstrip('^').rstrip('$')


def test_the_walk_covers_the_trip_routes():
    routes = _get_routes()
    assert any('ride/admin/trips/<int:trip_id>/' == r.split('api/v1/')[-1] for r in routes), routes
    assert len(routes) > 50


def test_no_admin_get_route_returns_the_pickup_code(live_trip):
    client, _ = checker()
    leaks, called = [], 0
    for route in _get_routes():
        response = client.get(_fill(route, live_trip.id))
        called += 1
        body = response.content.decode('utf-8', errors='replace')
        if CODE in body:
            leaks.append((route, response.status_code))
    assert called > 50
    assert leaks == []


def test_the_trip_detail_has_no_otp_key(live_trip):
    client, _ = checker()
    data = client.get(f'/api/v1/ride/admin/trips/{live_trip.id}/').json()['data']
    assert 'otp' not in data
    assert data['actions']['resend_pickup_code'] is True


def test_the_rider_still_gets_their_own_code(live_trip, auth_client_rider, auth_client_driver):
    rider_client, _ = auth_client_rider
    driver_client, _ = auth_client_driver
    mine = rider_client.get(f'/api/v1/ride/trip/{live_trip.id}/')
    assert mine.status_code == 200 and CODE in mine.content.decode()
    theirs = driver_client.get(f'/api/v1/ride/trip/{live_trip.id}/')
    assert CODE not in theirs.content.decode()


class TestSupportResendsWithoutSeeingIt:
    def test_support_resends_and_nothing_stored_holds_the_code(self, live_trip, geo,  # noqa: F811
                                                                monkeypatch):
        from servers.admin_audit.models import AdminAuditLog
        from servers.notifications import dispatcher

        sent = []
        monkeypatch.setattr(dispatcher, 'notify',
                            lambda *a, **k: sent.append((a, k)) or ['row'])
        client, _ = checker()
        r = client.post(f'/api/v1/ride/admin/trips/{live_trip.id}/resend-pickup-code/',
                        {'reason': 'Rider says the app lost the code'}, format='json')
        assert r.status_code == 200, r.content
        assert CODE not in r.content.decode()
        (args, kwargs), = sent
        assert args[0] == 'trips.pickup_code_reminder' and args[1] == live_trip.user_id
        assert CODE not in repr((args, kwargs))
        row = AdminAuditLog.objects.get(action='trip_pickup_code_resent')
        assert CODE not in repr((row.before, row.after, row.reason))

    def test_the_template_never_carries_the_code(self):
        from servers.notifications.seed_data import seed_rows

        rows = [copy for key, _, _, copy, _ in seed_rows() if key == 'trips.pickup_code_reminder']
        assert len(rows) == 4
        assert all(copy['variables'] == ['trip_id'] for copy in rows)
        assert all('otp' not in (copy.get('body', '') + copy.get('title', '')).lower()
                   and '{code}' not in copy.get('body', '') for copy in rows)

    def test_only_before_the_trip_starts(self, live_trip):
        from servers.ride.models import TripStatus

        live_trip.status_id, _ = TripStatus.objects.get_or_create(status_code='in_progress')
        live_trip.save(update_fields=['status_id'])
        client, _ = checker()
        r = client.post(f'/api/v1/ride/admin/trips/{live_trip.id}/resend-pickup-code/',
                        {'reason': 'x'}, format='json')
        assert r.status_code == 409

    def test_a_reason_is_required(self, live_trip):
        client, _ = checker()
        r = client.post(f'/api/v1/ride/admin/trips/{live_trip.id}/resend-pickup-code/', {},
                        format='json')
        assert r.status_code == 400


def test_the_django_admin_form_has_no_otp_field():
    # The Django admin app is not installed under the test settings, so read the class source.
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / 'servers' / 'ride' / 'admin.py').read_text(
        encoding='utf-8')
    trip_admin = source.split('class TripAdmin(', 1)[1].split(chr(10) + 'class ', 1)[0]
    assert "exclude = ('otp',)" in trip_admin
    assert 'readonly_fields' not in trip_admin
