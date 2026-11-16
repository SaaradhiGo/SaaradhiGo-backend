"""Launch round 2 (16 Nov): drive before payout setup (Srini's ruling); payouts only to a confirmed
account; the reverse geocode that put a Gachibowli pickup in Moinabad (trip 220 on QA)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from finance_world import client_for
from servers.driver import requirements as req
from servers.driver.models import DriverBankAccount
from servers.payments.weekly_payout import has_payout_handle
from servers.pricing.regulation_models import MarketRegulation
from servers.ride.maps_utils import consensus_first
from test_driver_requirements import get, new_driver
from test_pricing_module import geo, world  # noqa: F401

pytestmark = pytest.mark.django_db

GACHIBOWLI = json.loads((Path(__file__).parent / 'fixtures' / 'revgeo_gachibowli_1116.json').read_text(encoding='utf-8'))


class TestReverseGeocode:
    def test_the_locality_most_results_share_comes_first(self):
        assert 'Moinabad' in GACHIBOWLI['results'][0]['formatted_address']      # Google's answer, as received
        out = consensus_first(GACHIBOWLI)
        assert 'Gachibowli' in out['results'][0]['formatted_address']
        assert 'Moinabad' not in out['results'][0]['formatted_address']
        assert len(out['results']) == len(GACHIBOWLI['results'])               # nothing dropped

    def test_one_locality_or_no_majority_keeps_googles_order(self):
        one = {'results': GACHIBOWLI['results'][1:3]}
        assert consensus_first(one) == one
        assert consensus_first({'results': []}) == {'results': []}

    def test_the_pickup_street_address_uses_it(self, monkeypatch):
        from servers.ride import rider_booking

        monkeypatch.setattr('servers.redis_client.get_cached_map_data', lambda k: GACHIBOWLI)
        assert 'Gachibowli' in rider_booking.street_address(17.4401, 78.3489)


class TestDriveBeforePayoutSetup:
    def test_the_payout_method_never_blocks_even_if_a_market_row_says_so(self, world):  # noqa: F811
        MarketRegulation.objects.create(country='US', state_code='TX', driver_requirements={
            'payout_method': {'need': 'required', 'blocks_online': True}})
        assert req.resolve('US', 'TX')['items']['payout_method'] == {
            'need': 'required', 'blocks_online': False, 'source': 'TX'}
        driver = new_driver(world, calling='1', node='US-TX-AUS')
        assert 'payout_method' not in req.checklist(driver)['blocking']

    def test_the_driver_is_told_payouts_wait_for_setup(self, world):  # noqa: F811
        us = get(client_for(new_driver(world, calling='1', node='US-TX-AUS').user_id), 'requirements/')
        assert us['payout_method']['ready'] is False
        india = get(client_for(new_driver(world).user_id), 'requirements/')
        assert india['payout_method']['ready'] is False


class TestPayoutsOnlyToAConfirmedAccount:
    def test_india_needs_a_razorpayx_validated_bank_or_fund_account(self, world):  # noqa: F811
        driver = new_driver(world)
        bank = DriverBankAccount.objects.create(driver=driver, holder_name='A', account_number='123456789',
                                                ifsc_code='HDFC0000001', verification_status='pending')
        assert has_payout_handle(driver, 'INR') is False
        bank.verification_status = 'verified'
        bank.save(update_fields=['verification_status'])
        assert has_payout_handle(driver, 'INR') is True

    def test_usa_needs_stripe_to_confirm(self, monkeypatch, settings):
        from servers.driver import stripe_connect
        settings.STRIPE_SECRET_KEY = 'sk_test_x'
        monkeypatch.setattr(stripe_connect.vendor, 'stripe_account', lambda _id: {
            'payouts_enabled': False, 'details_submitted': True, 'capabilities': {'transfers': 'inactive'}})
        d = SimpleNamespace(pk=901, stripe_account_id='acct_x', stripe_onboarding_status='in_progress',
                            save=lambda update_fields: None)
        assert has_payout_handle(d, 'USD') is False
