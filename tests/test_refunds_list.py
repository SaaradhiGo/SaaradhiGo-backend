"""Refunds list (GET /payments/admin/refunds/): ledger rows and waiting requests, in scope."""

from datetime import timedelta

import pytest
from django.utils import timezone

from finance_world import build_world, operator
from servers.approvals.actions import SUPPORT_REFUND
from servers.approvals.models import ApprovalRequest
from servers.ride.models import TripMoneyAction

pytestmark = pytest.mark.django_db

API = '/api/v1/payments/admin/refunds/'


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access
    from servers.admin_access.models import GeoNode

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def world(geo):
    w = build_world(geo)
    TripMoneyAction.objects.create(
        trip=w.in_trip, kind=TripMoneyAction.REFUND, amount_minor=5000, currency='INR',
        method=TripMoneyAction.GATEWAY, status=TripMoneyAction.PENDING_GATEWAY, reason='late')
    TripMoneyAction.objects.create(
        trip=w.us_trip, kind=TripMoneyAction.REFUND, amount_minor=700, currency='USD',
        method=TripMoneyAction.WALLET_CREDIT, status=TripMoneyAction.COMPLETED, reason='detour')
    ApprovalRequest.objects.create(
        action_key=SUPPORT_REFUND, country='IN', scope_node=geo['IN-TG-HYD'],
        amount=90000, currency='INR', target_type='trip', target_id=str(w.in_trip.id),
        request_reason='overcharged', maker_id=1, maker_label='Support',
        expires_at=timezone.now() + timedelta(days=1))
    return w


def rows(client, **params):
    r = client.get(API, params)
    assert r.status_code == 200, r.content
    return r.json()['data']


def test_hyderabad_finance_sees_only_its_city(world, geo):
    client, _ = operator('finance', geo)
    data = rows(client)
    assert [r['currency'] for r in data['results']] == ['INR']
    assert data['counts'] == {'waiting': 1, 'pending_gateway': 1}


def test_views(world, geo):
    client, _ = operator('finance', geo)
    waiting = rows(client, view='waiting')['results']
    assert waiting[0]['source'] == 'approval' and waiting[0]['trip_id'] == world.in_trip.id
    assert rows(client, view='pending_gateway')['results'][0]['status'] == 'pending_gateway'
    assert rows(client, view='done')['results'] == []


def test_needs_finance_read(world, geo):
    client, _ = operator('marketing', geo)
    assert client.get(API).status_code == 403
