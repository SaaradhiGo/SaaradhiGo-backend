"""Module 11: each provider adapter against MOCKED HTTP and SDK calls only.

Nothing here reaches MSG91, Twilio, SES or FCM, and no credential is real: the network guard in
`notifications_support` fails any test that opens a socket. These tests pin the request each
adapter builds (documented API shape), how replies map to a message id, and which errors are
retried (transient) and which are final (permanent).
"""

from unittest import mock

import pytest
import requests

from notifications_support import block_the_network, fake_credentials  # noqa: F401
from servers.notifications.providers.base import (
    Outbound, PermanentProviderError, TransientProviderError,
)

pytestmark = []


def reply(status=200, body=None):
    response = mock.Mock(status_code=status)
    response.json.return_value = body if body is not None else {}
    return response


def sms_outbound(**kw):
    base = dict(message_id=7, channel='sms', category='transactional', country='IN', user_id=1,
                to_phone='+919876543210', body='Hello 1 2',
                values=['1', '2'], data={'variable_names': ['a', 'b']},
                registration={'provider_template_id': 'flow-1', 'sender_id': 'SGOTST',
                              'dlt_template_id': '1107000000000000001'})
    base.update(kw)
    return Outbound(**base)


class TestMsg91Sms:
    def adapter(self, fake_credentials):
        from servers.notifications.providers.msg91 import Msg91Sms
        return Msg91Sms()

    def test_builds_the_documented_flow_request(self, fake_credentials):
        with mock.patch('servers.notifications.providers.msg91.requests.post',
                        return_value=reply(200, {'type': 'success', 'message': 'req-abc'})) as post:
            result = self.adapter(fake_credentials).send(sms_outbound())
        assert result.provider_message_id == 'req-abc' and result.status == 'sent'
        (url,), kwargs = post.call_args
        assert url == 'https://control.msg91.com/api/v5/flow/'
        assert kwargs['headers']['authkey'] == 'test-msg91-key'
        assert kwargs['json'] == {
            'template_id': 'flow-1', 'short_url': '0',
            'recipients': [{'mobiles': '919876543210', 'a': '1', 'b': '2'}]}
        assert kwargs['timeout'] == 10

    def test_errors_are_classified(self, fake_credentials):
        adapter = self.adapter(fake_credentials)
        for status, error in ((500, TransientProviderError), (429, TransientProviderError),
                              (400, PermanentProviderError), (401, PermanentProviderError)):
            with mock.patch('servers.notifications.providers.msg91.requests.post',
                            return_value=reply(status, {'type': 'error', 'message': 'no'})):
                with pytest.raises(error):
                    adapter.send(sms_outbound())
        with mock.patch('servers.notifications.providers.msg91.requests.post',
                        return_value=reply(200, {'type': 'error', 'message': 'bad template'})):
            with pytest.raises(PermanentProviderError):
                adapter.send(sms_outbound())
        with mock.patch('servers.notifications.providers.msg91.requests.post',
                        side_effect=requests.Timeout()):
            with pytest.raises(TransientProviderError):
                adapter.send(sms_outbound())

    def test_no_flow_id_is_permanent_and_no_key_refuses_to_construct(self, fake_credentials,
                                                                      settings):
        with pytest.raises(PermanentProviderError):
            self.adapter(fake_credentials).send(sms_outbound(registration={}))
        settings.MSG91_AUTH_KEY = ''
        with pytest.raises(PermanentProviderError):
            self.adapter(fake_credentials)


class TestMsg91WhatsApp:
    def test_builds_the_documented_template_request(self, fake_credentials, settings):
        from servers.notifications.providers.msg91 import Msg91WhatsApp
        settings.MSG91_WHATSAPP_NUMBER = '919000000000'
        outbound = sms_outbound(channel='whatsapp', registration={
            'whatsapp_template_name': 'commission_notice', 'whatsapp_namespace': 'ns_1'})
        with mock.patch('servers.notifications.providers.msg91.requests.post',
                        return_value=reply(200, {'type': 'success', 'request_id': 'wa-1'})) as post:
            result = Msg91WhatsApp().send(outbound)
        assert result.provider_message_id == 'wa-1'
        (url,), kwargs = post.call_args
        assert url.endswith('/whatsapp/whatsapp-outbound-message/bulk/')
        template = kwargs['json']['payload']['template']
        assert template['name'] == 'commission_notice' and template['namespace'] == 'ns_1'
        assert template['to_and_components'][0]['to'] == ['919876543210']
        assert template['to_and_components'][0]['components'] == {
            'body_1': {'type': 'text', 'value': '1'}, 'body_2': {'type': 'text', 'value': '2'}}
        assert kwargs['json']['integrated_number'] == '919000000000'

    def test_a_missing_template_name_is_permanent(self, fake_credentials):
        from servers.notifications.providers.msg91 import Msg91WhatsApp
        with pytest.raises(PermanentProviderError):
            Msg91WhatsApp().send(sms_outbound(channel='whatsapp', registration={}))


