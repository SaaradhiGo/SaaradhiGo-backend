"""Regression-locks for the Cashfree webhook hardening (PRs #4, #22).

Three things must be true for every inbound webhook:
  1. Signature is required AND verified (PR #4).
  2. Timestamp is required AND within a 5-min window (PR #4).
  3. The same delivery cannot be processed twice (PR #22 — WebhookEvent
     unique-on-(gateway, dedupe_key)).

Without these, anyone on the internet who can guess the path could
forge a `PAYMENT_SUCCESS` and mint either a paid trip or wallet credit.
"""
import base64
import hashlib
import hmac
import json
import time

import pytest
from django.conf import settings


WEBHOOK_PATH = '/api/v1/payments/webhook/'
TEST_SECRET = 'phase0-test-webhook-secret'


def _sign(body_bytes: bytes, timestamp: str, secret: str = TEST_SECRET) -> str:
    """Reproduce CashfreeGateway.verify_webhook_signature's HMAC."""
    msg = timestamp + body_bytes.decode('utf-8')
    digest = hmac.new(
        secret.encode('utf-8'),
        msg.encode('utf-8'),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode('utf-8')


@pytest.fixture(autouse=True)
def _patch_secret(settings):
    """Pin the Cashfree webhook secret to a known value for the suite."""
    settings.CASHFREE_WEBHOOK_SECRET = TEST_SECRET


# -------------------------------------------------------------------------
# Signature handling (PR #4)
# -------------------------------------------------------------------------

@pytest.mark.django_db
def test_webhook_rejects_missing_signature(api_client):
    """No x-webhook-signature header → 400."""
    body = json.dumps({'type': 'PAYMENT_SUCCESS_WEBHOOK', 'data': {}})
    resp = api_client.post(
        WEBHOOK_PATH,
        data=body,
        content_type='application/json',
        HTTP_X_WEBHOOK_TIMESTAMP=str(int(time.time())),
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_webhook_rejects_missing_timestamp(api_client):
    """No x-webhook-timestamp header → 400 (no replay window without it)."""
    body = json.dumps({'type': 'PAYMENT_SUCCESS_WEBHOOK', 'data': {}}).encode()
    sig = _sign(body, str(int(time.time())))
    resp = api_client.post(
        WEBHOOK_PATH,
        data=body,
        content_type='application/json',
        HTTP_X_WEBHOOK_SIGNATURE=sig,
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_webhook_rejects_bad_signature(api_client):
    """Signature computed with the wrong secret → 400."""
    body = json.dumps({'type': 'PAYMENT_SUCCESS_WEBHOOK', 'data': {}}).encode()
    ts = str(int(time.time()))
    wrong_sig = _sign(body, ts, secret='not-the-real-secret')
    resp = api_client.post(
        WEBHOOK_PATH,
        data=body,
        content_type='application/json',
        HTTP_X_WEBHOOK_SIGNATURE=wrong_sig,
        HTTP_X_WEBHOOK_TIMESTAMP=ts,
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_webhook_rejects_stale_timestamp(api_client):
    """Timestamp older than 5 min → 400 even with a valid signature."""
    body = json.dumps({'type': 'PAYMENT_SUCCESS_WEBHOOK', 'data': {}}).encode()
    # 10 minutes in the past
    ts = str(int(time.time()) - 10 * 60)
    sig = _sign(body, ts)
    resp = api_client.post(
        WEBHOOK_PATH,
        data=body,
        content_type='application/json',
        HTTP_X_WEBHOOK_SIGNATURE=sig,
        HTTP_X_WEBHOOK_TIMESTAMP=ts,
    )
    assert resp.status_code == 400


# -------------------------------------------------------------------------
# Event-id dedupe (PR #22)
# -------------------------------------------------------------------------

@pytest.mark.django_db
def test_webhook_dedupes_replayed_delivery(api_client):
    """Second POST with the same (gateway, signature) returns
    `already_processed` without firing the inner handler twice."""
    body = json.dumps({
        'type': 'SOMETHING_ELSE',  # event we don't process — handler is a no-op
        'data': {'order': {'order_id': 'unmatched-order'}},
    }).encode()
    ts = str(int(time.time()))
    sig = _sign(body, ts)

    # First delivery — recorded
    r1 = api_client.post(
        WEBHOOK_PATH,
        data=body,
        content_type='application/json',
        HTTP_X_WEBHOOK_SIGNATURE=sig,
        HTTP_X_WEBHOOK_TIMESTAMP=ts,
    )
    assert r1.status_code == 200, r1.content

    # Second delivery, identical body/timestamp/signature — should
    # short-circuit on the WebhookEvent unique constraint.
    r2 = api_client.post(
        WEBHOOK_PATH,
        data=body,
        content_type='application/json',
        HTTP_X_WEBHOOK_SIGNATURE=sig,
        HTTP_X_WEBHOOK_TIMESTAMP=ts,
    )
    assert r2.status_code == 200, r2.content
    assert r2.json().get('status') == 'already_processed'

    # Only one row exists for this delivery.
    from servers.payments.models import WebhookEvent
    assert WebhookEvent.objects.filter(dedupe_key=sig).count() == 1


@pytest.mark.django_db
def test_webhook_fail_closed_when_secret_missing(api_client, settings):
    """Empty CASHFREE_WEBHOOK_SECRET → ALL webhooks rejected (regression
    guard for PR #4: the previous code returned True from the gateway as
    a 'dev fallback', which let anyone forge a PAYMENT_SUCCESS in
    production if the env var was unset)."""
    settings.CASHFREE_WEBHOOK_SECRET = ''
    body = json.dumps({'type': 'PAYMENT_SUCCESS_WEBHOOK', 'data': {}}).encode()
    ts = str(int(time.time()))
    # Signature can be anything; should still be rejected.
    resp = api_client.post(
        WEBHOOK_PATH,
        data=body,
        content_type='application/json',
        HTTP_X_WEBHOOK_SIGNATURE='whatever',
        HTTP_X_WEBHOOK_TIMESTAMP=ts,
    )
    assert resp.status_code == 400
