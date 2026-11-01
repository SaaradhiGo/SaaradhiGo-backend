"""Razorpay / RazorpayX wiring: defaults, selection, readiness, the key guard, the check command.

Owner decision 2026-11-01: India uses Razorpay (payments) and RazorpayX (payouts); the US has no
provider; Cashfree stays in the code but is configured nowhere. The suite pins India to Cashfree in
`conftest.legacy_cashfree_providers` for the older Cashfree-flow tests, so the SHIPPED defaults are
asserted here against `base.settings` itself.
"""

import io
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

import razorpay_support as rz
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from platform_support import BASE, data

pytestmark = pytest.mark.django_db
URL = f'{BASE}/integrations/'


class TestDefaults:
    def test_shipped_defaults_are_razorpay_for_india_and_none_for_the_us(self):
        from base import settings as shipped
        assert shipped.PAYMENT_PROVIDERS == {'IN': 'razorpay', 'US': None}
        assert shipped.PAYOUT_PROVIDERS == {'IN': 'razorpay', 'US': None}

    def test_the_legacy_global_names_have_no_default(self):
        from base import settings as shipped
        assert shipped.PAYMENT_GATEWAY == '' and shipped.PAYOUT_GATEWAY == ''

    def test_cashfree_is_configured_nowhere_by_default(self, settings):
        from servers.payments.payment_gateways.factory import configured_providers
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.PAYOUT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        for kind in ('payments', 'payouts'):
            assert 'cashfree' not in configured_providers(kind).values()

    def test_cashfree_is_still_selectable_by_explicit_config(self, settings):
        from servers.payments.payment_gateways.factory import get_payment_gateway_for_payments
        settings.PAYMENT_PROVIDERS = {'IN': 'cashfree', 'US': None}
        settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY = 'app-test', 'secret-test'
        assert get_payment_gateway_for_payments('IN').get_name() == 'cashfree'

    def test_the_cashfree_adapter_is_still_registered(self):
        from integrations.payments import registry
        assert registry.status('payments', 'cashfree') == 'implemented'
        assert registry.status('payouts', 'cashfree') == 'implemented'

    def test_india_resolves_to_razorpay_and_the_us_never_does(self, settings):
        from servers.payments.payment_gateways.factory import (
            get_payment_gateway_for_payments, get_payment_gateway_for_payouts, provider_name_for,
        )
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.PAYOUT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        settings.RAZORPAYX_ACCOUNT_NUMBER = rz.ACCOUNT
        assert get_payment_gateway_for_payments('IN').get_name() == 'razorpay'
        assert get_payment_gateway_for_payouts('IN').get_name() == 'razorpay'
        for kind, getter in (('payments', get_payment_gateway_for_payments),
                             ('payouts', get_payment_gateway_for_payouts)):
            assert provider_name_for(kind, 'US') is None
            us = getter('US')
            assert not us and us.get_name() == 'none'
        assert not get_payment_gateway_for_payments('')

    def test_india_without_keys_is_unavailable_not_cashfree(self, settings):
        from servers.payments.payment_gateways.factory import get_payment_gateway_for_payments
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay'}
        settings.RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_SECRET = ''
        gateway = get_payment_gateway_for_payments('IN')
        assert not gateway and gateway.get_name() == 'razorpay'
        assert 'key id is not set' in gateway.reason

    def test_available_gateways_lists_razorpay_only_when_keys_are_present(self, settings):
        from servers.payments.payment_gateways.factory import get_available_gateways
        settings.RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_SECRET = ''
        assert 'razorpay' not in get_available_gateways()
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        assert 'razorpay' in get_available_gateways()


class TestKeyGuard:
    @pytest.mark.parametrize('key,production,ok', [
        ('rzp_test_fakefakefake', False, True), ('rzp_test_fakefakefake', True, True),
        ('rzp_live_fakefakefake', False, False), ('rzp_live_fakefakefake', True, True),
        ('', False, False), ('garbage', True, False)])
    def test_key_refusal(self, settings, key, production, ok):
        from integrations import razorpay_http
        settings.IS_PRODUCTION = production
        assert (razorpay_http.key_refusal(key) is None) is ok

    def test_the_reason_never_contains_the_key(self, settings):
        from integrations import razorpay_http
        settings.IS_PRODUCTION = False
        assert 'fakefakefake' not in razorpay_http.key_refusal('rzp_live_fakefakefake')

    def test_no_request_is_sent_with_a_live_key_on_qa(self, settings, monkeypatch):
        server = rz.FakeRazorpay().install(monkeypatch)
        settings.IS_PRODUCTION = False
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = 'rzp_live_fakefakefake', 'x'
        settings.RAZORPAYX_ACCOUNT_NUMBER = rz.ACCOUNT
        from integrations.payments.registry import adapter_class
        assert adapter_class('payments', 'razorpay')().create_order(100, trip_id=1) is None
        assert adapter_class('payouts', 'razorpay')().create_upi_payout('a@b', 100, reference_id='r') is None
        assert server.calls == []


