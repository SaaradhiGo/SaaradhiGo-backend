"""Regression-locks for the wallet top-up idempotency fix (PR #6).

The original code had three independent paths that could mint balance:
  1. Client-supplied amount (server didn't verify against gateway).
  2. Double-credit race between user-verify and webhook.
  3. Float-precision drift.

These tests exercise each by stubbing the gateway and driving both paths
end-to-end. If any future change re-opens any of the doors, the test
fails with a clear "Wallet balance is X, expected Y" message.
"""
import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import patch

import pytest


# These tests pre-date the Phase-0 closed-loop wallet posture
# (WALLET_TOPUPS_ENABLED=False by default; see ADR-0003). They exercise
# the OLD top-up code path to lock in the idempotency fix from PR #6.
# Re-enable the flag for every test in this module so the gate doesn't
# short-circuit the assertions about gateway verification / double-credit
# / amount-mismatch behaviour. The closed-loop gate itself is asserted
# separately in tests/test_closed_loop_wallet.py.
@pytest.fixture(autouse=True)
def _enable_topups_for_idempotency_tests(settings):
    settings.WALLET_TOPUPS_ENABLED = True


# A test-only stand-in for `gateway` — must implement the methods the
# wallet flow calls (get_order_status). create_order isn't exercised
# in these tests; we seed the WalletTransaction directly.
class _StubGateway:
    """Returns whatever the test plants in `cls.last_status`."""
    last_status = None

    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def get_name(cls):
        return 'cashfree'

    @classmethod
    def get_order_status(cls, order_id):
        return cls.last_status

    @classmethod
    def create_order(cls, **kwargs):
        return {
            'order_id': kwargs.get('reference_id') or f'order_{int(time.time())}',
            'payment_session_id': 'sess_stub',
        }

    def verify_webhook_signature(self, body, signature, timestamp=None):
        # These tests aren't exercising the signature check (separate
        # test_phase0_webhooks.py covers that). Accept whatever the
        # caller supplies so we can focus on idempotency.
        return True


@pytest.fixture(autouse=True)
def _patch_gateway(monkeypatch):
    """Swap the real Cashfree gateway for the stub in both rider and
    payments views. monkeypatch undoes itself between tests so each gets
    a fresh stub."""
    monkeypatch.setattr(
        'servers.payments.payment_gateways.factory.get_payment_gateway',
        lambda *a, **kw: _StubGateway(),
    )


@pytest.fixture
def wallet_topup_row(db, auth_client_rider):
    """Create a pending WalletTransaction for ₹500 with a known order id."""
    from servers.rider.models import WalletTransaction
    client, user = auth_client_rider
    txn = WalletTransaction.objects.create(
        user_id=user,
        amount=Decimal('500.00'),
        txn_type='credit',
        status='pending',
        gateway_order_id='order_phase0_test',
        payment_gateway='cashfree',
    )
    return client, user, txn


# -------------------------------------------------------------------------
# Server-verified amount
# -------------------------------------------------------------------------

def test_verify_credits_when_gateway_says_paid(wallet_topup_row):
    """Happy path: gateway returns PAID with matching amount → wallet
    balance becomes ₹500.00."""
    from servers.rider.models import Wallet, WalletTransaction
    client, user, txn = wallet_topup_row
    _StubGateway.last_status = {
        'order_status': 'PAID',
        'order_amount': '500.00',
    }
    resp = client.post(
        '/api/v1/rider/wallet/verify/',
        {'gateway_order_id': txn.gateway_order_id},
        format='json',
    )
    assert resp.status_code == 200, resp.content
    txn.refresh_from_db()
    assert txn.status == 'completed'
    assert Wallet.objects.get(user_id=user).balance == Decimal('500.00')


def test_verify_refuses_when_gateway_says_not_paid(wallet_topup_row):
    """Gateway returns ACTIVE → no credit, wallet remains zero."""
    from servers.rider.models import Wallet, WalletTransaction
    client, user, txn = wallet_topup_row
    _StubGateway.last_status = {
        'order_status': 'ACTIVE',
        'order_amount': '500.00',
    }
    resp = client.post(
        '/api/v1/rider/wallet/verify/',
        {'gateway_order_id': txn.gateway_order_id},
        format='json',
    )
    assert resp.status_code == 400
    txn.refresh_from_db()
    assert txn.status != 'completed'
    assert not Wallet.objects.filter(user_id=user).exists() or \
        Wallet.objects.get(user_id=user).balance == Decimal('0.00')