class TestTwilio:
    def adapter(self):
        from servers.notifications.providers.twilio import TwilioSms
        return TwilioSms()

    def test_builds_the_documented_messages_request(self, fake_credentials):
        outbound = sms_outbound(country='US', to_phone='+15005550001', body='Hi there')
        with mock.patch('servers.notifications.providers.twilio.requests.post',
                        return_value=reply(201, {'sid': 'SMabc', 'status': 'queued'})) as post:
            result = self.adapter().send(outbound)
        assert result.provider_message_id == 'SMabc'
        (url,), kwargs = post.call_args
        assert url == 'https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json'
        assert kwargs['auth'] == ('ACtest', 'test-twilio-token')
        assert kwargs['data'] == {'To': '+15005550001', 'Body': 'Hi there',
                                  'From': '+15005550006'}

    def test_a_messaging_service_replaces_the_from_number_and_sets_the_callback(
            self, fake_credentials, settings):
        settings.TWILIO_MESSAGING_SERVICE_SID = 'MGtest'
        settings.NOTIFY_WEBHOOK_BASE_URL = 'https://api.example.test/'
        with mock.patch('servers.notifications.providers.twilio.requests.post',
                        return_value=reply(201, {'sid': 'SMabc'})) as post:
            self.adapter().send(sms_outbound())
        data = post.call_args.kwargs['data']
        assert data['MessagingServiceSid'] == 'MGtest' and 'From' not in data
        assert data['StatusCallback'] == 'https://api.example.test/api/v1/notifications/webhooks/twilio/'

    def test_errors_are_classified(self, fake_credentials):
        adapter = self.adapter()
        for status, body, error in (
                (503, {}, TransientProviderError), (429, {}, TransientProviderError),
                (400, {'code': 21211, 'message': 'invalid To'}, PermanentProviderError),
                (400, {'code': 21610, 'message': 'unsubscribed'}, PermanentProviderError)):
            with mock.patch('servers.notifications.providers.twilio.requests.post',
                            return_value=reply(status, body)):
                with pytest.raises(error):
                    adapter.send(sms_outbound())

    def test_the_error_text_never_carries_the_number(self, fake_credentials):
        with mock.patch('servers.notifications.providers.twilio.requests.post',
                        return_value=reply(400, {'code': 21211,
                                                 'message': 'The number +919876543210 is invalid'})):
            with pytest.raises(PermanentProviderError) as exc:
                self.adapter().send(sms_outbound())
        assert '9876543210' not in str(exc.value)

    def test_it_refuses_to_construct_without_credentials(self, settings):
        settings.TWILIO_ACCOUNT_SID = ''
        with pytest.raises(PermanentProviderError):
            self.adapter()