class TestReadiness:
    def rows(self, client):
        return {(r['type'], r['country']): r for r in data(client.get(URL))['rows']}

    def test_india_rows_show_razorpay_and_missing_until_keys_are_present(self, geo, settings):
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.PAYOUT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_SECRET = ''
        settings.RAZORPAYX_KEY_ID = settings.RAZORPAYX_KEY_SECRET = ''
        settings.RAZORPAYX_ACCOUNT_NUMBER = ''
        client, _ = operator('super_admin', geo)
        got = self.rows(client)
        for key in (('payment_gateway', 'IN'), ('payout_provider', 'IN')):
            assert got[key]['provider'] == 'razorpay' and got[key]['configured'] is False
            assert got[key]['mode'] == 'off'
        assert 'RAZORPAY_KEY_ID' in got[('payment_gateway', 'IN')]['env_vars']
        assert 'RAZORPAYX_ACCOUNT_NUMBER' in got[('payout_provider', 'IN')]['env_vars']
        assert got[('payment_gateway', 'US')]['provider'] == 'none'
        assert got[('payout_provider', 'US')]['provider'] == 'none'

    def test_payments_need_only_the_key_pair_and_payouts_also_the_account(self, geo, settings):
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.PAYOUT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        settings.RAZORPAYX_KEY_ID = settings.RAZORPAYX_KEY_SECRET = ''
        settings.RAZORPAYX_ACCOUNT_NUMBER = ''
        client, _ = operator('super_admin', geo)
        got = self.rows(client)
        assert got[('payment_gateway', 'IN')]['configured'] is True
        assert got[('payment_gateway', 'IN')]['mode'] == 'sandbox'   # a rzp_test_ key
        assert got[('payout_provider', 'IN')]['configured'] is False
        settings.RAZORPAYX_ACCOUNT_NUMBER = rz.ACCOUNT
        assert self.rows(client)[('payout_provider', 'IN')]['configured'] is True

    def test_a_missing_webhook_secret_is_a_warning_not_missing(self, geo, settings):
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = ''
        client, _ = operator('super_admin', geo)
        row = self.rows(client)[('payment_gateway', 'IN')]
        assert row['configured'] is True and 'RAZORPAY_WEBHOOK_SECRET is not set' in row['note']
        from servers.admin_access import readiness
        item = next(i for i in readiness.city_readiness(geo['IN-TG-WGL']) if i['key'] == 'payment_gateway')
        assert item['status'] == 'warning' and item['blocking'] is False
        settings.RAZORPAY_WEBHOOK_SECRET = rz.WEBHOOK_SECRET
        assert 'not set' not in self.rows(client)[('payment_gateway', 'IN')]['note']

    def test_a_live_key_on_a_non_production_deployment_is_not_configured(self, geo, settings):
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.IS_PRODUCTION = False
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = 'rzp_live_fakefakefake', 'x'
        client, _ = operator('super_admin', geo)
        row = self.rows(client)[('payment_gateway', 'IN')]
        assert row['configured'] is False and 'refused' in row['note']

    def test_the_city_readiness_gateway_item_is_missing_until_keys_exist(self, geo, settings):
        from servers.admin_access import readiness
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_SECRET = ''
        item = next(i for i in readiness.city_readiness(geo['IN-TG-WGL']) if i['key'] == 'payment_gateway')
        assert item['status'] == 'missing'
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = rz.WEBHOOK_SECRET
        item = next(i for i in readiness.city_readiness(geo['IN-TG-WGL']) if i['key'] == 'payment_gateway')
        assert item['status'] == 'ok'

    def test_no_razorpay_value_leaves_the_response(self, geo, settings):
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.PAYOUT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        planted = {'RAZORPAY_KEY_ID': 'rzp_test_zzzzzzzzzzzz1', 'RAZORPAY_KEY_SECRET': 'zzzzzzzzzzzz-2',
                   'RAZORPAY_WEBHOOK_SECRET': 'zzzzzzzzzzzz-3', 'RAZORPAYX_ACCOUNT_NUMBER': 'zzzzzzzzzzzz-4',
                   'RAZORPAYX_WEBHOOK_SECRET': 'zzzzzzzzzzzz-5'}
        for name, value in planted.items():
            setattr(settings, name, value)
        client, _ = operator('super_admin', geo)
        text = client.get(URL).content.decode()
        assert 'zzzzzzzzzzzz' not in text and 'rzp_test_' not in text
        assert 'RAZORPAY_KEY_SECRET' in text  # the NAME is shown, never the value


