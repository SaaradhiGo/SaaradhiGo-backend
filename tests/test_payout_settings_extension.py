"""Payout settings extension (owner ruling, 2026-11-09).

Drivers are paid automatically on a cycle, free; Withdraw now is optional per market and costs
only the provider's own fee. Settings resolve city -> state -> country, field by field.
"""
from datetime import date, datetime, time, timezone as dt_timezone
from decimal import Decimal

import pytest

from finance_world import client_for, make_driver, operator
from servers.driver.models import DriverUPIContact, WithdrawalRequest
from servers.payments.models import PayoutCountryConfig, PayoutRun, PayoutScopeOverride
from servers.payments.payout_scope import (
    due_cycle, effective_config, effective_for_driver, instant_fee, next_payout_at,
)
from servers.payments.weekly_payout import run_weekly_payouts
from servers.rider.models import Wallet
from test_finance_actions import approve_request, body_of, checker, get, post
from test_pricing_module import geo, make_rule, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]
BASE = '/api/v1/payments/admin'
RAZORPAYX = [[99999, 472], [2500000, 708], [None, 1062]]


@pytest.fixture
def india(world):
    PayoutCountryConfig.objects.filter(country='IN').delete()
    world.cfg = PayoutCountryConfig.objects.create(
        country='IN', currency='INR', fee_percent=Decimal('0'), minimum_withdrawal_minor=10000,
        payout_schedule='every_n_days', payout_interval_days=5,
        payout_anchor_date=date(2026, 11, 5), payout_time_local=time(10, 0),
        instant_cashout_enabled=True, instant_fee_provider='RazorpayX',
        instant_fee_tiers=RAZORPAYX, is_active=True, version=1)
    make_rule('IN', '15.00', node=world.geo['IN'])
    return world


def funded(world, tag, minor=100_000):
    driver = make_driver(world.geo, 'IN-TG-HYD', tag, currency='INR', calling='91')
    Wallet.objects.filter(user_id=driver.user_id, scope='driver').update(balance_minor=minor)
    DriverUPIContact.objects.create(driver=driver, upi_id=f'{tag.lower()}@okaxis', is_active=True)
    return driver


def override(code, kind, **values):
    return PayoutScopeOverride.objects.create(country='IN', scope_code=code, scope_kind=kind,
                                              **values)


# 10 Nov 2026 is a cycle day (5, 10, 15 ...). 04:30 UTC = 10:00 IST.
AT_TEN = datetime(2026, 11, 10, 4, 30, tzinfo=dt_timezone.utc)
BEFORE_TEN = datetime(2026, 11, 10, 3, 0, tzinfo=dt_timezone.utc)


class TestResolution:
    def test_city_then_state_then_country_field_by_field(self, india):
        override('IN-TG', 'state', minimum_withdrawal_minor=20000, payout_time_local=time(9, 0))
        override('IN-TG-HYD', 'city', minimum_withdrawal_minor=15000)
        eff = effective_config('IN', ['IN-TG-HYD', 'IN-TG'])
        assert eff.minimum_withdrawal_minor == 15000 and eff.sources['minimum_withdrawal_minor'] == 'IN-TG-HYD'
        assert eff.payout_time_local == time(9, 0) and eff.sources['payout_time_local'] == 'IN-TG'
        assert eff.payout_interval_days == 5 and eff.sources['payout_interval_days'] == 'country'
        assert eff.schedule_scope == 'IN-TG'          # the state sets a schedule field
        assert effective_config('IN', ['IN-AP']).minimum_withdrawal_minor == 10000

    def test_a_driver_resolves_through_their_home_city(self, india):
        override('IN-TG-HYD', 'city', instant_cashout_enabled=False)
        driver = funded(india, 'R1')
        country, eff = effective_for_driver(driver)
        assert country == 'IN' and eff.instant_cashout_enabled is False
        assert eff.fee_percent == 0          # SaaradhiGo's own fee, never overridden


class TestTheCycle:
    def test_every_five_days_from_the_anchor_at_ten(self, india):
        cfg = india.cfg
        assert due_cycle(cfg, AT_TEN) == date(2026, 11, 10)
        assert due_cycle(cfg, BEFORE_TEN) is None
        assert due_cycle(cfg, datetime(2026, 11, 11, 5, 0, tzinfo=dt_timezone.utc)) is None
        nxt = next_payout_at(cfg, datetime(2026, 11, 11, 5, 0, tzinfo=dt_timezone.utc))
        assert (nxt.date(), nxt.hour) == (date(2026, 11, 15), 10)

    def test_each_cycle_runs_once_even_if_the_job_runs_twice(self, india):
        driver = funded(india, 'C1')
        first = run_weekly_payouts(AT_TEN)
        second = run_weekly_payouts(AT_TEN)
        assert first[0]['ran'] is True and first[0]['withdrawals'] == 1
        assert second[0]['ran'] is False and second[0]['why'] == 'already_ran'
        assert PayoutRun.objects.filter(week_start=date(2026, 11, 10)).count() == 1
        assert WithdrawalRequest.objects.get(origin='scheduled').driver_id == driver.id
        assert WithdrawalRequest.objects.get(origin='scheduled').fee_minor == 0   # free

    def test_a_city_with_its_own_schedule_runs_on_its_own(self, india):
        override('IN-TG-HYD', 'city', payout_schedule='weekly', payout_weekday=0)
        funded(india, 'C2')
        result = run_weekly_payouts(AT_TEN)[0]           # Tuesday: the city's day is Monday
        assert result['ran'] is True and result['withdrawals'] == 0
        assert result['scopes']['IN-TG-HYD']['why'] == 'not_the_day'
        monday = datetime(2026, 11, 16, 5, 0, tzinfo=dt_timezone.utc)
        assert run_weekly_payouts(monday)[0]['scopes']['IN-TG-HYD']['ran'] is True
        assert PayoutRun.objects.filter(scope_key='IN-TG-HYD').count() == 1


