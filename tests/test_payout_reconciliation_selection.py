"""Which payouts the reconciler picks up, proven from our own state machine.

`reconcile_stuck_withdrawals` has been scheduled every ten minutes and has
reconciled nothing, ever. The known cause was a missing provider method. It was
not the only one, and it was not the important one:

    status__in=['processing', 'approved']

`'processing'` is not a status this system has -- it is absent from
`WithdrawalRequest.STATUS_CHOICES` and no code writes it. `'approved'` is the state
*before* dispatch, so it has no `payout_reference_id`, which the same queryset then
required. The two conditions were mutually exclusive, so **the one state that can
actually get stuck -- `processed`, dispatched, awaiting a webhook that never came --
was the only state never selected.**

Implementing the provider call without fixing this would have produced a task that
reported success while still reconciling nothing.

These tests cover only the selection, which is provider-independent and therefore
provable without inventing any Cashfree behaviour.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver, WithdrawalRequest
from servers.payments.tasks import (
    WITHDRAWAL_RECON_FAILURE_HANDLING_ENABLED,
    reconcile_stuck_withdrawals,
    stuck_withdrawal_candidates,
)

User = get_user_model()


@pytest.fixture
def driver(db):
    u = User.objects.create_user(phone_number='+919800000501', role='driver')
    return Driver.objects.create(user_id=u, approved=True)


def _withdrawal(driver, status, *, processed_minutes_ago=None,
                requested_minutes_ago=60, reference='withdrawal_1_driver_1'):
    """A withdrawal in a given state.

    `processed_at` is set explicitly rather than by the real code path, so each
    test states the state it is exercising instead of depending on a long
    maker/checker sequence.
    """
    w = WithdrawalRequest.objects.create(
        driver=driver, amount=Decimal('1000.00'), payout_method='upi',
        status=status, payout_reference_id=reference,
    )
    # requested_at is auto_now_add, so it needs an UPDATE to move.
    WithdrawalRequest.objects.filter(pk=w.pk).update(
        requested_at=timezone.now() - timedelta(minutes=requested_minutes_ago),
        processed_at=(None if processed_minutes_ago is None
                      else timezone.now() - timedelta(minutes=processed_minutes_ago)),
    )
    w.refresh_from_db()
    return w


def _ids(qs):
    return {w.id for w in qs}


# ---------------------------------------------------------------------------
# What must be selected
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_truly_stuck_transfer_is_selected(driver):
    """Dispatched 30 minutes ago, still 'processed', no webhook. The whole point."""
    stuck = _withdrawal(driver, 'processed', processed_minutes_ago=30)
    assert _ids(stuck_withdrawal_candidates()) == {stuck.id}


@pytest.mark.django_db
def test_an_old_request_dispatched_recently_is_judged_on_the_transfer_time(driver):
    """Maker/checker is a human step, so request time is the wrong clock.

    A withdrawal requested five days ago and only dispatched twenty minutes ago is
    a live, reconcilable transfer. Under the old window -- measured from
    `requested_at` with a 48-hour lookback -- it was already too old to be picked
    up the moment it was dispatched, so it could never have been reconciled even
    after the provider call was implemented.
    """
    w = _withdrawal(driver, 'processed', processed_minutes_ago=20,
                    requested_minutes_ago=60 * 24 * 5)
    assert _ids(stuck_withdrawal_candidates()) == {w.id}


@pytest.mark.django_db
def test_a_transfer_dispatched_just_inside_the_lookback_is_still_selected(driver):
    w = _withdrawal(driver, 'processed', processed_minutes_ago=(48 * 60) - 30)
    assert _ids(stuck_withdrawal_candidates()) == {w.id}


# ---------------------------------------------------------------------------
# What must NOT be selected
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_an_approved_but_undispatched_row_is_not_selected(driver):
    """There is no transfer to ask about yet.

    'approved' also stamps `processed_at` -- the column is overloaded -- which is
    exactly why the status filter is not optional.
    """
    _withdrawal(driver, 'approved', processed_minutes_ago=30, reference='')
    assert _ids(stuck_withdrawal_candidates()) == set()


@pytest.mark.django_db
def test_an_approved_row_that_somehow_has_a_reference_is_still_not_selected(driver):
    """Belt and braces: the status filter alone must exclude it."""
    _withdrawal(driver, 'approved', processed_minutes_ago=30,
                reference='withdrawal_9_driver_9')
    assert _ids(stuck_withdrawal_candidates()) == set()


@pytest.mark.django_db
def test_a_terminal_success_is_not_selected(driver):
    """Completed by the webhook. Nothing to reconcile."""
    _withdrawal(driver, 'completed', processed_minutes_ago=30)
    assert _ids(stuck_withdrawal_candidates()) == set()


@pytest.mark.django_db
def test_a_terminal_failure_is_not_selected(driver):
    """Failed and refunded by the webhook. Re-examining it could double-refund."""
    _withdrawal(driver, 'failed', processed_minutes_ago=30)
    assert _ids(stuck_withdrawal_candidates()) == set()


@pytest.mark.django_db
def test_a_rejected_request_is_not_selected(driver):
    """Rejected by an admin, never dispatched -- and it stamps processed_at too."""
    _withdrawal(driver, 'rejected', processed_minutes_ago=30)
    assert _ids(stuck_withdrawal_candidates()) == set()


@pytest.mark.django_db
def test_a_pending_request_is_not_selected(driver):
    _withdrawal(driver, 'pending', processed_minutes_ago=None)
    assert _ids(stuck_withdrawal_candidates()) == set()


@pytest.mark.django_db
def test_a_freshly_dispatched_transfer_is_not_selected(driver):
    """Inside the grace window. The webhook deserves a chance to arrive first."""
    _withdrawal(driver, 'processed', processed_minutes_ago=1)
    assert _ids(stuck_withdrawal_candidates()) == set()


@pytest.mark.django_db
def test_a_transfer_older_than_the_lookback_is_not_selected(driver):
    """Beyond the window this is a manual investigation, not a sweep."""
    _withdrawal(driver, 'processed', processed_minutes_ago=60 * 72)
    assert _ids(stuck_withdrawal_candidates()) == set()


@pytest.mark.django_db
def test_a_dispatched_row_with_no_reference_is_not_selected(driver):
    """Nothing to ask the provider about. Dispatch writes both together, so this
    is an anomaly, better left visible than polled blindly."""
    _withdrawal(driver, 'processed', processed_minutes_ago=30, reference='')
    assert _ids(stuck_withdrawal_candidates()) == set()


@pytest.mark.django_db
def test_a_processed_row_with_no_processed_at_is_not_selected(driver):
    """Defensive: both writers of 'processed' set processed_at in the same save,
    so a null here means something unknown happened. Do not guess its age."""
    _withdrawal(driver, 'processed', processed_minutes_ago=None)
    assert _ids(stuck_withdrawal_candidates()) == set()


# ---------------------------------------------------------------------------
# Ordering, bounds, and the old queryset
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_candidates_are_bounded_and_oldest_first(driver):
    """A backlog must not turn one tick into an unbounded unit of work."""
    for minutes in (30, 60, 90, 120):
        _withdrawal(driver, 'processed', processed_minutes_ago=minutes,
                    reference=f'withdrawal_{minutes}_driver_1')

    assert len(stuck_withdrawal_candidates()) == 4
    limited = list(stuck_withdrawal_candidates(limit=2))
    assert len(limited) == 2
    # Oldest transfer first: the one waiting longest is reconciled first.
    assert limited[0].processed_at < limited[1].processed_at


@pytest.mark.django_db
def test_the_old_filter_would_have_matched_nothing(driver):
    """The defect, stated as a test, so it cannot quietly return.

    A realistic population: one genuinely stuck transfer plus the states around it.
    The old queryset finds none of them; the new one finds exactly the stuck one.
    """
    stuck = _withdrawal(driver, 'processed', processed_minutes_ago=30)
    _withdrawal(driver, 'approved', processed_minutes_ago=40, reference='')
    _withdrawal(driver, 'completed', processed_minutes_ago=50)
    _withdrawal(driver, 'failed', processed_minutes_ago=60)

    old_style = WithdrawalRequest.objects.filter(
        status__in=['processing', 'approved'],
        requested_at__lt=timezone.now() - timedelta(minutes=5),
        requested_at__gt=timezone.now() - timedelta(hours=48),
    ).exclude(payout_reference_id='').exclude(payout_reference_id__isnull=True)

    assert list(old_style) == [], 'the old filter could never match anything'
    assert _ids(stuck_withdrawal_candidates()) == {stuck.id}


# ---------------------------------------------------------------------------
# The provider boundary stays closed
# ---------------------------------------------------------------------------

def test_failure_handling_is_disabled_by_default():
    """The webhook refunds on failure and this task does not.

    Until that asymmetry is resolved, reconciliation must not be able to mark a
    payout failed -- that would leave a driver debited with nothing returned.
    """
    assert WITHDRAWAL_RECON_FAILURE_HANDLING_ENABLED is False


@pytest.mark.django_db
def test_the_task_reports_stuck_payouts_without_polling_the_provider(driver):
    """No provider contract has been verified, so nothing is polled.

    What the task now does that it could not before: say how many payouts are
    sitting unresolved. Previously it returned early on a queryset that was empty
    by construction, so the number was always invisible.
    """
    stuck = _withdrawal(driver, 'processed', processed_minutes_ago=30)

    result = reconcile_stuck_withdrawals()

    assert result['ok'] is False
    assert result['reason'] == 'provider_polling_unimplemented'
    assert result['candidates'] == 1
    assert result['candidate_ids'] == [stuck.id]


@pytest.mark.django_db
def test_the_task_moves_no_money_and_changes_no_status(driver):
    """It is a read while the provider boundary is closed."""
    w = _withdrawal(driver, 'processed', processed_minutes_ago=30)
    before = WithdrawalRequest.objects.filter(pk=w.pk).values().first()

    reconcile_stuck_withdrawals()

    assert WithdrawalRequest.objects.filter(pk=w.pk).values().first() == before


@pytest.mark.django_db
def test_the_selection_ignores_other_drivers_nothing(driver):
    """Sanity: selection is by state, not by driver, and finds an empty set clean."""
    assert _ids(stuck_withdrawal_candidates()) == set()


# ---------------------------------------------------------------------------
# The settle path, exercised with an injected provider
# ---------------------------------------------------------------------------
#
# These use a stub gateway. They do NOT claim anything about Cashfree's real
# contract -- that is unverified and the boundary ships closed. What they pin is
# our own logic on the far side of it, because two defects were hiding there that
# only appear once a provider exists: the in-lock re-check used the OLD status
# vocabulary (so every selected row would have been skipped), and the success
# branch overwrote processed_at (relabelling dispatch time as reconciliation
# time and destroying the timestamp the selection window depends on).

class _StubGateway:
    def __init__(self, status_value, fail=False):
        self._status = status_value
        self._fail = fail
        self.calls = []

    def get_payout_status(self, reference):
        self.calls.append(reference)
        if self._fail:
            raise RuntimeError('provider unavailable')
        return {'status': self._status, 'failure_reason': 'stub reason'}


def _with_gateway(gateway):
    return mock.patch(
        'servers.payments.payment_gateways.factory.get_payment_gateway_for_payouts',
        return_value=gateway,
    )


@pytest.mark.django_db
def test_a_confirmed_success_is_settled_and_keeps_its_dispatch_time(driver):
    """The re-check must accept the status the selection actually produced."""
    w = _withdrawal(driver, 'processed', processed_minutes_ago=30)
    dispatched_at = WithdrawalRequest.objects.get(pk=w.pk).processed_at

    gw = _StubGateway('SUCCESS')
    with _with_gateway(gw):
        result = reconcile_stuck_withdrawals()

    assert gw.calls == [w.payout_reference_id]
    assert result['ok'] is True
    assert result['settled'] == 1

    row = WithdrawalRequest.objects.get(pk=w.pk)
    assert row.status == 'completed'
    assert row.payout_status == 'SUCCESS'
    assert row.processed_at == dispatched_at, \
        'processed_at means "dispatched at" and must survive reconciliation'


@pytest.mark.django_db
def test_a_confirmed_failure_is_left_alone_while_handling_is_disabled(driver):
    """No status change, and above all no refund, until the policy is settled."""
    from servers.rider.models import WalletTransaction

    w = _withdrawal(driver, 'processed', processed_minutes_ago=30)
    before = WithdrawalRequest.objects.filter(pk=w.pk).values().first()

    with _with_gateway(_StubGateway('FAILED')):
        result = reconcile_stuck_withdrawals()

    assert result['skipped'] == 1
    assert result['failed'] == 0
    assert WithdrawalRequest.objects.filter(pk=w.pk).values().first() == before
    assert not WalletTransaction.objects.exists(), 'reconciliation must never refund'


@pytest.mark.django_db
def test_an_unrecognised_provider_status_is_skipped_not_guessed(driver):
    """An unknown vocabulary must never be read as success or failure."""
    w = _withdrawal(driver, 'processed', processed_minutes_ago=30)

    with _with_gateway(_StubGateway('SOME_NEW_STATE')):
        result = reconcile_stuck_withdrawals()

    assert result['skipped'] == 1
    assert WithdrawalRequest.objects.get(pk=w.pk).status == 'processed'


@pytest.mark.django_db
def test_a_provider_error_skips_rather_than_mutating(driver):
    w = _withdrawal(driver, 'processed', processed_minutes_ago=30)

    with _with_gateway(_StubGateway('SUCCESS', fail=True)):
        result = reconcile_stuck_withdrawals()

    assert result['skipped'] == 1
    assert WithdrawalRequest.objects.get(pk=w.pk).status == 'processed'
