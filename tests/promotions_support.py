"""Shared fixtures and builders for the Promotions & Incentives tests (module 12).

Low-entropy fixture values only (the secret scan reads every commit). No network: the
notifications tests' network block is reused, and nothing here talks to a gateway.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from finance_world import client_for, make_driver, make_rider, make_trip, operator, settle, zone
from notifications_support import block_the_network, geo  # noqa: F401
from test_finance_actions import approve_request, body_of, checker, error_code, get, post
from servers.promotions.models import Campaign

API = '/api/v1/promotions'
ADM = f'{API}/admin'
APPROVALS = '/api/v1/approvals/admin/requests'


def when(days=0, hours=0):
    return (timezone.now() + timedelta(days=days, hours=hours)).isoformat()


def promo_payload(**over):
    body = {
        'kind': 'rider_promo', 'name': 'Welcome 50', 'scope': 'IN-TG-HYD',
        'description': 'Fifty off your first rides',
        'budget_minor': 100_000, 'starts_at': when(hours=-1), 'ends_at': when(days=30),
        'config': {'code': 'WELCOME50', 'discount_type': 'flat', 'discount_flat_minor': 5_000,
                   'min_fare_minor': 0, 'max_per_user': 1},
        'reason': 'Launch offer for new riders',
    }
    body.update(over)
    return body


def incentive_payload(**over):
    body = {
        'kind': 'driver_incentive', 'name': 'Weekend 3', 'scope': 'IN-TG-HYD',
        'budget_minor': 100_000, 'starts_at': when(hours=-1), 'ends_at': when(days=7),
        'config': {'target_trips': 3, 'reward_minor': 30_000},
        'reason': 'Weekend supply push',
    }
    body.update(over)
    return body


def referral_payload(audience='rider', **over):
    body = {
        'kind': 'referral', 'name': f'Refer a {audience}', 'scope': 'IN',
        'budget_minor': 100_000, 'starts_at': when(hours=-1), 'ends_at': when(days=60),
        'config': {'audience': audience, 'required_trips': 2,
                   'referrer_reward_minor': 10_000, 'referee_reward_minor': 5_000},
        'reason': 'Grow the base',
    }
    body.update(over)
    return body


def create(client, payload, expect=201):
    response = post(client, f'{ADM}/campaigns/', payload, expect=expect)
    return body_of(response) if expect == 201 else response


def approve_latest(request_id):
    chk, _ = checker()
    response = approve_request(chk, request_id)
    assert response.status_code == 200, response.content[:400]
    return response


def launch(client, payload):
    """Draft, submit, approve. Returns the Campaign (live or scheduled)."""
    made = create(client, payload)
    submitted = post(client, f'{ADM}/campaigns/{made["id"]}/submit/', {'reason': 'go'}, expect=202)
    approve_latest(body_of(submitted)['approval_request']['id'])
    return Campaign.objects.get(pk=made['id'])


@pytest.fixture
def world(geo):  # noqa: F811
    hyd = zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus = zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
               timezone_name='America/Chicago')
    return SimpleNamespace(
        geo=geo, hyd=hyd, aus=aus,
        mkt=operator('marketing', geo, 'IN'),
        mkt_hyd=operator('marketing', geo, 'IN-TG-HYD'),
        mkt_us=operator('marketing', geo, 'US'),
        ops=operator('city_ops_manager', geo, 'IN-TG-HYD'),
        ops_us=operator('city_ops_manager', geo, 'US-TX-AUS'),
        finance=operator('finance', geo, 'IN-TG-HYD'),
        analyst=operator('analyst', geo, 'IN-TG-HYD'),
        support=operator('support_agent', geo, 'IN-TG-HYD'),
        super=operator('super_admin', geo),
    )


def hyd_driver(world, tag):
    from finance_world import make_driver as _md

    return _md(world.geo, 'IN-TG-HYD', tag, currency='INR', calling='91')


def completed_trip(world, rider, driver, fare='200.00', method='online'):
    return make_trip(world.hyd, rider, driver, fare=fare, method=method)


def dec(value):
    return Decimal(str(value))


__all__ = ['API', 'ADM', 'APPROVALS', 'approve_latest', 'block_the_network', 'body_of',
           'checker', 'client_for', 'completed_trip', 'create', 'dec',
           'error_code', 'geo', 'get', 'hyd_driver', 'incentive_payload', 'launch', 'make_rider',
           'operator', 'post', 'promo_payload', 'referral_payload', 'settle', 'when', 'world']
