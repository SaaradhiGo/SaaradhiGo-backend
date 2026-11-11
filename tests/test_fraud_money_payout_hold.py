"""Ruling (d), AUDIT 27.56: NO automatic payout hold on a confirmed flag. A full payout hold is
allowed only together with a suspension for serious fraud, needs a second admin, and has a maximum
duration (14 days, per country) after which it lapses unless renewed (another checker request).
"""

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from django.utils import timezone

from fraud_money_support import (  # noqa: F401
    ADM, APPROVALS, approve, block_the_network, body_of, campaign, client_for, credit, decide,
    error_code, flag_url, geo, get, hyd_driver, make_flag, make_rider, operator, post, reward,
    seed_money, us_driver, world,
)
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.command_centre.models import FRAUD_PAYOUT_HOLD_EXPIRED, OperatorAlert
from servers.driver.admin_drivers import operator_suspend
from servers.driver.models import WithdrawalRequest
from servers.fraud import money
from servers.fraud.models import PayoutHold
from servers.payments.models import PayoutCountryConfig, PayoutRun
from servers.payments.weekly_payout import run_country
from servers.rider.models import Wallet, get_wallet

pytestmark = pytest.mark.django_db

HOLD = 'payout-hold/'


@pytest.fixture
def money_world(world):
    seed_money()
    return world


def fake_trips_flag(w, drv, *, confirm='gps_spoofing', node='IN-TG-HYD', client=None):
    flag = make_flag(drv.user_id, w.geo[node], kind='gps_anomaly', evidence={
        'signals': ['teleport'], 'refs': {'trip_ids': []}, 'measures': {}, 'hit_count': 1})
    if confirm:
        r = decide(client or w.ops[0], flag, 'confirm', confirm)
        assert r.status_code == 200, r.content
    flag.refresh_from_db()
    return flag


def suspend(drv, actor):
    outcome, _ = operator_suspend(drv.id, reason_code='fraud', note='Fake trips', actor=actor)
    assert outcome == 'suspended'


def ask(w, flag, *, key='h1', client=None, expect=202, **extra):
    body = {'note': 'Fake trips confirmed, driver suspended', 'idempotency_key': key, **extra}
    return post(client or w.ops[0], flag_url(flag, HOLD), body, expect=expect)


def place(w, flag, **extra):
    request_id = body_of(ask(w, flag, **extra))['approval_request']['id']
    assert approve(request_id).status_code == 200
    return PayoutHold.objects.get(approval_request_id=request_id)


class TestNoAutomaticHold:
    def test_confirming_a_serious_flag_holds_no_payout_anywhere(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'N1')
        suspend(drv, w.ops[1])
        credit(drv.user_id, 70_000)
        flag = fake_trips_flag(w, drv)
        assert flag.status == 'confirmed'
        assert not PayoutHold.objects.exists()
        assert money.active_payout_hold(drv) is None
        # The driver can still withdraw (a suspension is the module 3 decision, not a hold).
        r = client_for(drv.user_id).post('/api/v1/driver/withdrawals/request/',
                                         {'amount': '600.00'}, format='json')
        assert r.status_code == 201, r.content

    def test_the_weekly_run_still_pays_a_driver_with_a_confirmed_flag(self, money_world):
        w = money_world
        drv = us_driver(w, 'N2')
        credit(drv.user_id, 5_000, currency='USD')
        fake_trips_flag(w, drv, node='US-TX-AUS', client=w.super[0])
        config = PayoutCountryConfig.objects.get(country='US', is_active=True)
        result = run_country(config, datetime(2026, 11, 2, 18, 0, tzinfo=dt_tz.utc))
        assert result['withdrawals'] == 1 and result['skipped_held'] == []


