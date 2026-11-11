"""The integrations status page (module 15): read-only, presence only, no secret."""

import json

import pytest
from django.utils import timezone

from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from platform_support import BASE, data

pytestmark = pytest.mark.django_db
URL = f'{BASE}/integrations/'

#: Every credential setting the module can name, planted with a distinctive value.
PLANTED = {
    'CASHFREE_APP_ID': 'zzzzzzzzzzzz-1', 'CASHFREE_SECRET_KEY': 'zzzzzzzzzzzz-2',
    'CASHFREE_WEBHOOK_SECRET': 'zzzzzzzzzzzz-3', 'MSG91_AUTH_KEY': 'zzzzzzzzzzzz-4',
    'TWILIO_ACCOUNT_SID': 'zzzzzzzzzzzz-5', 'TWILIO_AUTH_TOKEN': 'zzzzzzzzzzzz-6',
    'SES_AWS_ACCESS_KEY_ID': 'zzzzzzzzzzzz-18', 'SES_AWS_SECRET_ACCESS_KEY': 'zzzzzzzzzzzz-7',
    'SES_AWS_REGION': 'zzzzzzzzzzzz-8', 'SES_FROM_EMAIL': 'zzzzzzzzzzzz-9',
    'FIREBASE_CREDENTIALS_JSON': 'zzzzzzzzzzzz-10',
    'AWS_ACCESS_KEY_ID': 'zzzzzzzzzzzz-19', 'AWS_SECRET_ACCESS_KEY': 'zzzzzzzzzzzz-11',
    'AWS_REGION': 'zzzzzzzzzzzz-12', 'AWS_SNS_SENDER_ID': 'zzzzzzzzzzzz-20',
    'GOOGLE_MAPS_API_KEY': 'zzzzzzzzzzzz-13', 'AWS_S3_BUCKET_NAME': 'zzzzzzzzzzzz-14',
    'SENTRY_DSN': 'zzzzzzzzzzzz-15',
    'AWS_S3_ENDPOINT_URL': 'zzzzzzzzzzzz-16',
}


def rows(client):
    return {(r['type'], r['country']): r for r in data(client.get(URL))['rows']}


class TestNoSecretEverLeaves:
    def test_no_planted_value_appears_anywhere_in_the_response(self, geo, settings):
        for name, value in PLANTED.items():
            setattr(settings, name, value)
        settings.NOTIFY_LIVE_SEND = True
        client, _ = operator('super_admin', geo)

        text = client.get(URL).content.decode()

        for name, value in PLANTED.items():
            assert value not in text, name
        assert 'zzzzzzzzzzzz' not in text
        # Names of variables are fine (they are in the repository); values are not.
        assert 'CASHFREE_SECRET_KEY' in text

    def test_the_response_has_only_booleans_modes_times_and_names(self, geo, settings):
        for name, value in PLANTED.items():
            setattr(settings, name, value)
        client, _ = operator('super_admin', geo)
        for row in data(client.get(URL))['rows']:
            assert set(row) == {'type', 'label', 'provider', 'country', 'configured', 'mode',
                                'env_vars', 'blocker', 'note', 'last_success_at',
                                'last_error_at'}
            assert isinstance(row['configured'], bool)
            assert row['mode'] in ('live', 'sandbox', 'stubbed', 'off')


class TestRows:
    def test_payments_per_country_us_waits_on_blocker_17(self, geo, settings):
        settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY = 'a-test', 'b-test'
        settings.CASHFREE_ENVIRONMENT = 'sandbox'
        client, _ = operator('super_admin', geo)
        got = rows(client)
        assert got[('payment_gateway', 'IN')]['configured'] is True
        assert got[('payment_gateway', 'IN')]['mode'] == 'sandbox'
        us = got[('payment_gateway', 'US')]
        assert (us['configured'], us['mode'], us['blocker']) == (False, 'off', 17)
        assert 'not configured, blocker #17' in us['note']
        settings.CASHFREE_ENVIRONMENT = 'production'
        assert rows(client)[('payment_gateway', 'IN')]['mode'] == 'live'

    def test_sms_is_stubbed_until_live_sending_is_on(self, geo, settings):
        settings.NOTIFY_LIVE_SEND = False
        settings.MSG91_AUTH_KEY = 'k-test'
        client, _ = operator('super_admin', geo)
        sms = rows(client)[('sms', 'IN')]
        assert sms['provider'] == 'msg91' and sms['mode'] == 'stubbed' and sms['blocker'] == 18
        assert 'NOTIFY_LIVE_SEND' in sms['note']
        settings.NOTIFY_LIVE_SEND = True
        assert rows(client)[('sms', 'IN')]['mode'] == 'live'
        assert rows(client)[('sms', 'US')]['blocker'] == 19

    def test_every_provider_type_is_present(self, geo):
        client, _ = operator('super_admin', geo)
        types = {r['type'] for r in data(client.get(URL))['rows']}
        assert types >= {'payment_gateway', 'sms', 'whatsapp', 'email', 'push', 'otp_sms', 'maps',
                         'storage', 'error_tracking'}

    def test_last_success_and_error_come_from_recorded_messages(self, geo, settings):
        from servers.notifications.models import FAILED, SENT, NotificationMessage

        now = timezone.now()
        base = dict(template_key='t', channel='sms', recipient_user_id=1, provider='msg91',
                    country='IN')
        NotificationMessage.objects.create(idempotency_key='ok-1', status=SENT, sent_at=now,
                                           **base)
        NotificationMessage.objects.create(idempotency_key='bad-1', status=FAILED, **base)
        client, _ = operator('super_admin', geo)
        sms = rows(client)[('sms', 'IN')]
        assert sms['last_success_at'] and sms['last_error_at']
        assert rows(client)[('sms', 'US')]['last_success_at'] is None

    def test_the_page_is_read_only(self, geo):
        client, _ = operator('super_admin', geo)
        for method in (client.post, client.put, client.delete, client.patch):
            assert method(URL, {}, format='json').status_code == 405


class TestWhoMayRead:
    def test_super_admin_only(self, geo):
        assert operator('super_admin', geo)[0].get(URL).status_code == 200
        for role in ('city_ops_manager', 'finance', 'marketing', 'analyst', 'support_agent',
                     'support_lead', 'safety_officer', 'kyc_verifier'):
            assert operator(role, geo)[0].get(URL).status_code == 403, role


def test_the_response_is_json_serialisable_text(geo):
    client, _ = operator('super_admin', geo)
    json.loads(client.get(URL).content)