class TestStartup:
    def test_razorpay_can_boot_without_keys_but_cashfree_selected_needs_its_secret(self, settings):
        from django.apps import apps
        from django.core.exceptions import ImproperlyConfigured
        config = apps.get_app_config('payments')
        settings.DEBUG = False
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.PAYOUT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.RAZORPAY_WEBHOOK_SECRET = settings.RAZORPAYX_WEBHOOK_SECRET = ''
        config.ready()  # no keys, no secrets: it must still boot (the webhooks fail closed)
        settings.PAYMENT_PROVIDERS = {'IN': 'cashfree', 'US': None}
        settings.CASHFREE_WEBHOOK_SECRET = settings.CASHFREE_SECRET_KEY = ''
        with pytest.raises(ImproperlyConfigured):
            config.ready()


class TestTestModeCheckCommand:
    def _run(self):
        out = io.StringIO()
        try:
            call_command('razorpay_test_mode_check', stdout=out)
        finally:
            self.text = out.getvalue()

    def test_it_refuses_a_live_key(self, settings):
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = 'rzp_live_fakefakefake', 'x'
        with pytest.raises(CommandError, match='refusing'):
            call_command('razorpay_test_mode_check', stdout=io.StringIO())

    def test_it_refuses_a_live_payout_key_even_with_a_test_payment_key(self, settings):
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        settings.RAZORPAYX_KEY_ID, settings.RAZORPAYX_KEY_SECRET = 'rzp_live_fakefakefake', 'x'
        with pytest.raises(CommandError, match='refusing'):
            call_command('razorpay_test_mode_check', stdout=io.StringIO())

    def test_it_needs_test_keys(self, settings):
        settings.RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_SECRET = ''
        with pytest.raises(CommandError, match='not set'):
            call_command('razorpay_test_mode_check', stdout=io.StringIO())

    def test_it_runs_the_whole_chain_against_the_recorded_server(self, settings, monkeypatch):
        server = rz.FakeRazorpay().install(monkeypatch)
        settings.IS_PRODUCTION = False
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        settings.RAZORPAYX_KEY_ID = settings.RAZORPAYX_KEY_SECRET = ''
        settings.RAZORPAYX_ACCOUNT_NUMBER = rz.ACCOUNT
        self._run()
        assert 'FAIL' not in self.text and self.text.count('PASS') == 9, self.text
        assert len(server.payouts) == 1                      # the same key twice: one payout
        assert len(server.calls_to('POST', '/payouts')) == 2
        # no secret and no full id is printed
        assert rz.KEY_SECRET not in self.text and rz.ACCOUNT not in self.text
        for oid in list(server.orders) + list(server.payouts):
            assert oid not in self.text

    def test_payout_steps_are_skipped_without_the_account_number(self, settings, monkeypatch):
        rz.FakeRazorpay().install(monkeypatch)
        settings.IS_PRODUCTION = False
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        settings.RAZORPAYX_ACCOUNT_NUMBER = ''
        self._run()
        assert 'SKIP' in self.text and 'FAIL' not in self.text


class TestReconciliationReadsRazorpayDeliveries:
    def test_payment_events_parse_in_minor_units(self):
        from servers.payments import reconciliation as rec
        body = json.loads(rz.webhook_body('payment.captured', payment={
            'id': 'pay_1', 'order_id': 'order_1', 'amount': 19500, 'currency': 'INR'}))
        assert rec.parse_payment_webhook(body) == {
            'order_id': 'order_1', 'amount_minor': 19500, 'currency': 'INR'}
        assert 'payment.captured' in rec.PAYMENT_SUCCESS_EVENTS and 'order.paid' in rec.PAYMENT_SUCCESS_EVENTS

    def test_payout_events_parse_to_our_reference_and_the_provider_id(self):
        from servers.payments import reconciliation as rec
        body = json.loads(rz.webhook_body('payout.processed', payout={
            'id': 'pout_1', 'reference_id': 'withdrawal_3_driver_4'}))
        assert rec.parse_payout_webhook(body) == ['withdrawal_3_driver_4', 'pout_1']
        assert 'payout.processed' in rec.PAYOUT_SUCCESS_EVENTS
        assert rec.INBOX_GATEWAYS['razorpay'] == ('razorpay', 'razorpayx')

    def test_cashfree_shapes_still_parse(self):
        from servers.payments import reconciliation as rec
        assert rec.parse_payment_webhook(
            {'data': {'order': {'order_id': 'o1', 'order_amount': 10, 'order_currency': 'INR'}}}
        )['amount_minor'] == 1000