class TestConditions:
    def test_it_needs_a_suspended_driver(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'C1')
        flag = fake_trips_flag(w, drv)
        r = ask(w, flag, expect=409)
        assert error_code(r) == 'PAYOUT_HOLD_NOT_ALLOWED' and 'driver_not_suspended' in r.json()[
            'error']['message']
        suspend(drv, w.ops[1])
        ask(w, flag)

    def test_it_needs_a_serious_fraud_confirmation(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'C2')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv, confirm='promo_abuse')
        assert 'reason_not_serious' in ask(w, flag, expect=409).json()['error']['message']
        other = hyd_driver(w, 'C2b')
        suspend(other, w.ops[1])
        flag2 = fake_trips_flag(w, other, confirm='collusion')       # collusion counts as serious
        ask(w, flag2, key='c2b')

    def test_it_needs_a_confirmed_flag(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'C3')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv, confirm=None)
        assert 'flag_not_confirmed' in ask(w, flag, expect=409).json()['error']['message']
        decide(w.ops[0], flag, 'dismiss', 'false_positive')
        assert error_code(ask(w, flag, expect=409)) == 'PAYOUT_HOLD_NOT_ALLOWED'

    def test_a_rider_subject_is_refused(self, money_world):
        w = money_world
        rider = make_rider('C4', '91')
        flag = make_flag(rider, w.geo['IN-TG-HYD'], kind='gps_anomaly')
        decide(w.ops[0], flag, 'confirm', 'gps_spoofing')
        assert 'subject_not_driver' in ask(w, flag, expect=409).json()['error']['message']

    def test_the_capability_and_the_scope(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'C5')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        for who in (w.mkt, w.finance, w.analyst, w.support):
            assert post(who[0], flag_url(flag, HOLD),
                        {'note': 'long enough', 'idempotency_key': 'x'}).status_code == 403
        assert post(w.ops_us[0], flag_url(flag, HOLD),
                    {'note': 'long enough', 'idempotency_key': 'x'}).status_code == 404

    def test_days_are_bounded_by_the_countrys_maximum(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'C6')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        for bad in (0, 15, -1, '7', True):
            assert error_code(ask(w, flag, days=bad, expect=400)) == 'INVALID_DAYS', bad
        ask(w, flag, days=14)


