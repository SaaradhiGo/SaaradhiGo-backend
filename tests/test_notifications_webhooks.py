"""Module 11: provider webhooks. Signature valid, signature invalid (403), idempotency, opt-outs.

  * Twilio: X-Twilio-Signature, HMAC-SHA1 over the URL and the sorted form fields.
  * SES: the SNS message signature. The cert URL must be https on an sns.<region>.amazonaws.com
    host; the cert is cached; a SubscriptionConfirmation is LOGGED and never fetched.
  * MSG91: no signature exists, so a shared secret in a header or the path, compared in constant
    time.

A failed verification answers 403, writes NO event and logs a masked warning. Everything is
signed in-test with a throwaway key; no secret here is real.
"""

import base64
import json
import logging
from datetime import datetime, timedelta, timezone as dt_utc
from unittest import mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from django.test import Client

from notifications_support import (  # noqa: F401
    block_the_network, fake_credentials, geo, make_driver_user, make_template,
)
from servers.admin_audit.models import AdminAuditLog
from servers.notifications import preferences
from servers.notifications.models import (
    DELIVERED, FAILED, NotificationMessage, NotificationPreference, NotificationStatusEvent,
)
from servers.notifications.providers import ses as ses_module
from servers.notifications.providers.twilio import TwilioSms

pytestmark = [pytest.mark.django_db]

TOPIC = 'arn:aws:sns:us-east-1:000000000000:test-topic'
CERT_URL = 'https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem'
WEBHOOK = '/api/v1/notifications/webhooks'


@pytest.fixture(autouse=True)
def fresh_cert_cache():
    ses_module._CERT_CACHE.clear()
    yield
    ses_module._CERT_CACHE.clear()


@pytest.fixture
def sent_message(geo, settings):
    """A sent message the webhooks can refer to, via each provider."""
    user = make_driver_user(geo, phone='+919700000010', email='d10@example.test')
    template = make_template(geo, channel='sms')
    message = NotificationMessage.objects.create(
        template=template, template_key='test.event', template_version=1, channel='sms',
        country='IN', recipient_user_id=user.pk, status='sent', provider='twilio',
        provider_message_id='SMtest1', idempotency_key='wh1:sms')
    return message, user


def twilio_post(params, token='test-twilio-token', signature=None, path=f'{WEBHOOK}/twilio/'):
    url = 'http://testserver' + path
    sig = signature if signature is not None else TwilioSms.signature_for(url, params, token)
    return Client().post(path, params, HTTP_X_TWILIO_SIGNATURE=sig)


# ===========================================================================
# TWILIO
# ===========================================================================

