"""Leftovers finished on 9 Nov: "Free to cancel" only while it is free, driver uploads of the MVAG
records, US Withdraw now off until Stripe's fee is entered, and a Withdraw now on the fake payout
provider going Processing -> Paid with its ledger rows.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from finance_world import client_for, make_driver, make_rider, make_trip
from servers.driver import compliance
from servers.driver.models import DriverUPIContact, WithdrawalRequest
from servers.payments.models import PayoutCountryConfig
from servers.platform_ops.models import FeatureFlag, FeatureFlagOverride
from servers.ride import cancellation_penalty as cp
from servers.ride.models import TripStatus
from servers.rider.models import Wallet, WalletTransaction
from test_pricing_module import geo, make_rule, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]


@pytest.fixture
def hyd(world):
    world.hyd.state_code = 'TG'
    world.hyd.save(update_fields=['state_code'])
    FeatureFlagOverride.objects.get_or_create(flag=FeatureFlag.objects.get(key=cp.FLAG),
                                              node=world.geo['IN'], defaults={'enabled': True})
    make_rule('IN', '15.00', node=world.geo['IN'])
    world.driver = make_driver(world.geo, 'IN-TG-HYD', 'l', currency='INR', calling='91')
    world.rider = make_rider('l', '91')
    return world


class TestFreeToCancel:
    def _trip(self, w, accepted_ago=None):
        t = make_trip(w.hyd, w.rider, w.driver, fare='437', completed=False)
        if accepted_ago is None:
            t.driver_id = None
        else:
            t.accepted_at = timezone.now() - accepted_ago
            t.status_id, _ = TripStatus.objects.get_or_create(status_code='accepted')
        t.save()
        return t

    def test_free_while_finding_a_driver(self, hyd):
        p = cp.preview(self._trip(hyd))
        assert p['free'] is True and p['fee'] is None

    def test_free_inside_the_window_with_the_time_left(self, hyd):
        p = cp.preview(self._trip(hyd, timedelta(minutes=2)))
        assert p['free'] is True and p['free_until']

    def test_the_fee_after_the_window(self, hyd):
        trip = self._trip(hyd, timedelta(minutes=6))
        r = client_for(hyd.rider).get(f'/api/v1/ride/trip/{trip.id}/cancel-preview/')
        body = r.json()['data']
        assert r.status_code == 200 and body['free'] is False and body['fee'] == '43.70'

    def test_app_config_says_free_before_accept(self, hyd):
        from servers.platform_ops.app_config import _cancel_policy
        assert _cancel_policy('IN')['free_before_accept'] is True


class TestDriverUploads:
    def test_records_are_optional_and_listed_for_review(self, hyd):
        checks = compliance.evaluate_driver(hyd.driver)
        assert {'health_insurance', 'term_insurance', 'induction_training'} <= set(checks)
        assert not any(checks[k]['required'] for k in compliance.OPTIONAL_DRIVER_DOCUMENTS)
        assert not set(compliance.OPTIONAL_DRIVER_DOCUMENTS) & set(
            compliance.missing_documents(hyd.driver.active_vehicle, hyd.driver))

    def test_the_driver_saves_the_details(self, hyd):
        r = client_for(hyd.driver.user_id).patch('/api/v1/driver/driver/update/', {
            'health_insurance_policy_number': 'H-1', 'health_insurance_insurer': 'Insurer Ltd',
            'health_insurance_sum_insured': 500000, 'health_insurance_expiry': '2027-06-30',
            'induction_training_hours': '40', 'induction_training_date': '2026-10-01'}, format='json')
        assert r.status_code == 200, r.content[:300]
        hyd.driver.refresh_from_db()
        assert hyd.driver.health_insurance_sum_insured == 500000
        assert hyd.driver.induction_training_hours == Decimal('40.0')


def _india_instant():
    PayoutCountryConfig.objects.filter(country='IN').delete()
    return PayoutCountryConfig.objects.create(
        country='IN', currency='INR', fee_percent=Decimal('0'), minimum_withdrawal_minor=10000,
        instant_cashout_enabled=True, instant_fee_provider='RazorpayX',
        instant_fee_tiers=[[99999, 472], [None, 1062]], is_active=True, version=1)


class TestUsInstantOff:
    def test_no_fee_rule_no_withdraw_now(self, hyd):
        from servers.payments.payout_scope import instant_ready
        us = PayoutCountryConfig(country='US', instant_cashout_enabled=True, instant_fee_provider='Stripe')
        assert instant_ready(us) is False
        assert instant_ready(_india_instant()) is True


class TestFakeWithdrawNow:
    def test_processing_then_paid_with_ledger_rows(self, hyd, settings):
        from integrations.payouts import fake

        settings.PAYOUT_FAKE_ALLOWED = True
        _india_instant()
        d = hyd.driver
        d.upi_id = fake.SUCCESS_VPA
        d.save(update_fields=['upi_id'])
        DriverUPIContact.objects.create(driver=d, upi_id=fake.SUCCESS_VPA, is_active=True)
        Wallet.objects.filter(user_id=d.user_id, scope='driver').update(balance_minor=50000)
        scheduled = []
        from unittest import mock
        with mock.patch('servers.payments.tasks.fake_payout_settle.apply_async',
                        side_effect=lambda args, countdown: scheduled.append(args)):
            r = client_for(d.user_id).post('/api/v1/driver/withdrawals/request/',
                                           {'amount': '200', 'instant': True}, format='json')
        assert r.status_code == 201, r.content[:300]
        w = WithdrawalRequest.objects.get(origin='instant')
        assert w.status == 'processed' and w.fee_minor == 472 and scheduled
        fake.settle(*scheduled[0])
        w.refresh_from_db()
        assert w.status == 'completed'
        rows = WalletTransaction.objects.filter(user_id=d.user_id, reference_id=f'withdrawal_{w.id}')
        # The ledger row reaches its final state with the payout (AUDIT §35, item 18 reversed).
        assert rows.filter(purpose='withdrawal', txn_type='debit', amount_minor=20000).get().status == 'completed'

    def test_never_without_the_qa_switch(self, hyd, settings):
        from integrations.payouts import fake
        settings.PAYOUT_FAKE_ALLOWED = False
        assert fake.allowed_for(fake.SUCCESS_VPA) is False