class TestSes:
    def adapter(self, fake_credentials):
        from servers.notifications.providers.ses import SesEmail
        client = mock.Mock()
        with mock.patch('boto3.client', return_value=client) as factory:
            adapter = SesEmail()
        return adapter, client, factory

    def test_builds_the_sesv2_request_from_the_ses_credentials_only(self, fake_credentials):
        adapter, client, factory = self.adapter(fake_credentials)
        factory.assert_called_once_with(
            'sesv2', region_name='us-east-1', aws_access_key_id='test-ses-id',
            aws_secret_access_key='test-ses-secret')
        client.send_email.return_value = {'MessageId': 'ses-1'}
        outbound = sms_outbound(channel='email', to_email='who@example.test', subject='Subj',
                                body='Body text')
        assert adapter.send(outbound).provider_message_id == 'ses-1'
        kwargs = client.send_email.call_args.kwargs
        assert kwargs['FromEmailAddress'] == 'noreply@example.test'
        assert kwargs['Destination'] == {'ToAddresses': ['who@example.test']}
        simple = kwargs['Content']['Simple']
        assert simple['Subject']['Data'] == 'Subj' and simple['Body']['Text']['Data'] == 'Body text'
        assert 'ConfigurationSetName' not in kwargs

    def test_a_configuration_set_is_passed_when_set(self, fake_credentials, settings):
        settings.SES_CONFIGURATION_SET = 'test-set'
        adapter, client, _ = self.adapter(fake_credentials)
        client.send_email.return_value = {'MessageId': 'ses-2'}
        adapter.send(sms_outbound(channel='email', to_email='a@example.test', subject='s'))
        assert client.send_email.call_args.kwargs['ConfigurationSetName'] == 'test-set'

    def test_sandbox_rejection_is_permanent_and_throttling_is_transient(self, fake_credentials):
        from botocore.exceptions import ClientError
        adapter, client, _ = self.adapter(fake_credentials)

        def error(code):
            return ClientError({'Error': {'Code': code, 'Message': 'x'}}, 'SendEmail')

        client.send_email.side_effect = error('MessageRejected')   # "Email address is not verified"
        with pytest.raises(PermanentProviderError):
            adapter.send(sms_outbound(channel='email', to_email='a@example.test'))
        client.send_email.side_effect = error('Throttling')
        with pytest.raises(TransientProviderError):
            adapter.send(sms_outbound(channel='email', to_email='a@example.test'))

    def test_it_refuses_to_construct_without_its_own_credentials(self, settings):
        from servers.notifications.providers.ses import SesEmail
        settings.SES_AWS_ACCESS_KEY_ID = ''
        with mock.patch('boto3.client') as factory, pytest.raises(PermanentProviderError):
            SesEmail()
        factory.assert_not_called()


class TestFcm:
    def outbound(self):
        return sms_outbound(channel='push', push_token='device-token', title='T', body='B',
                            data={'variable_names': ['x'], 'deep_link': 'trip/1'})

    def test_it_refuses_to_construct_without_a_firebase_app(self, fake_credentials):
        from servers.notifications.providers.fcm import FcmPush
        with mock.patch('firebase_admin._apps', {}), pytest.raises(PermanentProviderError):
            FcmPush()

    def test_sends_a_notification_with_data_through_the_sdk(self, fake_credentials):
        from firebase_admin import messaging

        from servers.notifications.providers.fcm import FcmPush
        with mock.patch('firebase_admin._apps', {'[DEFAULT]': object()}):
            adapter = FcmPush()
        with mock.patch('firebase_admin.messaging.send', return_value='projects/x/messages/1') as send:
            result = adapter.send(self.outbound())
        assert result.provider_message_id == 'projects/x/messages/1'
        message = send.call_args.args[0]
        assert isinstance(message, messaging.Message) and message.token == 'device-token'
        assert message.notification.title == 'T' and message.notification.body == 'B'
        assert message.data['deep_link'] == 'trip/1' and 'variable_names' not in message.data
        assert message.data['category'] == 'transactional'

    def test_errors_are_classified(self, fake_credentials):
        from firebase_admin import exceptions, messaging

        from servers.notifications.providers.fcm import FcmPush
        with mock.patch('firebase_admin._apps', {'[DEFAULT]': object()}):
            adapter = FcmPush()
        cases = ((exceptions.UnavailableError('down'), TransientProviderError),
                 (exceptions.InternalError('oops'), TransientProviderError),
                 (messaging.UnregisteredError('gone'), PermanentProviderError),
                 (exceptions.InvalidArgumentError('bad'), PermanentProviderError))
        for raised, expected in cases:
            with mock.patch('firebase_admin.messaging.send', side_effect=raised):
                with pytest.raises(expected):
                    adapter.send(self.outbound())

    def test_no_token_is_permanent(self, fake_credentials):
        from servers.notifications.providers.fcm import FcmPush
        with mock.patch('firebase_admin._apps', {'[DEFAULT]': object()}):
            adapter = FcmPush()
        with pytest.raises(PermanentProviderError):
            adapter.send(sms_outbound(channel='push', push_token=''))


class TestInApp:
    @pytest.mark.django_db
    def test_writes_the_row_the_apps_already_list(self):
        from servers.notifications.providers.internal import InAppProvider
        from servers.rider.models import Notification
        from notifications_support import make_user

        user = make_user('driver')
        result = InAppProvider().send(sms_outbound(
            channel='in_app', user_id=user.pk, title='Title', body='Body'))
        row = Notification.objects.get(user_id=user)
        assert (row.title, row.message, row.notif_type) == ('Title', 'Body', 'system')
        assert result.provider_message_id == f'inapp-{row.pk}' and result.status == 'delivered'
