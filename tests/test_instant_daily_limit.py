"""The daily Withdraw now limit (owner, 10 Nov): a count and a total per driver's local day, set per
market (country -> state -> city) through maker-checker. Failed or reversed requests do not count;
scheduled payouts are never limited. Statements show each Withdraw now's provider fee, and
reconciliation checks it against the provider's report.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

import pytest

from finance_world import client_for, operator
from servers.driver.models import WithdrawalRequest
from servers.payments import ledger
from servers.payments.models import PayoutCountryConfig, PayoutScopeOverride, ReconciliationItem, WebhookEvent
from servers.payments.payout_scope import effective_for_driver, instant_used_today
from servers.rider.models import WalletTransaction
from test_finance_actions import approve_request, body_of, checker, get, post
from test_payout_settings_extension import BASE, funded, india, override  # noqa: F401
from test_pricing_module import geo, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]

REQUEST = '/api/v1/driver/withdrawals/request/'
QUOTE = '/api/v1/driver/withdrawals/instant/quote/'


def limits(india, count=None, amount=None):  # noqa: F811
    PayoutCountryConfig.objects.filter(pk=india.cfg.pk).update(
        instant_max_per_day=count, instant_max_amount_per_day_minor=amount)


def instant(client, amount='200'):
    with mock.patch('servers.driver.services.trigger_payout_creation'):
        return client.post(REQUEST, {'amount': amount, 'instant': True}, format='json')


class TestCountLimit:
    def test_the_fourth_is_refused_with_the_reason_shown_before_tapping(self, india):  # noqa: F811
        limits(india, count=3)
        client = client_for(funded(india, 'L1').user_id)
        for _ in range(3):
            assert instant(client).status_code == 201
        quote = client.get(f'{QUOTE}?amount=200').json()['data']
        assert quote['limit_reached'] is True and quote['valid'] is False
        assert quote['limit_message'].startswith('Daily limit reached: 3 instant withdrawals.')
        assert 'Your next scheduled payout is ' in quote['limit_message']
        r = instant(client)
        assert r.status_code == 400 and r.json()['error']['code'] == 'INSTANT_DAILY_COUNT'
        assert r.json()['error']['message'] == quote['limit_message']
        assert WithdrawalRequest.objects.filter(origin='instant').count() == 3


class TestAmountLimit:
    def test_the_total_for_the_day_is_held(self, india):  # noqa: F811
        limits(india, amount=50_000)                                  # ₹500 a day
        client = client_for(funded(india, 'L2').user_id)
        assert instant(client, '300').status_code == 201
        quote = client.get(f'{QUOTE}?amount=250').json()['data']
        assert quote['limit_reached'] is True and '₹200.00 left today' in quote['limit_message']
        r = instant(client, '250')
        assert r.status_code == 400 and r.json()['error']['code'] == 'INSTANT_DAILY_AMOUNT'
        assert instant(client, '200').status_code == 201              # exactly up to the limit


class TestLocalDay:
    def test_the_day_turns_at_local_midnight(self, india):  # noqa: F811
        driver = funded(india, 'L3')
        _, config = effective_for_driver(driver)
        late = WithdrawalRequest.objects.create(driver=driver, amount='200', origin='instant', status='completed')
        early = WithdrawalRequest.objects.create(driver=driver, amount='200', origin='instant', status='completed')
        # 23:59 IST on the 9th and 00:01 IST on the 10th.
        WithdrawalRequest.objects.filter(pk=late.pk).update(
            requested_at=datetime(2026, 11, 9, 18, 29, tzinfo=dt_timezone.utc))
        WithdrawalRequest.objects.filter(pk=early.pk).update(
            requested_at=datetime(2026, 11, 9, 18, 31, tzinfo=dt_timezone.utc))
        now = datetime(2026, 11, 10, 6, 0, tzinfo=dt_timezone.utc)     # 11:30 IST on the 10th
        assert instant_used_today(driver, config, now) == (1, 20_000)
        before_midnight = datetime(2026, 11, 9, 18, 29, 59, tzinfo=dt_timezone.utc)  # 23:59:59 IST, the 9th
        assert instant_used_today(driver, config, before_midnight) == (1, 20_000)


class TestWhatCounts:
    def test_failed_reversed_and_rejected_do_not_count(self, india):  # noqa: F811
        limits(india, count=1)
        driver = funded(india, 'L4')
        client = client_for(driver.user_id)
        assert instant(client).status_code == 201
        for status in ('failed', 'reversed', 'rejected'):
            WithdrawalRequest.objects.filter(driver=driver).update(status=status)
            assert instant_used_today(driver, effective_for_driver(driver)[1]) == (0, 0)
        assert instant(client).status_code == 201

    def test_scheduled_payouts_are_never_limited_and_never_count(self, india):  # noqa: F811
        limits(india, count=1, amount=10_000)
        driver = funded(india, 'L5')
        WithdrawalRequest.objects.create(driver=driver, amount='900', origin='scheduled', status='completed')
        WithdrawalRequest.objects.create(driver=driver, amount='900', origin='driver', status='completed')
        assert instant_used_today(driver, effective_for_driver(driver)[1]) == (0, 0)
        client = client_for(driver.user_id)
        assert instant(client, '100').status_code == 201
        # The limit is reached for Withdraw now, but a payout on request is not limited by it.
        with mock.patch('servers.driver.services.is_withdrawal_blocked', return_value=False):
            r = client.post(REQUEST, {'amount': '500'}, format='json')
        assert r.status_code == 201, r.content[:300]


class TestSettings:
    def test_a_city_sets_its_own_limit_through_the_checker(self, india):  # noqa: F811
        limits(india, count=3, amount=5_000_000)
        maker, _ = operator('finance', india.geo, 'IN')
        r = post(maker, f'{BASE}/payout-config/IN/', {
            'scope': 'IN-TG-HYD', 'instant_max_per_day': 2, 'reason': 'Hyderabad pilot',
            'idempotency_key': 'hyd-limit'}, expect=202)
        assert not PayoutScopeOverride.objects.exists()
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        _, config = effective_for_driver(funded(india, 'L6'))
        assert config.instant_max_per_day == 2 and config.sources['instant_max_per_day'] == 'IN-TG-HYD'
        assert config.instant_max_amount_per_day_minor == 5_000_000      # from the country
        country = next(c for c in get(maker, f'{BASE}/payout-config/')['countries'] if c['country'] == 'IN')
        assert country['config']['instant_max_per_day'] == 3
        assert country['config']['instant_max_amount_per_day_minor'] == 5_000_000
        hyd = next(x for x in country['scopes'] if x['scope'] == 'IN-TG-HYD')
        assert hyd['effective']['instant_max_per_day'] == 2

    def test_a_limit_below_one_is_refused(self, india):  # noqa: F811
        maker, _ = operator('finance', india.geo, 'IN')
        r = post(maker, f'{BASE}/payout-config/IN/', {
            'scope': 'IN-TG-HYD', 'instant_max_per_day': 0, 'reason': 'x', 'idempotency_key': 'z'}, expect=400)
        assert r.json()['error']['code'] == 'INVALID_LIMIT'


class TestStatementFeeLine:
    def test_each_withdraw_now_shows_its_provider_fee(self, india):  # noqa: F811
        driver = funded(india, 'L7')
        client = client_for(driver.user_id)
        assert instant(client, '200').status_code == 201
        row = client.get('/api/v1/driver/withdrawals/history/').json()['data']['results'][0]
        assert row['statement_line'] == 'Withdrawn ₹200.00 · provider fee ₹4.72 · received ₹195.28'
        assert row['origin'] == 'instant' and row['fee_minor'] == 472
        entries = ledger.entry_dicts(driver, WalletTransaction.objects.filter(
            user_id=driver.user_id, purpose='withdrawal'), 0, 0, 10)
        assert entries[0]['label'] == row['statement_line'] and entries[0]['provider_fee_minor'] == 472

    def test_a_scheduled_payout_has_no_fee_line(self, india):  # noqa: F811
        driver = funded(india, 'L8')
        WithdrawalRequest.objects.create(driver=driver, amount='900', origin='scheduled', status='completed')
        row = client_for(driver.user_id).get('/api/v1/driver/withdrawals/history/').json()['data']['results'][0]
        assert row['statement_line'] == ''


class TestFeeReconciliation:
    def _paid_instant(self, india, tag, reported):  # noqa: F811
        from django.utils import timezone

        driver = funded(india, tag)
        assert instant(client_for(driver.user_id), '200').status_code == 201
        w = WithdrawalRequest.objects.get(driver=driver)
        WithdrawalRequest.objects.filter(pk=w.pk).update(status='completed', payout_reference_id=f'withdrawal_{w.id}')
        entity = {'id': f'pout_{tag}', 'reference_id': f'withdrawal_{w.id}', 'status': 'processed'}
        if reported is not None:
            entity['fees'] = reported
        event = WebhookEvent.objects.create(
            gateway='razorpayx', dedupe_key=f'rzpx-{tag}', event_type='payout.processed',
            raw_payload={'event': 'payout.processed', 'payload': {'payout': {'entity': entity}}})
        # Received a minute ago: the run's window ends at "now" (the test clock is frozen).
        WebhookEvent.objects.filter(pk=event.pk).update(received_at=timezone.now() - timedelta(minutes=1))
        return w

    def _run(self):
        from django.utils import timezone

        from servers.payments.reconciliation import country_timezone, run_one
        return run_one('IN', 'razorpay', timezone.now().astimezone(country_timezone('IN')).date(), force=True)

    def test_a_fee_the_provider_reports_differently_is_flagged(self, india):  # noqa: F811
        w = self._paid_instant(india, 'R1', 590)
        self._run()
        item = ReconciliationItem.objects.get(kind=ReconciliationItem.PAYOUT_FEE_MISMATCH)
        assert item.withdrawal_id == w.id and item.expected == 'fee 472' and item.seen == 'provider reports 590'

    def test_a_matching_fee_raises_nothing_and_a_missing_one_is_said(self, india):  # noqa: F811
        self._paid_instant(india, 'R2', 472)
        self._paid_instant(india, 'R3', None)
        run = self._run()
        assert not ReconciliationItem.objects.filter(kind=ReconciliationItem.PAYOUT_FEE_MISMATCH).exists()
        assert run.checks['payout_fee_mismatch'] == 'ran; 1 payout(s) with no fee reported'