class TestInstant:
    @pytest.mark.parametrize('amount,fee', [(50_000, 472), (99_999, 472), (100_000, 708),
                                            (2_500_000, 708), (2_500_001, 1062)])
    def test_the_razorpayx_bands(self, india, amount, fee):
        assert instant_fee(india.cfg, amount) == fee

    def test_percent_fixed_min_and_max(self, india):
        cfg = india.cfg
        cfg.instant_fee_tiers = None
        cfg.instant_fee_percent = Decimal('1.00')
        cfg.instant_cashout_fee_flat_minor = 300
        cfg.instant_fee_min_minor, cfg.instant_fee_max_minor = 1000, 5000
        assert instant_fee(cfg, 20_000) == 1000           # 200 + 300, raised to the minimum
        assert instant_fee(cfg, 300_000) == 3300
        assert instant_fee(cfg, 900_000) == 5000          # held to the maximum

    def test_the_fee_shown_is_the_fee_charged_and_the_platform_takes_nothing(self, india):
        driver = funded(india, 'I1')
        client = client_for(driver.user_id)
        balance = client.get('/api/v1/driver/withdrawals/balance/').json()['data']
        assert balance['instant']['available'] is True and balance['instant']['saaradhigo_fee'] == '0'
        assert balance['next_payout']['currency'] == 'INR'
        quote = client.get('/api/v1/driver/withdrawals/instant/quote/?amount=500').json()['data']
        assert quote['provider_fee_minor'] == 472 and quote['saaradhigo_fee_minor'] == 0
        assert quote['you_receive_minor'] == 50_000 - 472
        assert quote['provider_fee_label'] == 'Provider fee (charged by RazorpayX)'
        assert quote['destination'] == 'UPI ••kaxis'
        r = client.post('/api/v1/driver/withdrawals/request/', {'amount': '500', 'instant': True},
                        format='json')
        assert r.status_code == 201, r.content[:300]
        w = WithdrawalRequest.objects.get(origin='instant')
        assert w.fee_minor == quote['provider_fee_minor'] and w.amount_minor == 50_000
        assert india.cfg.fee_percent == 0

    def test_withdraw_now_is_refused_where_instant_is_off(self, india):
        override('IN-TG-HYD', 'city', instant_cashout_enabled=False)
        client = client_for(funded(india, 'I2').user_id)
        assert client.get('/api/v1/driver/withdrawals/balance/').json()['data'][
            'instant']['available'] is False
        r = client.post('/api/v1/driver/withdrawals/request/', {'amount': '500', 'instant': True},
                        format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'INSTANT_NOT_AVAILABLE'


class TestConsole:
    def test_a_city_override_is_a_checked_request_and_the_preview_shows_it(self, india):
        maker, _ = operator('finance', india.geo, 'IN')
        r = post(maker, f'{BASE}/payout-config/IN/', {
            'scope': 'IN-TG-HYD', 'payout_time_local': '08:30', 'reason': 'Hyderabad ops',
            'idempotency_key': 'hyd-1'}, expect=202)
        assert not PayoutScopeOverride.objects.exists()
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        row = PayoutScopeOverride.objects.get(scope_code='IN-TG-HYD', is_active=True)
        assert row.payout_time_local == time(8, 30) and row.version == 1
        config = get(maker, f'{BASE}/payout-config/')
        india_row = next(c for c in config['countries'] if c['country'] == 'IN')
        hyd = next(s for s in india_row['scopes'] if s['scope'] == 'IN-TG-HYD')
        assert hyd['effective']['sources']['payout_time_local'] == 'IN-TG-HYD'
        assert hyd['preview'].startswith('Drivers in ') and 'at 08:30' in hyd['preview']

    def test_every_n_days_needs_an_interval_and_an_anchor(self, india):
        maker, _ = operator('finance', india.geo, 'IN')
        r = post(maker, f'{BASE}/payout-config/IN/', {
            'scope': 'IN-TG', 'payout_schedule': 'every_n_days', 'payout_interval_days': None,
            'payout_anchor_date': None, 'reason': 'x', 'idempotency_key': 'tg-1'}, expect=400)
        assert r.json()['error']['code'] in ('CYCLE_REQUIRED',)
