"""The three per-country settings of the fraud money actions (AUDIT 27.56): `reward_hold_days` (7),
`recovery_max_percent` (50) and `payout_hold_max_days` (14). Seeded for India and the US,
versioned, and changed only through the payout-config checker (`finance.payout_config_change`,
always a checker). Plus the raw-PII sweep over the screens that now carry money fields.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from conftest import seed_actor
from fraud_money_support import (  # noqa: F401
    ADM, approve, block_the_network, body_of, campaign, client_for, credit, decide, error_code,
    farming_flag, flag_url, geo, get, hyd_driver, make_flag, make_rider, operator, paid_out, post,
    reward, seed_money, us_driver, world,
)
from servers.approvals.models import ApprovalRequest
from servers.payments.models import PayoutCountryConfig

pytestmark = pytest.mark.django_db

BASE = '/api/v1/payments/admin'


class TestSeed:
    def test_the_seed_writes_the_defaults_for_india_and_the_us_and_is_idempotent(self, world):
        PayoutCountryConfig.objects.all().delete()
        call_command('seed_payout_config', actor_email=seed_actor().email, stdout=StringIO())
        rows = {c.country: c for c in PayoutCountryConfig.objects.filter(is_active=True)}
        assert set(rows) == {'IN', 'US'}
        for row in rows.values():
            assert (row.reward_hold_days, row.recovery_max_percent,
                    row.payout_hold_max_days) == (7, 50, 14)
        before = list(PayoutCountryConfig.objects.values_list('id', 'version'))
        call_command('seed_payout_config', actor_email=seed_actor().email, stdout=StringIO())
        call_command('apply_payout_ruling', actor_email=seed_actor().email, stdout=StringIO())
        assert list(PayoutCountryConfig.objects.values_list('id', 'version')) == before

    def test_the_settings_are_in_the_config_page(self, world):
        seed_money()
        data = get(world.finance[0], f'{BASE}/payout-config/')
        india = [r for r in data['results'] if r['country'] == 'IN'][0] if 'results' in data \
            else [r for r in data['countries'] if r['country'] == 'IN'][0]
        assert india['config']['reward_hold_days'] == 7
        assert india['config']['recovery_max_percent'] == 50
        assert india['config']['payout_hold_max_days'] == 14


class TestChanging:
    url = f'{BASE}/payout-config/US/'

    def test_a_change_is_a_checker_request_and_writes_the_next_version(self, world):
        seed_money()
        maker, _ = world.super
        r = post(maker, self.url, {'recovery_max_percent': 40, 'reward_hold_days': 5,
                                   'payout_hold_max_days': 10, 'reason': 'Finance review',
                                   'idempotency_key': 'fm-1'}, expect=202)
        request = body_of(r)['approval_request']
        assert request['action_key'] == 'finance.payout_config_change'
        active = PayoutCountryConfig.objects.get(country='US', is_active=True)
        assert (active.recovery_max_percent, active.version) == (50, 1)      # nothing yet
        assert approve(request['id']).status_code == 200
        active = PayoutCountryConfig.objects.get(country='US', is_active=True)
        assert (active.reward_hold_days, active.recovery_max_percent,
                active.payout_hold_max_days, active.version) == (5, 40, 10, 2)
        old = PayoutCountryConfig.objects.get(country='US', version=1)
        assert old.is_active is False and old.recovery_max_percent == 50     # history kept

    def test_an_unrelated_change_carries_the_three_values_forward(self, world):
        seed_money()
        post_ = post(world.super[0], self.url, {'fee_percent': '1.00', 'reason': 'Fee review',
                                                'idempotency_key': 'fm-2'}, expect=202)
        approve(body_of(post_)['approval_request']['id'])
        active = PayoutCountryConfig.objects.get(country='US', is_active=True)
        assert (active.reward_hold_days, active.recovery_max_percent,
                active.payout_hold_max_days) == (7, 50, 14)

    @pytest.mark.parametrize('body,code', [
        ({'reward_hold_days': 0}, 'INVALID_REWARD_HOLD_DAYS'),
        ({'reward_hold_days': 91}, 'INVALID_REWARD_HOLD_DAYS'),
        ({'recovery_max_percent': 0}, 'INVALID_RECOVERY_MAX_PERCENT'),
        ({'recovery_max_percent': 101}, 'INVALID_RECOVERY_MAX_PERCENT'),
        ({'payout_hold_max_days': 0}, 'INVALID_PAYOUT_HOLD_MAX_DAYS'),
        ({'payout_hold_max_days': 91}, 'INVALID_PAYOUT_HOLD_MAX_DAYS'),
    ])
    def test_values_are_bounded(self, world, body, code):
        seed_money()
        r = post(world.super[0], self.url, {**body, 'reason': 'x', 'idempotency_key': 'fm-3'})
        assert r.status_code == 400 and error_code(r) == code


class TestTheScreensStayFreeOfRawPersonalData:
    """`get` and `post` sweep every body for raw contact data (see `swept`)."""

    def test_flag_detail_with_rewards_debts_holds_and_the_cash_offer(self, world):
        from fraud_money_support import campaign as camp_, paid_out as paid_
        from servers.fraud import money

        w = world
        seed_money()
        drv = hyd_driver(w, 'Z1')
        camp = camp_(w)
        reward(drv.user_id, camp, 30_000)
        flag = farming_flag(w, drv.user_id, camp)
        money.hold_for_flag(flag)
        decide(w.ops[0], flag, 'confirm', 'promo_abuse')
        detail = get(w.ops[0], flag_url(flag))
        assert detail['money']['held_minor'] == 30_000
        assert detail['money']['rewards'][0]['state'] == 'held'
        assert detail['money']['recover']['applies'] is True
        assert detail['money']['payout_hold']['blockers']
        rider = make_rider('Z2', '91')
        rflag = make_flag(rider, w.geo['IN-TG-HYD'], kind='payment_abuse')
        decide(w.ops[0], rflag, 'confirm', 'payment_abuse')
        assert get(w.ops[0], flag_url(rflag))['money']['cash']['offered'] is True

    def test_the_ledger_page_with_a_recovery_debt_and_a_payout_hold(self, world):
        from datetime import timedelta

        from django.utils import timezone

        from test_fraud_money_payout_hold import fake_trips_flag, place, suspend

        w = world
        seed_money()
        drv = hyd_driver(w, 'Z3')
        camp = campaign(w)
        at = timezone.now() - timedelta(days=5)
        reward(drv.user_id, camp, 30_000, at=at)
        paid_out(drv, 30_000, at=at + timedelta(days=1))
        flag = farming_flag(w, drv.user_id, camp)
        decide(w.ops[0], flag, 'confirm', 'promo_abuse')
        post(w.super[0], flag_url(flag, 'recover/'),
             {'amount_minor': 30_000, 'note': 'Recovering the reward', 'idempotency_key': 'z3'},
             expect=202)
        approve(ApprovalRequest.objects.latest('id').pk)
        suspend(drv, w.ops[1])
        hold_flag = fake_trips_flag(w, drv)
        place(w, hold_flag)
        ledger = get(w.finance[0], f'{BASE}/drivers/{drv.id}/ledger/')
        assert ledger['recovery']['debts'][0]['remaining_minor'] == 30_000
        assert ledger['payout_hold']['status'] == 'active'