class TestTheHold:
    def test_it_needs_a_second_admin_and_lasts_the_maximum_by_default(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'H1')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        data = body_of(ask(w, flag))
        request = data['approval_request']
        assert request['action_key'] == 'finance.payout_hold' and data['state'] == 'pending_approval'
        assert not PayoutHold.objects.exists()                         # nothing until approved
        # The maker cannot approve their own request.
        assert post(w.ops[0], f'{APPROVALS}/{request["id"]}/approve/', {'reason': 'ok'}
                    ).status_code == 403
        assert approve(request['id']).status_code == 200
        hold = PayoutHold.objects.get()
        assert hold.status == 'active' and hold.max_days == 14
        assert hold.expires_at - hold.starts_at == timedelta(days=14)
        assert AdminAuditLog.objects.filter(action='payout_hold_placed',
                                            target_id=str(drv.pk)).count() == 1

    def test_it_is_a_checker_in_the_us_as_well(self, money_world):
        w = money_world
        drv = us_driver(w, 'H2')
        suspend(drv, w.super[1])
        flag = fake_trips_flag(w, drv, node='US-TX-AUS', client=w.super[0])
        data = body_of(ask(w, flag, client=w.super[0]))
        assert data['state'] == 'pending_approval'

    def test_the_conditions_are_checked_again_when_the_checker_approves(self, money_world):
        from servers.driver.admin_drivers import operator_reinstate

        w = money_world
        drv = hyd_driver(w, 'H3')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        request_id = body_of(ask(w, flag))['approval_request']['id']
        operator_reinstate(drv.id, note='Cleared on appeal', actor=w.ops[1])
        r = approve(request_id)
        assert r.status_code in (200, 409)
        assert not PayoutHold.objects.exists()
        assert ApprovalRequest.objects.get(pk=request_id).status == 'failed'

    def test_a_held_driver_cannot_withdraw_and_the_wording_is_neutral(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'H4')
        credit(drv.user_id, 70_000)
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        place(w, flag)
        r = client_for(drv.user_id).post('/api/v1/driver/withdrawals/request/',
                                         {'amount': '600.00'}, format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'PAYOUT_ON_HOLD'
        assert 'fraud' not in r.content.decode().lower()
        assert get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor == 70_000

    def test_the_weekly_run_skips_the_driver_and_lists_it_as_held(self, money_world):
        w = money_world
        held = us_driver(w, 'H5')
        paid = us_driver(w, 'H5b')
        for d in (held, paid):
            credit(d.user_id, 5_000, currency='USD')
        suspend(held, w.super[1])
        flag = fake_trips_flag(w, held, node='US-TX-AUS', client=w.super[0])
        place(w, flag, client=w.super[0])
        config = PayoutCountryConfig.objects.get(country='US', is_active=True)
        result = run_country(config, datetime(2026, 11, 2, 18, 0, tzinfo=dt_tz.utc))
        assert result['withdrawals'] == 1
        assert result['skipped_held'] == [held.id] and result['skipped_no_handle'] == []
        assert not WithdrawalRequest.objects.filter(driver=held).exists()
        assert get_wallet(held.user_id, Wallet.SCOPE_DRIVER).balance_minor == 5_000
        run = PayoutRun.objects.get(country='US')
        assert {'driver_id': held.id, 'reason': 'held'} in run.skipped
        # Finance sees the held count in the overview, in scope.
        today = '2026-11-02'
        fin = w.super[0].get(f'/api/v1/payments/admin/overview/?from={today}&to={today}')
        skipped = fin.json()['data']['skipped_in_latest_run']
        assert skipped and skipped[0]['held_count'] == 1 and skipped[0]['no_handle_count'] == 0

    def test_finance_sees_the_hold_and_held_rewards_on_the_ledger_page(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'H6')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        hold = place(w, flag)
        data = w.finance[0].get(f'/api/v1/payments/admin/drivers/{drv.id}/ledger/').json()['data']
        assert data['payout_hold']['id'] == hold.pk and data['payout_hold']['status'] == 'active'
        assert data['recovery']['debts'] == [] and data['held_rewards']['minor'] == 0


class TestWhenItEnds:
    def test_it_lapses_at_fourteen_days_and_finance_and_the_reviewers_are_alerted(
            self, money_world):
        w = money_world
        drv = hyd_driver(w, 'E1')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        hold = place(w, flag)
        assert money.lapse_due_holds(hold.expires_at - timedelta(minutes=1)) == 0
        assert money.active_payout_hold(drv) is not None
        assert money.lapse_due_holds(hold.expires_at + timedelta(minutes=1)) == 1
        hold.refresh_from_db()
        assert hold.status == 'lapsed' and hold.released_at
        assert money.active_payout_hold(drv, hold.expires_at + timedelta(minutes=2)) is None
        alert = OperatorAlert.objects.get(kind=FRAUD_PAYOUT_HOLD_EXPIRED)
        assert alert.payload['flag_id'] == flag.pk and 'driver' not in alert.title.lower()
        listing = {}
        for name, who in (('finance', w.finance), ('ops', w.ops), ('mkt', w.mkt),
                          ('analyst', w.analyst)):
            r = who[0].get('/api/v1/command-centre/admin/alerts/')
            listing[name] = r.status_code, [a['kind'] for a in r.json()['data']['results']
                                            ] if r.status_code == 200 else []
        assert FRAUD_PAYOUT_HOLD_EXPIRED in listing['finance'][1]
        assert FRAUD_PAYOUT_HOLD_EXPIRED in listing['ops'][1]
        assert FRAUD_PAYOUT_HOLD_EXPIRED not in listing['analyst'][1]
        # Sweeping again changes nothing.
        assert money.lapse_due_holds(hold.expires_at + timedelta(days=1)) == 0
        assert OperatorAlert.objects.filter(kind=FRAUD_PAYOUT_HOLD_EXPIRED).count() == 1

    def test_the_driver_is_paid_again_after_it_lapses(self, money_world):
        w = money_world
        drv = us_driver(w, 'E2')
        credit(drv.user_id, 5_000, currency='USD')
        suspend(drv, w.super[1])
        flag = fake_trips_flag(w, drv, node='US-TX-AUS', client=w.super[0])
        hold = place(w, flag, client=w.super[0])
        money.lapse_due_holds(hold.expires_at + timedelta(minutes=1))
        config = PayoutCountryConfig.objects.get(country='US', is_active=True)
        result = run_country(config, datetime(2026, 11, 2, 18, 0, tzinfo=dt_tz.utc))
        assert result['withdrawals'] == 1

    def test_the_beat_task_runs_both_sweeps(self, money_world):
        from servers.fraud.tasks import release_holds

        out = release_holds()
        assert out == {'rewards_released': 0, 'payout_holds_lapsed': 0}

    def test_early_release_by_a_reviewer_needs_no_checker(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'E3')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        hold = place(w, flag)
        before = ApprovalRequest.objects.count()
        r = post(w.ops[0], f'{ADM}/payout-holds/{hold.pk}/release/', {'note': 'Cleared on appeal'})
        assert r.status_code == 200 and r.json()['data']['status'] == 'released'
        assert ApprovalRequest.objects.count() == before                # no request raised
        assert money.active_payout_hold(drv) is None
        assert AdminAuditLog.objects.filter(action='payout_hold_released',
                                            target_id=str(drv.pk)).count() == 1
        again = post(w.ops[0], f'{ADM}/payout-holds/{hold.pk}/release/', {'note': 'Cleared again'})
        assert again.status_code == 409 and error_code(again) == 'NOT_ACTIVE'

    def test_early_release_by_finance_and_its_gates(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'E4')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        hold = place(w, flag)
        url = f'/api/v1/payments/admin/payout-holds/{hold.pk}/release/'
        assert post(w.mkt[0], url, {'note': 'Cleared on appeal'}).status_code == 403
        assert post(w.ops_us[0], f'{ADM}/payout-holds/{hold.pk}/release/',
                    {'note': 'Cleared on appeal'}).status_code == 404
        assert post(w.finance[0], url, {'note': 'x'}).status_code == 400
        assert post(w.finance[0], url, {'note': 'Cleared on appeal'}).status_code == 200
        assert PayoutHold.objects.get().status == 'released'
        assert post(w.finance[0], '/api/v1/payments/admin/payout-holds/999999/release/',
                    {'note': 'Cleared on appeal'}).status_code == 404


class TestRenewal:
    def test_renewing_is_another_checker_request(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'R1')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        first = place(w, flag, days=7)
        # While active, a plain second request is refused; a renewal is the way.
        assert error_code(ask(w, flag, key='again', expect=409)) == 'ALREADY_HELD'
        data = body_of(ask(w, flag, key='renew', renew=True, days=10))
        assert data['state'] == 'pending_approval'
        first.refresh_from_db()
        assert first.status == 'active'                              # nothing changed yet
        assert approve(data['approval_request']['id']).status_code == 200
        first.refresh_from_db()
        second = PayoutHold.objects.exclude(pk=first.pk).get()
        assert first.status == 'released' and first.release_note == 'renewed'
        assert second.status == 'active' and second.renewal_of_id == first.pk
        assert second.expires_at - second.starts_at == timedelta(days=10)

    def test_a_lapsed_hold_can_be_renewed_and_not_otherwise_restarted(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'R2')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        first = place(w, flag)
        money.lapse_due_holds(first.expires_at + timedelta(minutes=1))
        assert error_code(ask(w, flag, key='plain', expect=409)) == 'USE_RENEWAL'
        request_id = body_of(ask(w, flag, key='renew', renew=True))['approval_request']['id']
        assert approve(request_id).status_code == 200
        assert PayoutHold.objects.filter(status='active').count() == 1

    def test_nothing_to_renew_without_an_earlier_hold(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'R3')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        assert error_code(ask(w, flag, renew=True, expect=409)) == 'NOTHING_TO_RENEW'

    def test_a_renewal_is_still_capped_at_the_maximum(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'R4')
        suspend(drv, w.ops[1])
        flag = fake_trips_flag(w, drv)
        place(w, flag)
        assert error_code(ask(w, flag, key='r', renew=True, days=15, expect=400)) == 'INVALID_DAYS'
