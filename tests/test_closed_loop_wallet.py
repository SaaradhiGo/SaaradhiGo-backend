"""Closed-loop wallet (Phase-0 credits posture) tests.

Covers:
  * GET /api/v1/config/ returns wallet.topups_enabled = False by default
  * POST /rider/wallet/create-order/ returns 503 when disabled (default)
  * POST /rider/wallet/verify/    returns 503 when disabled (default)
  * Flipping WALLET_TOPUPS_ENABLED=True via settings restores the
    top-up endpoints (so we can re-enable for staging tests / future PPI)
  * issue_refund_credit / issue_promo_credit / issue_support_credit:
      - happy path credits the wallet + leaves a WalletTransaction row
      - idempotent: same idempotency_key never double-credits
      - cap-enforced: refused when the projected balance would exceed
        RIDER_CREDIT_BALANCE_CAP
  * Refund endpoint with mode='credit' issues a refund credit instead
    of a Cashfree refund
  * Refund endpoint with mode='credit' falls back to mode='original'
    when the cap would be exceeded
"""

from decimal import Decimal
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Public config endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_public_config_reports_closed_loop_posture(api_client):
    resp = api_client.get('/api/v1/config/')
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    wallet = data['wallet']
    assert wallet['topups_enabled'] is False
    assert wallet['credits_only'] is True
    assert wallet['display_name'] == 'VahanGo Credits'
    assert 'credit' in wallet['refund_modes']
    assert 'original' in wallet['refund_modes']


# ---------------------------------------------------------------------------
# Top-up endpoints gated
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_create_wallet_order_returns_503_when_disabled(auth_client_rider):
    client, _ = auth_client_rider
    resp = client.post(
        '/api/v1/rider/wallet/create-order/',
        {'amount': '500.00'}, format='json',
    )
    assert resp.status_code == 503
    assert resp.json()['error']['code'] == 'FEATURE_DISABLED'


@pytest.mark.django_db
def test_verify_wallet_payment_returns_503_when_disabled(auth_client_rider):
    client, _ = auth_client_rider
    resp = client.post(
        '/api/v1/rider/wallet/verify/',
        {'gateway_order_id': 'any-id'}, format='json',
    )
    assert resp.status_code == 503
    assert resp.json()['error']['code'] == 'FEATURE_DISABLED'


@pytest.mark.django_db
def test_create_wallet_order_works_when_explicitly_enabled(auth_client_rider, settings):
    """Sanity check: future PPI work re-enables this without re-deploying code."""
    settings.WALLET_TOPUPS_ENABLED = True
    client, _ = auth_client_rider
    with patch(
        'servers.payments.payment_gateways.factory.get_payment_gateway'
    ) as mocked:
        mocked.return_value.create_order.return_value = {
            'order_id': 'cf_order_test_123',
            'payment_session_id': 'sess_test',
            'order_token': 'tok_test',
        }
        resp = client.post(
            '/api/v1/rider/wallet/create-order/',
            {'amount': '100.00'}, format='json',
        )
    assert resp.status_code == 201, resp.content


# ---------------------------------------------------------------------------
# Credit issuance
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_issue_refund_credit_credits_wallet_and_writes_txn(db):
    from django.contrib.auth import get_user_model
    from servers.rider.credits import issue_refund_credit
    from servers.rider.models import Wallet, WalletTransaction

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918000000001', role='rider')

    result = issue_refund_credit(
        user=user,
        amount=Decimal('150.00'),
        trip_id=42,
        idempotency_key='refund-test-1',
    )
    assert result.ok is True
    assert result.new_balance == Decimal('150.00')

    wallet = Wallet.objects.get(user_id=user)
    assert wallet.balance == Decimal('150.00')

    txn = WalletTransaction.objects.get(idempotency_key='refund-test-1')
    assert txn.txn_type == 'credit'
    assert txn.status == 'completed'
    assert txn.purpose == 'refund'
    assert txn.reference_id == 'TRIP_42'


@pytest.mark.django_db
def test_credit_issuance_is_idempotent(db):
    from django.contrib.auth import get_user_model
    from servers.rider.credits import issue_promo_credit

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918000000002', role='rider')

    r1 = issue_promo_credit(user, Decimal('50.00'), 'WELCOME', 'promo-key-1')
    r2 = issue_promo_credit(user, Decimal('50.00'), 'WELCOME', 'promo-key-1')

    assert r1.ok is True
    assert r2.ok is True
    assert r2.reason == 'duplicate'
    # Balance was 50 after first, must STILL be 50 after retry (not 100).
    from servers.rider.models import Wallet
    assert Wallet.objects.get(user_id=user).balance == Decimal('50.00')


