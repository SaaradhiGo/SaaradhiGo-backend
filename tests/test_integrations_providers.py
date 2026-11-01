"""Provider flexibility (owner ruling, AUDIT 27.62): every provider is swappable by configuration.

  * AWS SNS SMS + push adapters are SANDBOX-ONLY: no AWS call without NOTIFY_LIVE_SEND AND
    SNS_LIVE_CONFIRMED; botocore Stubber is used for the confirmed path, so nothing is real.
  * Payments / payouts are selected PER COUNTRY; a country with no provider is "not configured",
    never India's provider. Razorpay / Stripe / Stripe Connect are clean stubs.
  * Cashfree: one consistent set of environment names, hosts derived from the environment.
  * The integrations page and readiness read the same registry.

No network, no credentials: every value below is an obviously fake, low-entropy placeholder.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from notifications_support import (  # noqa: F401  (fixtures)
    block_the_network, fake_credentials, geo, sms_world,
)
from servers.notifications import config, dispatcher
from servers.notifications.models import SUPPRESSED, NotificationMessage
from servers.notifications.providers import build
from servers.notifications.providers.base import (
    LiveSendDisabled, Outbound, PermanentProviderError, TransientProviderError,
)


# ---------------------------------------------------------------------------
# AWS SNS (SMS and push): sandbox-only
# ---------------------------------------------------------------------------

def _outbound(channel='sms', **kw):
    base = dict(message_id=7, channel=channel, category='transactional', country='US', user_id=1,
                to_phone='+15005550006', push_token='device-token', title='T', body='Hello')
    base.update(kw)
    return Outbound(**base)


@pytest.fixture
def sns_settings(settings):
    settings.SNS_AWS_ACCESS_KEY_ID = 'test-id'
    settings.SNS_AWS_SECRET_ACCESS_KEY = 'test-secret'
    settings.SNS_AWS_REGION = 'us-east-1'
    settings.SNS_PLATFORM_APPLICATION_ARN_FCM = 'arn:aws:sns:us-east-1:000000000000:app/GCM/test'
    settings.SNS_PLATFORM_APPLICATION_ARN_APNS = 'arn:aws:sns:us-east-1:000000000000:app/APNS/test'
    return settings


def _stubbed_sns():
    import boto3
    from botocore.stub import Stubber

    client = boto3.client('sns', region_name='us-east-1', aws_access_key_id='test-id',
                          aws_secret_access_key='test-secret')
    return client, Stubber(client)


class TestSnsIsSandboxOnly:
    def test_sns_confirmed_defaults_to_off(self):
        from base import settings as base_settings
        assert base_settings.SNS_LIVE_CONFIRMED is False
        assert config.sns_confirmed() is False

    def test_sns_is_a_valid_provider_for_sms_and_push(self):
        from servers.notifications.providers import live_class
        assert live_class('sns', 'sms').name == 'sns'
        assert live_class('sns', 'push').name == 'sns'
        with pytest.raises(LookupError):
            live_class('sns', 'email')

    def test_the_live_switch_alone_does_not_construct_it(self, settings, sns_settings):
        settings.NOTIFY_LIVE_SEND = True
        settings.SNS_LIVE_CONFIRMED = False
        with mock.patch('boto3.client') as factory:
            for channel in ('sms', 'push'):
                with pytest.raises(LiveSendDisabled):
                    build('sns', channel)
        factory.assert_not_called()

    def test_the_master_switch_off_refuses_even_when_confirmed(self, settings, sns_settings):
        settings.NOTIFY_LIVE_SEND = False
        settings.SNS_LIVE_CONFIRMED = True
        with mock.patch('boto3.client') as factory:
            with pytest.raises(LiveSendDisabled):
                build('sns', 'sms')
        factory.assert_not_called()

    def test_a_confirmed_send_goes_through_the_stubbed_client(self, settings, sns_settings):
        settings.NOTIFY_LIVE_SEND = True
        settings.SNS_LIVE_CONFIRMED = True
        client, stub = _stubbed_sns()
        stub.add_response('publish', {'MessageId': 'mid-1'}, {
            'PhoneNumber': '+15005550006', 'Message': 'Hello',
            'MessageAttributes': {'AWS.SNS.SMS.SMSType': {
                'DataType': 'String', 'StringValue': 'Transactional'}}})
        with stub, mock.patch('boto3.client', return_value=client):
            result = build('sns', 'sms').send(_outbound())
        assert result.provider_message_id == 'mid-1' and result.status == 'sent'

    def test_sms_errors_are_classified(self, settings, sns_settings):
        settings.NOTIFY_LIVE_SEND = True
        settings.SNS_LIVE_CONFIRMED = True
        for code, expected in (('Throttled', TransientProviderError),
                               ('InvalidParameter', PermanentProviderError)):
            client, stub = _stubbed_sns()
            stub.add_client_error('publish', service_error_code=code)
            with stub, mock.patch('boto3.client', return_value=client):
                with pytest.raises(expected):
                    build('sns', 'sms').send(_outbound())

    def test_push_registers_the_endpoint_then_publishes_per_platform(self, settings, sns_settings):
        settings.NOTIFY_LIVE_SEND = True
        settings.SNS_LIVE_CONFIRMED = True
        client, stub = _stubbed_sns()
        endpoint = 'arn:aws:sns:us-east-1:000000000000:endpoint/GCM/test/1'
        stub.add_response('create_platform_endpoint', {'EndpointArn': endpoint}, {
            'PlatformApplicationArn': settings.SNS_PLATFORM_APPLICATION_ARN_FCM,
            'Token': 'device-token'})
        stub.add_response('publish', {'MessageId': 'mid-2'}, {
            'TargetArn': endpoint, 'MessageStructure': 'json', 'Message': mock.ANY})
        with stub, mock.patch('boto3.client', return_value=client):
            result = build('sns', 'push').send(_outbound('push'))
        assert result.provider_message_id == 'mid-2'

    def test_push_needs_the_platform_application_arn(self, settings, sns_settings):
        settings.NOTIFY_LIVE_SEND = True
        settings.SNS_LIVE_CONFIRMED = True
        settings.SNS_PLATFORM_APPLICATION_ARN_FCM = ''
        client, _ = _stubbed_sns()
        with mock.patch('boto3.client', return_value=client):
            adapter = build('sns', 'push')
            with pytest.raises(PermanentProviderError):
                adapter.send(_outbound('push'))


@pytest.mark.django_db
class TestSnsInTheDispatcher:
    def _switch_sms_to_sns(self, settings):
        settings.NOTIFY_PROVIDERS = {**settings.NOTIFY_PROVIDERS,
                                     'sms': {'IN': 'sns', 'US': 'sns'}}
        settings.NOTIFY_LIVE_SEND = True
        settings.NOTIFY_TEST_RECIPIENTS = ['+919700000001']

    def test_selection_is_by_configuration_only(self, settings):
        self._switch_sms_to_sns(settings)
        assert config.effective_provider('sms', 'IN') == ('sns', True)
        assert config.effective_provider('sms', 'US') == ('sns', True)
        settings.NOTIFY_LIVE_SEND = False
        assert config.effective_provider('sms', 'IN') == ('stub', False)

    def test_unconfirmed_sns_is_recorded_as_suppressed_and_makes_no_call(
            self, geo, settings, monkeypatch):
        self._switch_sms_to_sns(settings)
        settings.SNS_LIVE_CONFIRMED = False
        import servers.notifications.tasks as tasks
        monkeypatch.setattr(tasks.send_message, 'delay', lambda *a, **k: None)
        world = sms_world(geo)
        with mock.patch('boto3.client') as factory:
            rows = dispatcher.notify('test.event', world.user, {'thing': 'x'},
                                     idempotency_key='sns-1')
            sms = next(m for m in NotificationMessage.objects.filter(
                recipient_user_id=world.user.pk, channel='sms'))
            assert sms.status == SUPPRESSED
            assert sms.suppression_reason == 'sns not confirmed'
            assert dispatcher.deliver(sms.pk) == 'skipped'
        factory.assert_not_called()
        assert rows

    def test_the_decision_names_the_reason(self, geo, settings):
        self._switch_sms_to_sns(settings)
        settings.SNS_LIVE_CONFIRMED = False
        world = sms_world(geo)
        decision = dispatcher.decide(world.user, channel='sms', category='transactional',
                                     country='IN')
        assert decision.status == SUPPRESSED and decision.reason == 'sns not confirmed'
        settings.SNS_LIVE_CONFIRMED = True
        decision = dispatcher.decide(world.user, channel='sms', category='transactional',
                                     country='IN')
        assert decision.status != SUPPRESSED


class TestExistingNotificationAdaptersAreUnchanged:
    def test_ses_bounce_and_complaint_feed_the_suppression_list(self):
        """Not rebuilt here: tests/test_notifications_webhooks.py proves a signature-verified
        SES->SNS permanent bounce and a complaint opt the address out of email. This pins that
        those tests still exist and still point at the moved adapter."""
        import pathlib
        text = (pathlib.Path(__file__).parent / 'test_notifications_webhooks.py').read_text(
            encoding='utf-8')
        assert 'test_a_permanent_bounce_marks_failed_and_opts_the_address_out_of_email' in text
        assert 'test_a_complaint_opts_the_address_out_of_email' in text
        assert 'from integrations.email import ses as ses_module' in text


# ---------------------------------------------------------------------------
# Payments and payouts: per country, by configuration
# ---------------------------------------------------------------------------

class TestPerCountryProviderSelection:
    def test_india_defaults_to_the_legacy_setting_and_us_has_none(self, settings):
        from servers.payments.payment_gateways.factory import (
            get_payment_gateway_for_payments, get_payment_gateway_for_payouts, provider_name_for,
        )
        assert provider_name_for('payments', 'IN') == 'cashfree'
        assert provider_name_for('payouts', 'IN') == 'cashfree'
        assert provider_name_for('payments', 'US') is None
        assert get_payment_gateway_for_payments('IN').get_name() == 'cashfree'
        us = get_payment_gateway_for_payments('US')
        assert not us and us.get_name() == 'none'
        assert not get_payment_gateway_for_payouts('US')

    def test_a_country_with_no_provider_never_gets_indias(self, settings):
        from servers.payments.payment_gateways.factory import get_payment_gateway
        settings.PAYMENT_PROVIDERS = {'IN': 'cashfree', 'US': None}
        assert not get_payment_gateway(country='US')
        assert not get_payment_gateway(country='')
        assert not get_payment_gateway(country=None)
        assert not get_payment_gateway(country='ZZ')

    def test_the_table_wins_over_the_legacy_setting(self, settings):
        from servers.payments.payment_gateways.factory import provider_name_for
        settings.PAYMENT_GATEWAY = 'cashfree'
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': 'stripe'}
        settings.PAYOUT_PROVIDERS = {'IN': 'razorpay', 'US': 'stripe-connect'}
        assert provider_name_for('payments', 'IN') == 'razorpay'
        assert provider_name_for('payments', 'US') == 'stripe'
        assert provider_name_for('payouts', 'US') == 'stripe_connect'

    def test_the_legacy_setting_still_moves_india(self, settings):
        from servers.payments.payment_gateways.factory import provider_name_for
        settings.PAYMENT_PROVIDERS = {'US': None}
        settings.PAYMENT_GATEWAY = 'razorpay'
        assert provider_name_for('payments', 'IN') == 'razorpay'

    def test_the_country_comes_from_the_trip(self):
        from servers.payments.payment_gateways.factory import country_for_trip
        assert country_for_trip(SimpleNamespace(zone=SimpleNamespace(country='US'))) == 'US'
        assert country_for_trip(SimpleNamespace(zone=None, currency='USD')) == 'US'
        assert country_for_trip(SimpleNamespace(zone=None, currency='INR')) == 'IN'

    def test_the_environment_json_builds_the_table(self, monkeypatch):
        from base import settings as base_settings
        monkeypatch.setenv('PAYMENT_PROVIDERS_JSON', '{"in": "Razorpay", "us": null}')
        table = base_settings._provider_table('PAYMENT_PROVIDERS_JSON', {'US': 'stripe'})
        assert table == {'IN': 'razorpay', 'US': None}
        monkeypatch.setenv('PAYMENT_PROVIDERS_JSON', 'not json')
        from django.core.exceptions import ImproperlyConfigured
        with pytest.raises(ImproperlyConfigured):
            base_settings._provider_table('PAYMENT_PROVIDERS_JSON', {})


class TestStubGatewaysAreCleanlyUnavailable:
    @pytest.mark.parametrize('kind,name', [
        ('payments', 'razorpay'), ('payments', 'stripe'),
        ('payouts', 'razorpay'), ('payouts', 'stripe_connect'),
    ])
    def test_selecting_a_stub_degrades_to_not_available(self, settings, kind, name):
        from servers.payments.payment_gateways.base_gateway import NotConfigured
        from servers.payments.payment_gateways.factory import (
            get_payment_gateway_for_payments, get_payment_gateway_for_payouts,
        )
        settings.PAYMENT_PROVIDERS = {'IN': name, 'US': name}
        settings.PAYOUT_PROVIDERS = {'IN': name, 'US': name}
        getter = (get_payment_gateway_for_payments if kind == 'payments'
                  else get_payment_gateway_for_payouts)
        with mock.patch('requests.post') as post, mock.patch('requests.get') as get:
            gateway = getter('IN')
            assert not gateway and gateway.available is False
            assert gateway.get_name() == name
            assert gateway.supported_currencies() == ()
            assert gateway.create_order(1000, 1, 'INR') is None
            assert gateway.verify_payment_signature('o') is False
            assert gateway.verify_webhook_signature(b'{}', 'sig', '1') is False
            assert gateway.create_refund('p', 100, 'INR') is None
            assert gateway.create_upi_payout('a@b', 100) is None
            assert gateway.get_order_status('o') is None
            with pytest.raises(NotConfigured):
                gateway.require()
        post.assert_not_called()
        get.assert_not_called()

    def test_the_stubs_are_registered_and_marked_as_stubs(self):
        from integrations.payments import registry
        assert registry.status('payments', 'cashfree') == 'implemented'
        assert registry.status('payments', 'razorpay') == 'stub'
        assert registry.status('payments', 'stripe') == 'stub'
        assert registry.status('payouts', 'razorpay') == 'stub'
        assert registry.status('payouts', 'stripe_connect') == 'stub'

    def test_an_unknown_explicit_name_is_still_an_error(self):
        from servers.payments.payment_gateways.factory import get_payment_gateway
        with pytest.raises(ValueError):
            get_payment_gateway('paypal')


# ---------------------------------------------------------------------------
# Cashfree environment names and hosts
# ---------------------------------------------------------------------------

class TestCashfreeEnvironment:
    def test_sandbox_never_points_at_a_production_host(self, settings):
        from integrations.payments import cashfree
        settings.CASHFREE_ENVIRONMENT = 'sandbox'
        settings.CASHFREE_PG_BASE_URL = ''
        settings.CASHFREE_PAYOUTS_BASE_URL = ''
        assert cashfree.pg_base_url() == 'https://sandbox.cashfree.com/pg'
        assert cashfree.payout_base_url() == 'https://payout-gamma.cashfree.com'
        for url in (cashfree.pg_base_url(), cashfree.payout_base_url()):
            assert '//api.cashfree.com' not in url and 'payout-api' not in url

    def test_an_unset_or_unknown_environment_is_sandbox(self, settings):
        from integrations.payments import cashfree
        for value in ('', None, 'staging', 'SANDBOX'):
            settings.CASHFREE_ENVIRONMENT = value
            assert cashfree.environment() == 'sandbox'

    def test_a_production_host_override_is_ignored_in_sandbox(self, settings):
        from integrations.payments import cashfree
        settings.CASHFREE_ENVIRONMENT = 'sandbox'
        settings.CASHFREE_PG_BASE_URL = 'https://api.cashfree.com'
        settings.CASHFREE_PAYOUTS_BASE_URL = 'https://payout-api.cashfree.com/payout/v1'
        assert cashfree.pg_base_url() == 'https://sandbox.cashfree.com/pg'
        assert cashfree.payout_base_url() == 'https://payout-gamma.cashfree.com'

    def test_production_uses_production_hosts(self, settings):
        from integrations.payments import cashfree
        settings.CASHFREE_ENVIRONMENT = 'production'
        settings.CASHFREE_PG_BASE_URL = ''
        settings.CASHFREE_PAYOUTS_BASE_URL = ''
        assert cashfree.pg_base_url() == 'https://api.cashfree.com/pg'
        assert cashfree.payout_base_url() == 'https://payout-api.cashfree.com'

    def test_a_sandbox_override_is_ignored_in_production(self, settings):
        from integrations.payments import cashfree
        settings.CASHFREE_ENVIRONMENT = 'production'
        settings.CASHFREE_PG_BASE_URL = 'https://sandbox.cashfree.com'
        assert cashfree.pg_base_url() == 'https://api.cashfree.com/pg'

    def test_the_legacy_pg_url_shape_is_normalised(self, settings):
        from integrations.payments import cashfree
        settings.CASHFREE_ENVIRONMENT = 'sandbox'
        settings.CASHFREE_PG_BASE_URL = 'https://sandbox.cashfree.com'
        assert cashfree.pg_base_url() == 'https://sandbox.cashfree.com/pg'

    def test_payout_credentials_use_the_new_names_with_the_old_as_aliases(self, settings):
        from integrations.payments import cashfree
        settings.CASHFREE_PAYOUTS_CLIENT_ID = 'new-id'
        settings.CASHFREE_PAYOUTS_CLIENT_SECRET = 'new-secret'
        settings.CASHFREE_PAYOUT_APP_ID = 'old-id'
        settings.CASHFREE_PAYOUT_SECRET_KEY = 'old-secret'
        assert cashfree.payout_credentials() == ('new-id', 'new-secret')
        settings.CASHFREE_PAYOUTS_CLIENT_ID = ''
        settings.CASHFREE_PAYOUTS_CLIENT_SECRET = ''
        assert cashfree.payout_credentials() == ('old-id', 'old-secret')
        settings.CASHFREE_PAYOUT_APP_ID = ''
        settings.CASHFREE_PAYOUT_SECRET_KEY = ''
        assert cashfree.payout_credentials() == (None, None)

    def test_the_webhook_secret_falls_back_to_the_client_secret(self, settings):
        from integrations.payments import cashfree
        settings.CASHFREE_WEBHOOK_SECRET = 'explicit'
        settings.CASHFREE_SECRET_KEY = 'client-secret'
        assert cashfree.webhook_secret() == 'explicit'
        settings.CASHFREE_WEBHOOK_SECRET = ''
        assert cashfree.webhook_secret() == 'client-secret'
        settings.CASHFREE_SECRET_KEY = ''
        assert cashfree.webhook_secret() == ''

    def test_payout_authorisation_reads_the_configured_names_and_host(self, settings):
        from integrations.payments import cashfree
        settings.CASHFREE_ENVIRONMENT = 'sandbox'
        settings.CASHFREE_PAYOUTS_BASE_URL = ''
        settings.CASHFREE_PAYOUTS_CLIENT_ID = 'cid'
        settings.CASHFREE_PAYOUTS_CLIENT_SECRET = 'csecret'
        gateway = cashfree.CashfreeGateway()
        gateway.__class__._payout_token = None
        reply = mock.Mock(status_code=200)
        reply.json.return_value = {'data': {'token': 'tok'}}
        reply.raise_for_status.return_value = None
        with mock.patch('integrations.payments.cashfree.requests.post', return_value=reply) as post:
            token = gateway._get_payout_token()
        gateway.__class__._payout_token = None
        assert token == 'tok'
        assert post.call_args.args[0] == 'https://payout-gamma.cashfree.com/payout/v1/authorize'
        assert post.call_args.kwargs['headers']['X-Client-Id'] == 'cid'

    def test_orders_are_created_on_the_derived_host(self, settings):
        from integrations.payments import cashfree
        settings.CASHFREE_ENVIRONMENT = 'sandbox'
        settings.CASHFREE_PG_BASE_URL = ''
        settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY = 'app', 'secret'
        reply = mock.Mock()
        reply.json.return_value = {'payment_session_id': 's', 'cf_order_id': 1}
        with mock.patch('integrations.payments.cashfree.requests.post', return_value=reply) as post:
            order = cashfree.CashfreeGateway().create_order(5000, 1, 'INR')
        assert order and post.call_args.args[0] == 'https://sandbox.cashfree.com/pg/orders'


# ---------------------------------------------------------------------------
# The integrations page and readiness read the same registry
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestIntegrationsPageFollowsTheRegistry:
    def _row(self, rows, ptype, country):
        return next(r for r in rows if r['type'] == ptype and r['country'] == country)

    def test_it_shows_the_configured_provider_per_country(self, settings):
        from servers.platform_ops import integrations
        settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY = 'app', 'secret'
        rows = integrations.rows()
        assert self._row(rows, 'payment_gateway', 'IN')['provider'] == 'cashfree'
        assert self._row(rows, 'payment_gateway', 'US')['provider'] == 'none'
        assert self._row(rows, 'payout_provider', 'US')['provider'] == 'none'

    def test_it_stays_correct_when_switched(self, settings):
        from servers.platform_ops import integrations
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': 'stripe'}
        settings.PAYOUT_PROVIDERS = {'IN': 'cashfree', 'US': 'stripe_connect'}
        settings.NOTIFY_PROVIDERS = {**settings.NOTIFY_PROVIDERS, 'sms': {'IN': 'msg91', 'US': 'sns'}}
        rows = integrations.rows()
        india = self._row(rows, 'payment_gateway', 'IN')
        assert india['provider'] == 'razorpay' and india['configured'] is False
        assert 'stub' in india['note']
        assert self._row(rows, 'payment_gateway', 'US')['provider'] == 'stripe'
        assert self._row(rows, 'payout_provider', 'US')['provider'] == 'stripe_connect'
        assert self._row(rows, 'sms', 'US')['provider'] == 'sns'

    def test_readiness_reads_the_same_registry(self, settings):
        from servers.platform_ops import integrations
        settings.PAYMENT_PROVIDERS = {'IN': None, 'US': None}
        assert integrations.payment_gateway_status('IN')['provider'] is None
        settings.PAYMENT_PROVIDERS = {'IN': 'cashfree'}
        settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY = 'app', 'secret'
        assert integrations.payment_gateway_status('IN')['configured'] is True
