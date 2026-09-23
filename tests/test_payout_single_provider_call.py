"""How many times can one withdrawal reach the payout provider?

This file exists because I reported, twice, that `execute_upi_payout` "retries up to
three times on a stable transferId" and that this was a live duplicate-payment risk
in production. Reading the code carefully, that was wrong, and these tests are the
proof either way — the number of provider creation calls is asserted directly by
counting them.

What is actually there, in order:

1. `initiate_upi_payout` returns early if `withdrawal.payout_reference_id` is
   already set — a payout that reached Cashfree can never be re-created.
2. The `max_retries = 3` guard below that is **unreachable in the normal flow**,
   because...
3. ...`trigger_payout_creation` treats the FIRST failure as terminal: it sets
   `status='failed'` and refunds the driver's wallet immediately.
4. And a `failed` withdrawal is refused at the top of `trigger_payout_creation`
   (`status not in ("approved","processed")`), so it cannot be dispatched again.

So the retry cap never binds, and one execution produces at most one provider call.
The genuine exposure is different and is NOT addressed here: if Cashfree executes a
transfer but our request appears to fail, we mark it failed AND refund — the driver
receives the transfer and the refund. That is a double-credit, and fixing it needs
the provider contract, which is unproven.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model

from servers.driver.models import Driver, WithdrawalRequest
from servers.rider.models import Wallet, WalletTransaction, get_wallet

User = get_user_model()


class _CountingGateway:
    """Counts provider creation calls. The whole point of the file."""

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
    u = User.objects.create_user(phone_number='+919800000801', role='driver')
    d = Driver.objects.create(user_id=u, approved=True, upi_id='qa-test@upi')
    # Fund the settlement wallet so the debit at request time is realistic.
    w = get_wallet(u, Wallet.SCOPE_DRIVER)
    w.balance = Decimal('5000.00')
    w.save(update_fields=['balance'])
    return d


def _approved_withdrawal(driver, amount='1000.00'):
    return WithdrawalRequest.objects.create(
        driver=driver, amount=Decimal(amount), payout_method='upi',
        status='approved',
    )


# ---------------------------------------------------------------------------
# The containment property, asserted by counting
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_one_execution_makes_exactly_one_provider_call_on_success(driver):
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _CountingGateway(result={'payout_id': 'withdrawal_1_driver_1',
                                  'status': 'PENDING'})

    with _with_gateway(gw):
        trigger_payout_creation(w)

    assert len(gw.calls) == 1, f'expected exactly one provider call, got {len(gw.calls)}'
    w.refresh_from_db()
    assert w.status == 'processed'
    assert w.payout_reference_id


@pytest.mark.django_db
def test_one_execution_makes_exactly_one_provider_call_on_failure(driver):
    """The claim under test. A failing attempt must not be retried into a second
    provider call within one execution."""
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _CountingGateway(result=None)          # gateway returns no result

    with _with_gateway(gw):
        trigger_payout_creation(w)

    assert len(gw.calls) == 1, f'expected exactly one provider call, got {len(gw.calls)}'


@pytest.mark.django_db
def test_a_failed_withdrawal_cannot_be_dispatched_again(driver):
    """The reason the retry cap never binds: failure is terminal.

    `trigger_payout_creation` refuses anything not in (approved, processed), so a
    second dispatch attempt makes no provider call at all.
    """
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _CountingGateway(result=None)
    with _with_gateway(gw):
        trigger_payout_creation(w)

    w.refresh_from_db()
    assert w.status == 'failed'

    gw2 = _CountingGateway(result={'payout_id': 'x', 'status': 'PENDING'})
    with _with_gateway(gw2):
        trigger_payout_creation(w)

    assert gw2.calls == [], 'a failed withdrawal must not reach the provider again'


@pytest.mark.django_db
def test_a_dispatched_withdrawal_is_never_re_sent(driver):
    """The strongest existing guard: a stored payout reference blocks re-creation.

    This is what protects a payout that DID reach Cashfree, and it is why the
    duplicate-payment story is narrower than I previously reported.
    """
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _CountingGateway(result={'payout_id': 'withdrawal_ref_1', 'status': 'PENDING'})
    with _with_gateway(gw):
        trigger_payout_creation(w)

    w.refresh_from_db()
    ref = w.payout_reference_id
    assert ref

    # Force it back to a dispatchable status, as a re-approval would, and try again.
    WithdrawalRequest.objects.filter(pk=w.pk).update(status='approved')
    w.refresh_from_db()

    gw2 = _CountingGateway(result={'payout_id': 'DIFFERENT', 'status': 'PENDING'})
    with _with_gateway(gw2):
        trigger_payout_creation(w)

    assert gw2.calls == [], 'an already-dispatched payout must not be re-created'
    w.refresh_from_db()
    assert w.payout_reference_id == ref, 'the original reference must be preserved'


@pytest.mark.django_db
def test_a_transport_exception_also_yields_one_call_at_most(driver):
    """A network failure is the in-doubt case. It must not become two calls."""
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _CountingGateway(raises=RuntimeError('connection reset'))

    with _with_gateway(gw):
        try:
            trigger_payout_creation(w)
        except RuntimeError:
            pass

    assert len(gw.calls) == 1


# ---------------------------------------------------------------------------
# The refund side, which is where the real exposure is
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_failed_payout_refunds_the_driver_exactly_once(driver):
    """Documents the behaviour, including the risk it carries.

    On failure the wallet is refunded immediately. That is right when the transfer
    genuinely never happened, and wrong when Cashfree executed it and our request
    merely appeared to fail — then the driver receives the transfer AND the refund.
    Changing that needs the provider contract, so this test pins today's behaviour
    rather than altering it.
    """
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _CountingGateway(result=None)

    with _with_gateway(gw):
        trigger_payout_creation(w)

    refunds = WalletTransaction.objects.filter(
        user_id=driver.user_id,
        purpose__in=('refund_failed_withdrawal', 'refund_rejected_withdrawal'),
    )
    assert refunds.count() == 1, 'exactly one refund, never two'


@pytest.mark.django_db
def test_repeated_failure_handling_cannot_double_refund(driver):
    """Idempotency of the refund itself, independent of the payout path."""
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    with _with_gateway(_CountingGateway(result=None)):
        trigger_payout_creation(w)
    # Attempt to drive the failure path a second time.
    WithdrawalRequest.objects.filter(pk=w.pk).update(status='approved')
    w.refresh_from_db()
    with _with_gateway(_CountingGateway(result=None)):
        trigger_payout_creation(w)

    refunds = WalletTransaction.objects.filter(
        user_id=driver.user_id,
        purpose__in=('refund_failed_withdrawal', 'refund_rejected_withdrawal'),
    )
    assert refunds.count() == 1, f'expected one refund, found {refunds.count()}'


# ---------------------------------------------------------------------------
# The identifier must stay stable
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_transfer_id_is_derived_from_the_withdrawal_not_the_clock(driver):
    """Whatever is decided about idempotency, the identifier must stay derivable.

    A timestamped transferId would make every attempt a definitively new transfer,
    converting an unproven risk into a certain one.
    """
    from servers.driver.services import trigger_payout_creation

    w = _approved_withdrawal(driver)
    gw = _CountingGateway(result={'payout_id': 'ref', 'status': 'PENDING'})
    with _with_gateway(gw):
        trigger_payout_creation(w)

    sent = gw.calls[0]['reference_id']
    assert str(w.id) in sent and str(driver.id) in sent
    assert 'withdrawal' in sent