class TestTwilio:
    def test_a_valid_signature_records_the_status(self, sent_message, fake_credentials):
        message, _ = sent_message
        response = twilio_post({'MessageSid': 'SMtest1', 'MessageStatus': 'delivered'})
        assert response.status_code == 200 and response.json() == {'applied': 1, 'ignored': 0}
        message.refresh_from_db()
        assert message.status == DELIVERED
        assert NotificationStatusEvent.objects.filter(
            message=message, status='delivered', source='webhook:twilio').count() == 1

    def test_an_invalid_signature_is_403_writes_no_event_and_logs_a_masked_warning(
            self, sent_message, fake_credentials, caplog):
        message, _ = sent_message
        with caplog.at_level(logging.WARNING):
            response = twilio_post({'MessageSid': 'SMtest1', 'MessageStatus': 'delivered',
                                    'To': '+919700000010'}, signature='forged')
        assert response.status_code == 403
        message.refresh_from_db()
        assert message.status == 'sent'
        assert not NotificationStatusEvent.objects.filter(source='webhook:twilio').exists()
        logged = ' '.join(r.getMessage() for r in caplog.records)
        assert 'notification_webhook_rejected provider=twilio' in logged
        assert '9700000010' not in logged

    def test_a_tampered_body_fails_the_signature(self, sent_message, fake_credentials):
        params = {'MessageSid': 'SMtest1', 'MessageStatus': 'delivered'}
        url = 'http://testserver' + f'{WEBHOOK}/twilio/'
        sig = TwilioSms.signature_for(url, params, 'test-twilio-token')
        response = twilio_post({**params, 'MessageStatus': 'failed'}, signature=sig)
        assert response.status_code == 403

    def test_no_signature_and_no_token_configured_are_both_refused(self, sent_message, settings):
        settings.TWILIO_AUTH_TOKEN = ''
        assert twilio_post({'MessageSid': 'SMtest1', 'MessageStatus': 'delivered'},
                           signature='x').status_code == 403
        settings.TWILIO_AUTH_TOKEN = 'test-twilio-token'
        assert Client().post(f'{WEBHOOK}/twilio/', {'MessageSid': 'x'}).status_code == 403

    def test_the_base_url_setting_is_what_is_signed(self, sent_message, fake_credentials, settings):
        settings.NOTIFY_WEBHOOK_BASE_URL = 'https://api.example.test'
        params = {'MessageSid': 'SMtest1', 'MessageStatus': 'delivered'}
        url = 'https://api.example.test' + f'{WEBHOOK}/twilio/'
        sig = TwilioSms.signature_for(url, params, 'test-twilio-token')
        assert twilio_post(params, signature=sig).status_code == 200

    def test_a_repeated_callback_changes_nothing_the_second_time(self, sent_message,
                                                                 fake_credentials):
        params = {'MessageSid': 'SMtest1', 'MessageStatus': 'delivered'}
        assert twilio_post(params).json() == {'applied': 1, 'ignored': 0}
        assert twilio_post(params).json() == {'applied': 0, 'ignored': 1}
        assert NotificationStatusEvent.objects.filter(status='delivered').count() == 1

    def test_undelivered_marks_the_message_failed(self, sent_message, fake_credentials):
        message, _ = sent_message
        twilio_post({'MessageSid': 'SMtest1', 'MessageStatus': 'undelivered', 'ErrorCode': '30003'})
        message.refresh_from_db()
        assert message.status == FAILED

    def test_an_inbound_stop_opts_the_number_out_of_all_sms_and_audits_the_system_actor(
            self, sent_message, fake_credentials):
        _, user = sent_message
        response = twilio_post({'MessageSid': 'SMin1', 'From': '+919700000010', 'Body': 'STOP'})
        assert response.status_code == 200
        for category in ('transactional', 'promotional'):
            assert preferences.is_opted_in(user.pk, 'sms', category)[0] is False
        row = AdminAuditLog.objects.get(action='notification_sms_stop_received')
        assert row.actor_id is None and row.actor_label == 'system:webhook:twilio'
        assert row.target_id == str(user.pk)
        assert NotificationPreference.objects.get(
            user=user, channel='sms', category='transactional').source == 'webhook:twilio'

    def test_a_second_stop_audits_nothing_new(self, sent_message, fake_credentials):
        twilio_post({'MessageSid': 'SMin1', 'From': '+919700000010', 'Body': 'STOP'})
        twilio_post({'MessageSid': 'SMin2', 'From': '+919700000010', 'Body': 'stop'})
        assert AdminAuditLog.objects.filter(action='notification_sms_stop_received').count() == 1

    def test_an_unknown_provider_is_404_and_get_is_405(self, sent_message):
        assert Client().post(f'{WEBHOOK}/nope/', {}).status_code == 404
        assert Client().get(f'{WEBHOOK}/twilio/').status_code == 405


# ===========================================================================
# MSG91 (shared secret)
# ===========================================================================

class TestMsg91:
    def post(self, body, headers=None, path=f'{WEBHOOK}/msg91/'):
        return Client().post(path, json.dumps(body), content_type='application/json',
                             **(headers or {}))

    @pytest.fixture
    def msg91_message(self, sent_message):
        message, user = sent_message
        NotificationMessage.objects.filter(pk=message.pk).update(
            provider='msg91', provider_message_id='req-77')
        return message, user

    def test_the_header_token_authenticates(self, msg91_message, fake_credentials):
        message, _ = msg91_message
        response = self.post({'report': [{'requestId': 'req-77', 'status': '1'}]},
                             {'HTTP_X_WEBHOOK_TOKEN': 'test-webhook-secret'})
        assert response.status_code == 200
        message.refresh_from_db()
        assert message.status == DELIVERED

    def test_the_path_token_authenticates(self, msg91_message, fake_credentials):
        response = self.post({'requestId': 'req-77', 'status': 'delivered'},
                             path=f'{WEBHOOK}/msg91/test-webhook-secret/')
        assert response.status_code == 200

    def test_a_wrong_missing_or_unconfigured_token_is_403_and_writes_nothing(
            self, msg91_message, fake_credentials, settings):
        body = {'requestId': 'req-77', 'status': 'delivered'}
        assert self.post(body, {'HTTP_X_WEBHOOK_TOKEN': 'wrong'}).status_code == 403
        assert self.post(body).status_code == 403
        assert self.post(body, path=f'{WEBHOOK}/msg91/wrong/').status_code == 403
        settings.MSG91_WEBHOOK_SECRET = ''
        assert self.post(body, {'HTTP_X_WEBHOOK_TOKEN': ''}).status_code == 403
        assert not NotificationStatusEvent.objects.filter(source='webhook:msg91').exists()

    def test_the_comparison_is_constant_time(self, fake_credentials):
        import inspect

        from servers.notifications.providers import msg91
        assert 'hmac.compare_digest' in inspect.getsource(msg91.verify_token)

    def test_a_failed_report_marks_the_message_failed(self, msg91_message, fake_credentials):
        message, _ = msg91_message
        self.post({'report': [{'requestId': 'req-77', 'status': '2', 'desc': 'handset off'}]},
                  {'HTTP_X_WEBHOOK_TOKEN': 'test-webhook-secret'})
        message.refresh_from_db()
        assert message.status == FAILED

    def test_an_inbound_stop_opts_out_of_sms(self, msg91_message, fake_credentials):
        _, user = msg91_message
        self.post({'mobile': '919700000010', 'text': 'STOP'},
                  {'HTTP_X_WEBHOOK_TOKEN': 'test-webhook-secret'})
        assert preferences.is_opted_in(user.pk, 'sms', 'transactional')[0] is False

    def test_garbage_is_acknowledged_and_ignored(self, msg91_message, fake_credentials):
        response = Client().post(f'{WEBHOOK}/msg91/', 'not json', content_type='text/plain',
                                 HTTP_X_WEBHOOK_TOKEN='test-webhook-secret')
        assert response.status_code == 200 and response.json() == {'applied': 0, 'ignored': 0}