def test_verify_refuses_when_gateway_amount_mismatches(wallet_topup_row):
    """Gateway says PAID for ₹1, txn was for ₹500 → refuse to credit.
    Closes the audit's C2: client-trusted amount."""
    from servers.rider.models import Wallet, WalletTransaction
    client, user, txn = wallet_topup_row
    _StubGateway.last_status = {
        'order_status': 'PAID',
        'order_amount': '1.00',   # Cashfree only saw ₹1
    }
    resp = client.post(
        '/api/v1/rider/wallet/verify/',
        {'gateway_order_id': txn.gateway_order_id},
        format='json',
    )
    assert resp.status_code == 400
    txn.refresh_from_db()
    assert txn.status != 'completed'


# -------------------------------------------------------------------------
# Idempotency: a second verify call is a no-op (does not double-credit)
# -------------------------------------------------------------------------

def test_verify_is_idempotent_on_retry(wallet_topup_row):
    """User taps 'verify' twice (or the SDK callback fires twice). Second
    call must be a no-op, NOT credit ₹500 again."""
    from servers.rider.models import Wallet, WalletTransaction
    client, user, txn = wallet_topup_row
    _StubGateway.last_status = {
        'order_status': 'PAID',
        'order_amount': '500.00',
    }

    r1 = client.post(
        '/api/v1/rider/wallet/verify/',
        {'gateway_order_id': txn.gateway_order_id},
        format='json',
    )
    assert r1.status_code == 200

    r2 = client.post(
        '/api/v1/rider/wallet/verify/',
        {'gateway_order_id': txn.gateway_order_id},
        format='json',
    )
    assert r2.status_code == 200, r2.content
    # Balance still 500 — NOT 1000.
    assert Wallet.objects.get(user_id=user).balance == Decimal('500.00')


# -------------------------------------------------------------------------
# Cross-path idempotency: verify-then-webhook (or vice versa) credits once
# -------------------------------------------------------------------------

def _make_signature(body_bytes, timestamp, secret):
    msg = timestamp + body_bytes.decode('utf-8')
    digest = hmac.new(
        secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode('utf-8')


def test_verify_then_webhook_credits_only_once(wallet_topup_row, settings):
    """Verify path lands first, then the webhook arrives. Wallet credited
    exactly once. Closes the audit's C1: double-credit race."""
    from servers.rider.models import Wallet, WalletTransaction
    from servers.payments.payment_gateways.factory import get_payment_gateway

    # Use a real gateway-name + stub signature path for the webhook leg.
    settings.CASHFREE_WEBHOOK_SECRET = 'test-secret'

    client, user, txn = wallet_topup_row
    _StubGateway.last_status = {
        'order_status': 'PAID',
        'order_amount': '500.00',
    }

    # 1. user-verify path
    r_verify = client.post(
        '/api/v1/rider/wallet/verify/',
        {'gateway_order_id': txn.gateway_order_id},
        format='json',
    )
    assert r_verify.status_code == 200
    assert Wallet.objects.get(user_id=user).balance == Decimal('500.00')

    # 2. webhook path arrives next
    body = json.dumps({
        'type': 'PAYMENT_SUCCESS_WEBHOOK',
        'data': {
            'order': {'order_id': txn.gateway_order_id},
            'payment': {'cf_payment_id': 'cf_test_pay'},
        },
    }).encode()
    ts = str(int(time.time()))
    sig = _make_signature(body, ts, 'test-secret')

    r_hook = client.post(
        '/api/v1/payments/webhook/',
        data=body,
        content_type='application/json',
        HTTP_X_WEBHOOK_SIGNATURE=sig,
        HTTP_X_WEBHOOK_TIMESTAMP=ts,
    )
    assert r_hook.status_code == 200

    # Wallet balance is STILL 500, not 1000.
    assert Wallet.objects.get(user_id=user).balance == Decimal('500.00')


def test_balance_uses_decimal_not_float():
    """Wallet.balance is Decimal; the wallet credit path must keep it that
    way (the audit found `float(wallet.balance) + float(amount)` which
    silently rounded at large balances)."""
    from servers.rider.models import Wallet
    field = Wallet._meta.get_field('balance')
    from django.db import models
    assert isinstance(field, models.DecimalField)
    assert field.max_digits >= 12
    assert field.decimal_places == 2
