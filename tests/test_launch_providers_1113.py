"""Providers for launch (13 Nov): MSG91 DLT, Twilio SMS and Voice, SES suppression, Stripe, Connect.

No network: every vendor call is mocked at `requests`. Test keys only.
"""
import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from base import sms_templates
from integrations import stripe_http

User = get_user_model()


def _resp(code, body):
    return mock.Mock(status_code=code, json=lambda: body, ok=code < 400)


# --- SMS templates and DLT ---------------------------------------------------------------------

def test_every_template_renders_matches_and_has_a_dlt_form():
    for kind, t in sms_templates.TEMPLATES.items():
        text = sms_templates.render(kind, **t.sample)
        assert sms_templates.matches(kind, text), kind
        assert '{#var#}' in sms_templates.dlt_text(kind) or '{#url#}' in sms_templates.dlt_text(kind)
        assert t.category in ('service', 'promotional')
    assert not sms_templates.matches('otp', 'Your code is 123456')
    assert sms_templates.dlt_text('trip_share').endswith('Follow it live: {#url#}')


@pytest.mark.django_db
def test_the_dlt_export_lists_direct_and_pipeline_sms(tmp_path):
    from django.core.management import call_command

    out = tmp_path / 'dlt.csv'
    call_command('export_dlt_templates', out=str(out))
    text = out.read_text(encoding='utf-8')
    assert text.splitlines()[0] == 'type,text,category,sample,purpose'
    assert 'otp,Your OTP for SaaradhiGo is {#var#}.' in text and 'payout.missing_details' in text


def test_msg91_gets_the_flow_and_variables_and_refuses_drift(settings):
    from integrations.sms import direct
    from servers.notifications.providers.base import PermanentProviderError

    settings.SMS_DIRECT_PROVIDERS = {'IN': 'msg91', 'US': 'twilio'}
    settings.MSG91_AUTH_KEY = 'test-key'
    settings.MSG91_FLOW_IDS = {'otp': 'flow-otp'}
    settings.MSG91_DLT_TEMPLATE_IDS = {'otp': '1107000000000000001'}
    with mock.patch('integrations.sms.msg91.requests.post',
                    return_value=_resp(200, {'type': 'success', 'message': 'req-1'})) as post:
        assert direct.send('+919700000010', sms_templates.render('otp', otp='482913'), 'otp',
                           {'otp': '482913'}) == 'req-1'
    body = post.call_args.kwargs['json']
    assert body['template_id'] == 'flow-otp'
    assert body['recipients'] == [{'mobiles': '919700000010', 'otp': '482913'}]
    with pytest.raises(PermanentProviderError):
        direct.send('+919700000010', 'Your code is 482913', 'otp', {'otp': '482913'})
    with pytest.raises(PermanentProviderError):
        direct.send('+919700000010', sms_templates.render('oncall_page', ref='SOS-1'), 'oncall_page',
                    {'ref': 'SOS-1'})        # no flow id for this kind yet


@pytest.mark.django_db
def test_the_sign_in_code_goes_through_the_market_provider(settings):
    from base.utils import send_otp_via_sns, stash_sms_message

    settings.SMS_DIRECT_PROVIDERS = {'US': 'twilio'}
    settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN = 'ACtest', 'test-token'
    settings.TWILIO_MESSAGING_SERVICE_SID = 'MGtest'
    ref = stash_sms_message(sms_templates.render('otp', otp='111222'), kind='otp', values={'otp': '111222'})
    with mock.patch('integrations.sms.twilio.requests.post',
                    return_value=_resp(201, {'sid': 'SM1', 'status': 'queued'})) as post, \
            mock.patch('base.utils.get_sns_client') as sns:
        result = send_otp_via_sns.apply(args=['+12125550142', ref]).get()
    assert result == {'success': True, 'message_id': 'SM1'}
    assert post.call_args.kwargs['data']['Body'].startswith('Your OTP for SaaradhiGo is 111222')
    sns.assert_not_called()


# --- Twilio Voice -----------------------------------------------------------------------------

@pytest.mark.django_db
def test_us_calls_use_twilio_voice_and_india_stays_on_the_fake(settings):
    from servers.callbridge.providers import FakeProvider, get_provider

    settings.CALL_BRIDGE_PROVIDERS = {'US': 'twilio'}
    settings.CALL_BRIDGE_REAL_ENABLED = True
    settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN = 'ACtest', 'test-token'
    settings.TWILIO_VOICE_NUMBER = '+12125550100'
    assert isinstance(get_provider('IN'), FakeProvider)
    voice = get_provider('US')
    assert voice.name == 'twilio'
    from django.utils import timezone
    session = SimpleNamespace(pk=1, expires_at=timezone.now() + timezone.timedelta(minutes=20),
                              provider_ref='')
    with mock.patch('integrations.voice.twilio_voice.requests.post',
                    return_value=_resp(201, {'sid': 'CA1'})) as post:
        bridge = voice.start(session, '+12125550142', '+12125550199')
        assert voice.page('+12125550177', 'SaaradhiGo SOS SOS-1 has not been acknowledged.', 'sos') == 'CA1'
    first = post.call_args_list[0].kwargs['data']
    assert bridge.kind == 'session' and bridge.number == '' and bridge.ref == 'CA1'
    assert first['To'] == '+12125550142' and first['From'] == '+12125550100'
    assert '<Number>+12125550199</Number>' in first['Twiml'] and 'callerId="+12125550100"' in first['Twiml']