# ===========================================================================
# SES through SNS
# ===========================================================================

@pytest.fixture(scope='module')
def signer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'sns.test')])
    now = datetime.now(dt_utc.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.PEM)


def sns_message(signer, inner=None, type_='Notification', version='1', cert_url=CERT_URL,
                topic=TOPIC, tamper=False):
    key, _pem = signer
    message = {
        'Type': type_, 'MessageId': 'm-1', 'TopicArn': topic, 'Timestamp': '2026-11-01T00:00:00Z',
        'SignatureVersion': version, 'SigningCertURL': cert_url,
        'Message': json.dumps(inner) if inner is not None else 'hello',
    }
    if type_ == 'SubscriptionConfirmation':
        message.update({'Token': 'tok', 'SubscribeURL': 'https://sns.us-east-1.amazonaws.com/?x=1'})
    to_sign = ses_module.string_to_sign(message)
    algorithm = hashes.SHA1() if version == '1' else hashes.SHA256()
    message['Signature'] = base64.b64encode(
        key.sign(to_sign.encode(), padding.PKCS1v15(), algorithm)).decode()
    if tamper:
        message['Message'] = message['Message'] + ' '
    return message


class TestSes:
    @pytest.fixture(autouse=True)
    def config_and_cert(self, settings, signer):
        settings.SES_SNS_TOPIC_ARNS = [TOPIC]
        response = mock.Mock(content=signer[1])
        response.raise_for_status = lambda: None
        with mock.patch('servers.notifications.providers.ses.requests.get',
                        return_value=response) as get:
            self.get = get
            yield

    def post(self, message):
        return Client().post(f'{WEBHOOK}/ses/', json.dumps(message), content_type='text/plain')

    @pytest.fixture
    def ses_message(self, sent_message):
        message, user = sent_message
        NotificationMessage.objects.filter(pk=message.pk).update(
            provider='ses', channel='email', provider_message_id='ses-msg-1')
        return message, user

    def delivery(self):
        return {'eventType': 'Delivery', 'mail': {'messageId': 'ses-msg-1'}}

    def test_a_valid_signature_records_a_delivery(self, signer, ses_message):
        message, _ = ses_message
        assert self.post(sns_message(signer, self.delivery())).status_code == 200
        message.refresh_from_db()
        assert message.status == DELIVERED

    def test_signature_version_2_uses_sha256(self, signer, ses_message):
        assert self.post(sns_message(signer, self.delivery(), version='2')).status_code == 200

    def test_the_cert_is_fetched_once_and_cached(self, signer, ses_message):
        self.post(sns_message(signer, self.delivery()))
        self.post(sns_message(signer, {**self.delivery(), 'eventType': 'Bounce',
                                       'bounce': {'bounceType': 'Transient'}}))
        assert self.get.call_count == 1

    def test_a_tampered_message_is_403_and_writes_no_event(self, signer, ses_message):
        response = self.post(sns_message(signer, self.delivery(), tamper=True))
        assert response.status_code == 403
        assert not NotificationStatusEvent.objects.filter(source='webhook:ses').exists()

    def test_a_signature_from_another_key_is_403(self, signer, ses_message):
        other = (rsa.generate_private_key(public_exponent=65537, key_size=2048), signer[1])
        assert self.post(sns_message(other, self.delivery())).status_code == 403

    @pytest.mark.parametrize('url', [
        'http://sns.us-east-1.amazonaws.com/cert.pem',               # not https
        'https://sns.us-east-1.amazonaws.com.evil.test/cert.pem',    # lookalike host
        'https://evil.test/sns.us-east-1.amazonaws.com/cert.pem',    # host in the path
        'https://sns.us-east-1.amazonaws.com@evil.test/cert.pem',    # userinfo trick
        'https://s3.amazonaws.com/cert.pem',                         # an AWS host, not SNS
        'https://sns.us-east-1.amazonaws.com:8443/cert.pem',         # odd port
        'https://sns.us-east-1.amazonaws.com/cert.txt',              # not a .pem
        '', 'not a url',
    ])
    def test_the_cert_url_host_is_validated_and_nothing_is_fetched(self, signer, ses_message, url):
        assert ses_module.cert_url_ok(url) is False
        response = self.post(sns_message(signer, self.delivery(), cert_url=url))
        assert response.status_code == 403
        self.get.assert_not_called()

    def test_real_sns_hosts_are_accepted(self):
        assert ses_module.cert_url_ok(CERT_URL)
        assert ses_module.cert_url_ok('https://sns.eu-west-1.amazonaws.com/x.pem')
        assert ses_module.cert_url_ok('https://sns.cn-north-1.amazonaws.com.cn/x.pem')

    def test_a_topic_not_on_the_allowlist_is_refused_and_so_is_no_allowlist(
            self, signer, ses_message, settings):
        other = 'arn:aws:sns:us-east-1:000000000000:other'
        assert self.post(sns_message(signer, self.delivery(), topic=other)).status_code == 403
        settings.SES_SNS_TOPIC_ARNS = []
        assert self.post(sns_message(signer, self.delivery())).status_code == 403

    def test_a_subscription_confirmation_is_logged_and_never_fetched(
            self, signer, ses_message, caplog):
        with caplog.at_level(logging.WARNING):
            response = self.post(sns_message(signer, type_='SubscriptionConfirmation'))
        assert response.status_code == 200 and response.json() == {'applied': 0, 'ignored': 0}
        assert 'ses_sns_subscription_confirmation_received' in ' '.join(
            r.getMessage() for r in caplog.records)
        self.get.assert_called_once()           # only the CERT, never the SubscribeURL

    def test_a_forged_subscription_confirmation_is_403(self, signer, ses_message):
        message = sns_message(signer, type_='SubscriptionConfirmation')
        message['SubscribeURL'] = 'https://evil.test/confirm'
        assert self.post(message).status_code == 403

    def test_a_permanent_bounce_marks_failed_and_opts_the_address_out_of_email(
            self, signer, ses_message):
        message, user = ses_message
        inner = {'eventType': 'Bounce', 'mail': {'messageId': 'ses-msg-1'},
                 'bounce': {'bounceType': 'Permanent'}}
        assert self.post(sns_message(signer, inner)).status_code == 200
        message.refresh_from_db()
        assert message.status == FAILED
        for category in ('transactional', 'promotional'):
            assert preferences.is_opted_in(user.pk, 'email', category)[0] is False
        row = AdminAuditLog.objects.get(action='notification_email_bounce_opt_out')
        assert row.actor_id is None and row.actor_label == 'system:webhook:ses'

    def test_a_transient_bounce_does_not_opt_out(self, signer, ses_message):
        _, user = ses_message
        inner = {'eventType': 'Bounce', 'mail': {'messageId': 'ses-msg-1'},
                 'bounce': {'bounceType': 'Transient'}}
        self.post(sns_message(signer, inner))
        assert preferences.is_opted_in(user.pk, 'email', 'transactional')[0] is True

    def test_a_complaint_opts_the_address_out_of_email(self, signer, ses_message):
        _, user = ses_message
        self.post(sns_message(signer, {'eventType': 'Complaint',
                                       'mail': {'messageId': 'ses-msg-1'}}))
        assert preferences.is_opted_in(user.pk, 'email', 'transactional')[0] is False
        assert AdminAuditLog.objects.filter(action='notification_email_complaint_opt_out').exists()

    def test_garbage_is_403_not_a_crash(self, ses_message):
        for body in ('not json', '[]', '{}'):
            assert Client().post(f'{WEBHOOK}/ses/', body,
                                 content_type='text/plain').status_code == 403
