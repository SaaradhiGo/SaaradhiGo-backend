"""An unknown payout outcome must not refund the driver's wallet.

`tests/test_payout_single_provider_call.py` proved the containment property -- one
execution makes at most one provider call -- and its docstring named the exposure
it could not close:

    "if Cashfree executes a transfer but our request appears to fail, we mark it
    failed AND refund -- the driver receives the transfer and the refund. That is
    a double-credit, and fixing it needs the provider contract, which is
    unproven."

It does not need the provider contract. It needs the client to notice *where* the
failure happened. `create_upi_payout` returned None for three different
situations:

  1. validation failure (bad UPI id, bad amount) -- no request was ever issued
  2. missing payout token                        -- no request was ever issued
  3. a network failure after requests.post()     -- UNKNOWN: Cashfree may have
                                                    created the transfer

Cases 1 and 2 are safely refundable. Case 3 is not, and it was wearing the same
return value. The wallet was credited on top of a transfer that may have
succeeded.

The classification is entirely local: the gateway knows whether it got as far as
issuing the HTTP request. No provider status call is required to tell "definitely
not sent" from "we do not know".

These tests are structured as reproduction, negative control, and regression, so
the distinction cannot quietly collapse again.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model

from servers.driver.models import Driver, WithdrawalRequest
from servers.payments.payment_gateways.cashfree_gateway import (
    PayoutDispatchUnknown,
)
from servers.rider.models import Wallet, WalletTransaction, get_wallet

User = get_user_model()


class _Gateway:
    """Provider stub that can fail in each distinguishable way."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result
        self._raises = raises

    def create_upi_payout(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._result

    def get_name(self):
        return 'cashfree'


def _with_gateway(gw):
    return mock.patch(
        'servers.payments.payment_gateways.factory.get_payment_gateway_for_payouts',
        return_value=gw,
    )


@pytest.fixture
def driver(db):
    u = User.objects.create_user(phone_number='+919800000901', role='driver')
    d = Driver.objects.create(user_id=u, approved=True, upi_id='qa-test@upi')
    w = get_wallet(u, Wallet.SCOPE_DRIVER)
    w.balance = Decimal('5000.00')
    w.save(update_fields=['balance'])
    return d


def _approved_withdrawal(driver, amount='1000.00'):
    return WithdrawalRequest.objects.create(
        driver=driver, amount=Decimal(amount), payout_method='upi',
        status='approved',
    )


def _balance(driver):
    return get_wallet(driver.user_id, Wallet.SCOPE_DRIVER).balance


def _refunds(driver):
    return WalletTransaction.objects.filter(
        user_id=driver.user_id,
        purpose='refund_failed_withdrawal',
        txn_type='credit',
    )


# ---------------------------------------------------------------------------
# The reproduction: an unknown outcome must hold the money
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_an_unknown_outcome_does_not_refund_the_wallet(driver):
    """The defect. A driver who may already have been paid must not be credited."""
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    before = _balance(driver)
    gw = _Gateway(raises=PayoutDispatchUnknown(
        'transferId withdrawal_1_driver_1: request issued, outcome unknown'))

    with _with_gateway(gw):
        trigger_payout_creation(w)

    assert _balance(driver) == before, (
        'the wallet was refunded for a payout whose outcome is unknown; if '
        'Cashfree did pay, the driver has now been credited twice'
    )
    assert not _refunds(driver).exists(), (
        'a refund transaction was written for an unknown provider outcome'
    )


@pytest.mark.django_db
def test_an_unknown_outcome_is_recorded_as_unresolved_not_failed(driver):
    """'failed' asserts no money moved. That assertion would be unfounded here."""
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _Gateway(raises=PayoutDispatchUnknown('outcome unknown'))

    with _with_gateway(gw):
        trigger_payout_creation(w)

    w.refresh_from_db()
    assert w.status == 'unresolved', (
        f"status is {w.status!r}; an unknown outcome must not claim to be a "
        'definite failure or a definite success'
    )
    assert 'unknown' in (w.failure_reason or '').lower()
    assert 'NOT refunded' in (w.failure_reason or ''), (
        'the reason should tell an operator the money is still held'
    )


@pytest.mark.django_db
def test_an_unresolved_withdrawal_is_not_silently_marked_processed(driver):
    """The opposite lie is equally bad."""
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _Gateway(raises=PayoutDispatchUnknown('outcome unknown'))

    with _with_gateway(gw):
        trigger_payout_creation(w)

    w.refresh_from_db()
    assert w.status != 'processed'
    assert not w.payout_reference_id, (
        'no provider reference was confirmed, so none should be recorded'
    )


@pytest.mark.django_db
def test_an_unresolved_withdrawal_cannot_be_redispatched(driver):
    """It must wait for a human, not loop back into the provider.

    Retrying would depend on Cashfree honouring transferId idempotency, which is
    unverified. Until that is proven in the sandbox, a second provider call is
    not a safe automatic action.
    """
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    with _with_gateway(_Gateway(raises=PayoutDispatchUnknown('unknown'))):
        trigger_payout_creation(w)

    w.refresh_from_db()
    assert w.status == 'unresolved'

    second = _Gateway(result={'payout_id': 'x', 'status': 'PENDING'})
    with _with_gateway(second):
        trigger_payout_creation(w)

    assert second.calls == [], (
        'an unresolved withdrawal reached the provider again; that risks a '
        'second transfer for the same withdrawal'
    )


@pytest.mark.django_db
def test_exactly_one_provider_call_on_an_unknown_outcome(driver):
    """Containment must survive the new path."""
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _Gateway(raises=PayoutDispatchUnknown('unknown'))

    with _with_gateway(gw):
        trigger_payout_creation(w)

    assert len(gw.calls) == 1, f'expected one provider call, got {len(gw.calls)}'


# ---------------------------------------------------------------------------
# Negative control: a DEFINITE rejection must still refund
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_definite_rejection_still_refunds_the_wallet(driver):
    """The control.

    Without this, "stop refunding" would look like a fix while actually stranding
    every driver whose payout was legitimately rejected. A provider that
    understood the request and declined it moved no money, so the driver must get
    their balance back.
    """
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    before = _balance(driver)
    gw = _Gateway(result=None)          # definite rejection, nothing raised

    with _with_gateway(gw):
        trigger_payout_creation(w)

    w.refresh_from_db()
    assert w.status == 'failed'
    assert _balance(driver) == before + w.amount, (
        'a definitely-rejected payout did not return the money to the driver'
    )
    assert _refunds(driver).count() == 1


@pytest.mark.django_db
def test_a_successful_payout_is_unaffected(driver):
    """The other control: the happy path must not have moved."""
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    before = _balance(driver)
    gw = _Gateway(result={'payout_id': 'withdrawal_x', 'status': 'PENDING'})

    with _with_gateway(gw):
        trigger_payout_creation(w)

    w.refresh_from_db()
    assert w.status == 'processed'
    assert w.payout_reference_id
    assert _balance(driver) == before, 'a successful payout must not refund'
    assert not _refunds(driver).exists()


# ---------------------------------------------------------------------------
# The gateway's own classification
# ---------------------------------------------------------------------------

def _gateway_instance():
    from servers.payments.payment_gateways.cashfree_gateway import CashfreeGateway
    return CashfreeGateway()


@pytest.mark.django_db
def test_a_network_failure_after_the_request_raises_unknown(monkeypatch):
    """The request was issued. The answer was lost. That is not a rejection."""
    gw = _gateway_instance()
    monkeypatch.setattr(gw, '_get_payout_token', lambda: 'tok')

    import servers.payments.payment_gateways.cashfree_gateway as mod

    def _boom(*a, **k):
        raise TimeoutError('read timed out')

    monkeypatch.setattr(mod.requests, 'post', _boom)

    with pytest.raises(PayoutDispatchUnknown):
        gw.create_upi_payout(
            upi_id='x@upi', amount=Decimal('100.00'), reference_id='ref-1',
        )


@pytest.mark.django_db
def test_a_5xx_raises_unknown_because_the_provider_may_have_accepted(monkeypatch):
    gw = _gateway_instance()
    monkeypatch.setattr(gw, '_get_payout_token', lambda: 'tok')

    import servers.payments.payment_gateways.cashfree_gateway as mod

    class _Resp:
        status_code = 502
        content = b'{}'

        def json(self):
            return {}

    monkeypatch.setattr(mod.requests, 'post', lambda *a, **k: _Resp())

    with pytest.raises(PayoutDispatchUnknown):
        gw.create_upi_payout(
            upi_id='x@upi', amount=Decimal('100.00'), reference_id='ref-2',
        )


@pytest.mark.django_db
def test_a_4xx_is_a_definite_rejection_and_returns_none(monkeypatch):
    """The provider understood and declined. Safe to refund."""
    gw = _gateway_instance()
    monkeypatch.setattr(gw, '_get_payout_token', lambda: 'tok')

    import servers.payments.payment_gateways.cashfree_gateway as mod

    class _Resp:
        status_code = 400
        content = b'{"message":"invalid vpa"}'

        def json(self):
            return {'message': 'invalid vpa'}

    monkeypatch.setattr(mod.requests, 'post', lambda *a, **k: _Resp())

    assert gw.create_upi_payout(
        upi_id='x@upi', amount=Decimal('100.00'), reference_id='ref-3',
    ) is None


@pytest.mark.django_db
def test_validation_failures_never_reach_the_network_and_are_not_unknown():
    """No request was issued, so these are definite. They must not raise."""
    gw = _gateway_instance()

    assert gw.create_upi_payout(
        upi_id='', amount=Decimal('100.00'), reference_id='r',
    ) is None
    assert gw.create_upi_payout(
        upi_id='x@upi', amount=Decimal('0.00'), reference_id='r',
    ) is None
    assert gw.create_upi_payout(
        upi_id='x@upi', amount='not-a-number', reference_id='r',
    ) is None