@pytest.mark.django_db
def test_credit_refused_when_cap_exceeded(db, settings):
    settings.RIDER_CREDIT_BALANCE_CAP = Decimal('200.00')

    from django.contrib.auth import get_user_model
    from servers.rider.credits import issue_promo_credit
    from servers.rider.models import Wallet

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918000000003', role='rider')

    # First credit fits.
    assert issue_promo_credit(user, Decimal('150.00'), 'A', 'k1').ok is True
    # Second would push balance to 350 > 200 cap.
    r = issue_promo_credit(user, Decimal('200.00'), 'B', 'k2')
    assert r.ok is False
    assert r.reason == 'cap_exceeded'
    assert Wallet.objects.get(user_id=user).balance == Decimal('150.00')


@pytest.mark.django_db
def test_credit_rejects_invalid_amounts(db):
    from django.contrib.auth import get_user_model
    from servers.rider.credits import issue_support_credit

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918000000004', role='rider')

    for bad in (None, '', 'abc', '-5', '0'):
        r = issue_support_credit(user, bad, 'TICKET-1', f'k-{bad}')
        assert r.ok is False
        assert r.reason == 'invalid_amount'


# ---------------------------------------------------------------------------
# Refund endpoint mode=credit
# ---------------------------------------------------------------------------

def _make_cancelled_trip_with_completed_online_payment(rider_user, driver_user):
    from decimal import Decimal as D
    from servers.payments.models import Payment
    from servers.ride.models import Trip, TripStatus

    cancelled, _ = TripStatus.objects.get_or_create(status_code='cancelled')
    trip = Trip.objects.create(
        user_id=rider_user,
        driver_id=driver_user.driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        estimated_fare=D('150.00'),
        status_id=cancelled,
    )
    Payment.objects.create(
        trip_id=trip,
        user_id=rider_user,
        amount=D('150.00'),
        method='online',
        status='completed',
        payment_gateway='cashfree',
        cashfree_order_id='cf_order_refund_test_1',
        gateway_order_id='cf_order_refund_test_1',
    )
    return trip


@pytest.mark.django_db
def test_refund_mode_credit_issues_credit_not_cashfree_refund(
    auth_client_rider, auth_client_driver,
):
    from servers.rider.models import Wallet, WalletTransaction

    client, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    trip = _make_cancelled_trip_with_completed_online_payment(rider_user, driver_user)

    with patch(
        'servers.payments.views.get_payment_gateway'
    ) as mocked:
        # If the code wrongly takes the original-method path, this would
        # be the call -- assert it's NOT made.
        mocked.return_value.create_refund.side_effect = AssertionError(
            'Should not call gateway.create_refund for mode=credit'
        )
        mocked.return_value.get_name.return_value = 'cashfree'
        resp = client.post(
            '/api/v1/payments/refund/',
            {'trip_id': trip.id, 'mode': 'credit'},
            format='json',
        )

    assert resp.status_code == 200, resp.content
    body = resp.json()['data']
    assert body['mode'] == 'credit'
    assert body['amount'] == '150.00'

    wallet = Wallet.objects.get(user_id=rider_user)
    assert wallet.balance == Decimal('150.00')
    assert WalletTransaction.objects.filter(
        user_id=rider_user, purpose='refund', amount=Decimal('150.00')
    ).exists()


@pytest.mark.django_db
def test_refund_mode_credit_falls_back_when_cap_exceeded(
    auth_client_rider, auth_client_driver, settings,
):
    settings.RIDER_CREDIT_BALANCE_CAP = Decimal('100.00')  # below the 150 trip fare
    client, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    trip = _make_cancelled_trip_with_completed_online_payment(rider_user, driver_user)

    with patch(
        'servers.payments.views.get_payment_gateway'
    ) as mocked:
        mocked.return_value.create_refund.return_value = {'refund_id': 'rf_test_1'}
        mocked.return_value.get_name.return_value = 'cashfree'
        resp = client.post(
            '/api/v1/payments/refund/',
            {'trip_id': trip.id, 'mode': 'credit'},
            format='json',
        )
    assert resp.status_code == 200, resp.content
    body = resp.json()['data']
    # The cap forced a fallback to original-method.
    assert body['mode'] == 'original'
    mocked.return_value.create_refund.assert_called_once()


@pytest.mark.django_db
def test_refund_endpoint_rejects_bad_mode(
    auth_client_rider, auth_client_driver,
):
    client, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    trip = _make_cancelled_trip_with_completed_online_payment(rider_user, driver_user)
    resp = client.post(
        '/api/v1/payments/refund/',
        {'trip_id': trip.id, 'mode': 'bitcoin'},
        format='json',
    )
    assert resp.status_code == 400
    assert resp.json()['error']['code'] == 'INVALID_MODE'