# --- SES suppression --------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_bounced_address_gets_no_mail(settings):
    from base.auth_email import send_message
    from servers.notifications import preferences

    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    settings.DEFAULT_FROM_EMAIL = 'noreply@example.test'
    settings.AUTH_EMAIL_ALLOWLIST = ['@example.test']
    user = User.objects.create_user(phone_number='+919700000077', email='gone@example.test',
                                    password='x-Long-pass-1', role='rider')
    assert send_message(to='gone@example.test', subject='s', body='b')[0]
    preferences.opt_out_channel(user, 'email', 'webhook:ses', 'notification_email_bounce_opt_out', 'bounce')
    sent, reason = send_message(to='gone@example.test', subject='s', body='b')
    assert not sent and 'suppressed' in reason


# --- Stripe -----------------------------------------------------------------------------------

def test_stripe_signatures():
    body = b'{"id":"evt_1"}'
    header = stripe_http.sign(body, 'whsec_test')
    assert stripe_http.verify_signature(body, header, 'whsec_test')
    assert not stripe_http.verify_signature(body, header, 'whsec_other')
    assert not stripe_http.verify_signature(body + b' ', header, 'whsec_test')
    old = stripe_http.sign(body, 'whsec_test', stamp=int(time.time()) - 3600)
    assert not stripe_http.verify_signature(body, old, 'whsec_test')


def test_stripe_payments_test_mode(settings):
    from integrations.payments.stripe import StripeGateway

    settings.STRIPE_SECRET_KEY = 'sk_live_x'
    settings.IS_PRODUCTION = False
    assert not StripeGateway()                       # a live key is refused outside production
    settings.STRIPE_SECRET_KEY = 'sk_test_x'
    settings.STRIPE_PUBLISHABLE_KEY = 'pk_test_x'
    gw = StripeGateway()
    with mock.patch('integrations.stripe_http.requests.request', return_value=_resp(
            200, {'id': 'pi_1', 'client_secret': 'pi_1_secret', 'amount': 2350, 'status': 'requires_payment_method'})) as req:
        order = gw.create_order(2350, trip_id=9, currency='USD')
    assert order['client_secret'] == 'pi_1_secret' and order['publishable_key'] == 'pk_test_x'
    sent = req.call_args.kwargs
    assert sent['data']['amount'] == '2350' and sent['data']['payment_method_types[0]'] == 'card'
    assert sent['headers']['Idempotency-Key'] == 'pi_trip_9'
    with mock.patch('integrations.stripe_http.requests.request',
                    return_value=_resp(200, {'id': 'pi_1', 'status': 'succeeded', 'amount': 2350,
                                             'amount_received': 2350, 'latest_charge': 'ch_1'})):
        assert gw.verify_payment_signature('pi_1')
        assert gw.get_order_status('pi_1')['order_status'] == 'PAID'
    assert gw.create_order(2350, trip_id=9, currency='INR') is None


@pytest.mark.django_db
def test_stripe_webhook_is_signed_deduplicated_and_routed(settings):
    settings.STRIPE_SECRET_KEY = 'sk_test_x'
    settings.STRIPE_WEBHOOK_SECRET = 'whsec_test'
    body = json.dumps({'id': 'evt_1', 'type': 'payment_intent.succeeded', 'created': int(time.time()),
                       'data': {'object': {'id': 'pi_1', 'latest_charge': 'ch_1'}}}).encode()
    url = '/api/v1/webhooks/stripe/'
    assert Client().post(url, body, content_type='application/json',
                         HTTP_STRIPE_SIGNATURE='t=1,v1=bad').status_code == 400
    with mock.patch('servers.payments.views._handle_cashfree_webhook') as handler:
        from django.http import JsonResponse
        handler.return_value = JsonResponse({'status': 'ok'})
        sig = stripe_http.sign(body, 'whsec_test')
        r1 = Client().post(url, body, content_type='application/json', HTTP_STRIPE_SIGNATURE=sig)
        r2 = Client().post(url, body, content_type='application/json', HTTP_STRIPE_SIGNATURE=sig)
    assert r1.status_code == 200 and r2.json()['status'] == 'already_processed'
    event = handler.call_args.args[0]
    assert event['type'] == 'PAYMENT_SUCCESS_WEBHOOK' and event['data']['order']['order_id'] == 'pi_1'


def test_stripe_connect_transfer(settings):
    from integrations.payouts.stripe_connect import StripeConnectGateway
    from servers.payments.payment_gateways.base_gateway import PayoutDispatchUnknown

    settings.STRIPE_SECRET_KEY = 'sk_test_x'
    gw = StripeConnectGateway()
    with mock.patch('integrations.stripe_http.requests.request',
                    return_value=_resp(200, {'id': 'tr_1', 'amount': 4200, 'destination': 'acct_1'})) as req:
        result = gw.create_connect_transfer('acct_1', 4200, 'USD', reference_id='withdrawal_5_driver_2')
    assert result['payout_id'] == 'withdrawal_5_driver_2' and result['gateway_reference'] == 'tr_1'
    assert req.call_args.kwargs['data']['destination'] == 'acct_1'
    with mock.patch('integrations.stripe_http.requests.request', return_value=_resp(502, {})):
        with pytest.raises(PayoutDispatchUnknown):
            gw.create_connect_transfer('acct_1', 4200, 'USD', reference_id='withdrawal_5_driver_2')
    assert gw.create_connect_transfer('acct_1', 4200, 'INR', reference_id='x') is None


@pytest.mark.django_db
def test_usd_payouts_stay_held_until_stripe_connect_is_configured(settings):
    from servers.payments import payout_rails

    settings.PAYOUT_PROVIDERS = {'US': 'stripe_connect'}
    settings.STRIPE_SECRET_KEY = ''
    assert payout_rails.has_rail('INR') and not payout_rails.has_rail('USD')
    settings.STRIPE_SECRET_KEY = 'sk_test_x'
    assert payout_rails.has_rail('USD')
