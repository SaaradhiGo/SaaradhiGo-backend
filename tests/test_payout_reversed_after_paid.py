"""A payout reported PAID and then reversed by the bank (owner ruling, AUDIT 27.71).

Mark it reversed; credit the amount back as a NEW ledger line (append only); raise a Finance alert;
tell the driver through the notifications system. Before this the late reversal was ignored, so the
balance said the money had been paid out when it never stayed with the driver. Both payout adapters
(Cashfree and RazorpayX) go through the same handler, so every test runs on both.
"""

from decimal import Decimal as D
from unittest import mock

import pytest

from test_payout_provider_contract import _balance, _refunds, _withdrawal, driver, rx, world  # noqa: F401
from servers.command_centre.models import FINANCE_ALERT_KINDS, OperatorAlert
from servers.driver.models import WithdrawalRequest
from servers.payments import payout_reversal
from servers.rider.models import WalletTransaction

pytestmark = pytest.mark.django_db


def _deliver(client, world, kind, reference, event_id):
    body, headers = world.webhook(kind, reference, event_id)
    return client.post(world.webhook_path, data=body, content_type='application/json', **headers)


def _paid(client, world, driver):
    w = _withdrawal(driver, status='processed')
    w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
    w.save(update_fields=['payout_reference_id'])
    assert _deliver(client, world, 'success', w.payout_reference_id, 'evt_paid').status_code == 200
    w.refresh_from_db()
    assert w.status == 'completed'
    return w


def _credits(driver):
    return WalletTransaction.objects.filter(user_id=driver.user_id,
                                            purpose='refund_reversed_withdrawal')


class TestReversedAfterPaid:
    def test_it_is_marked_reversed_and_the_amount_comes_back_once(self, world, driver, api_client):
        w = _paid(api_client, world, driver)
        before = _balance(driver)
        response = _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev')
        assert response.status_code == 200
        w.refresh_from_db()
        assert (w.status, w.failure_reason) == ('reversed', payout_reversal.FAILURE_REASON)
        credit = _credits(driver).get()
        assert (credit.txn_type, credit.status, credit.amount_minor, credit.reference_id) == (
            'credit', 'completed', w.amount_minor, f'refund_reversed_withdrawal_{w.id}')
        assert _balance(driver) == before + D('1000.00')
        # Not the before-payment refund path.
        assert _refunds(driver).count() == 0

    def test_a_resent_reversal_writes_nothing_more(self, world, driver, api_client):
        w = _paid(api_client, world, driver)
        before = _balance(driver)
        _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev1')
        _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev2')
        assert _credits(driver).count() == 1
        assert _balance(driver) == before + D('1000.00')
        assert OperatorAlert.objects.filter(kind='finance.payout_reversed').count() == 1

    def test_a_late_success_does_not_mark_it_paid_again(self, world, driver, api_client):
        w = _paid(api_client, world, driver)
        _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev')
        _deliver(api_client, world, 'success', w.payout_reference_id, 'evt_late')
        w.refresh_from_db()
        assert w.status == 'reversed'

    def test_the_original_withdrawal_debit_stays(self, world, driver, api_client):
        w = _paid(api_client, world, driver)
        rows_before = set(WalletTransaction.objects.filter(user_id=driver.user_id)
                          .values_list('id', 'amount_minor', 'txn_type', 'status', 'purpose'))
        _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev')
        rows_after = set(WalletTransaction.objects.filter(user_id=driver.user_id)
                         .values_list('id', 'amount_minor', 'txn_type', 'status', 'purpose'))
        assert rows_before < rows_after and len(rows_after - rows_before) == 1

    def test_a_reversal_before_payment_is_still_a_failed_payout(self, world, driver, api_client):
        w = _withdrawal(driver, status='processed')
        w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
        w.save(update_fields=['payout_reference_id'])
        _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_early')
        w.refresh_from_db()
        assert w.status == 'failed' and _refunds(driver).count() == 1
        assert not _credits(driver).exists()
        assert not OperatorAlert.objects.filter(kind='finance.payout_reversed').exists()


class TestFinanceIsAlertedAndTheDriverTold:
    def test_one_finance_alert_with_the_amount(self, world, driver, api_client):
        w = _paid(api_client, world, driver)
        _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev')
        alert = OperatorAlert.objects.get(kind='finance.payout_reversed')
        assert alert.kind in FINANCE_ALERT_KINDS and alert.severity == 'warning'
        assert (alert.payload['withdrawal_id'], alert.payload['amount_minor'],
                alert.payload['currency']) == (w.id, w.amount_minor, w.currency)
        assert alert.source_id == str(w.id)
        assert '+91' not in alert.title and driver.user_id.full_name not in alert.title

    def test_the_driver_is_notified_after_the_commit(self, world, driver, api_client,
                                                     django_capture_on_commit_callbacks):
        w = _paid(api_client, world, driver)
        with mock.patch('servers.notifications.dispatcher.notify') as notify:
            with django_capture_on_commit_callbacks(execute=True):
                _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev')
        notify.assert_called_once()
        args, kwargs = notify.call_args
        assert args[0] == 'payout.reversed' and args[1] == driver.user_id
        assert args[2]['amount'].endswith('1,000.00')
        assert kwargs['idempotency_key'] == f'payout.reversed:{w.id}'
        assert kwargs['channels'] == ('in_app', 'push')

    def test_a_failing_notice_never_undoes_the_credit(self, world, driver, api_client,
                                                      django_capture_on_commit_callbacks):
        w = _paid(api_client, world, driver)
        with mock.patch('servers.notifications.dispatcher.notify', side_effect=RuntimeError):
            with django_capture_on_commit_callbacks(execute=True):
                response = _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev')
        assert response.status_code == 200
        assert _credits(driver).count() == 1
        assert WithdrawalRequest.objects.get(pk=w.pk).status == 'reversed'

    def test_the_template_is_seeded_transactional_on_in_app_and_push(self):
        from servers.notifications.seed_data import seed_rows

        rows = [(key, channel, country, copy) for key, channel, country, copy, _ in seed_rows()
                if key == 'payout.reversed']
        assert {(c, n) for _, c, n, _ in rows} == {('in_app', 'IN'), ('push', 'IN'),
                                                   ('in_app', 'US'), ('push', 'US')}
        assert all(copy.get('category', 'transactional') == 'transactional' for *_, copy in rows)
        assert all(copy['variables'] == ['amount'] for *_, copy in rows)


class TestStatementAndReports:
    def test_the_statement_names_the_line_and_links_the_withdrawal(self):
        from servers.payments.ledger import ENTRY_KINDS, ENTRY_LABELS, WITHDRAWAL_REF
        from servers.payments.scope import DRIVER_LEDGER_PURPOSES

        assert 'refund_reversed_withdrawal' in DRIVER_LEDGER_PURPOSES
        assert ENTRY_KINDS['refund_reversed_withdrawal'] == 'withdrawal_reversed'
        assert ENTRY_LABELS['refund_reversed_withdrawal'] == 'Payout reversed by the bank (returned)'
        assert WITHDRAWAL_REF.match('refund_reversed_withdrawal_12').group(1) == '12'
