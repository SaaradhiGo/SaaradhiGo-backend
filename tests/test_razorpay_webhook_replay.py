"""Razorpay / RazorpayX webhooks: replays of a captured signed body are refused (AUDIT 27.76).

The HMAC covers the raw body only, never the headers. So the inbox must be keyed on the signed
body, not on X-Razorpay-Event-Id (a replayer can set any header), and an event older than
Razorpay's retry window must not be processed at all. Both endpoints share one code path, so
every test runs against both.
"""

import json
import time
from unittest import mock

import pytest
from django.http import JsonResponse

import razorpay_support as rz
from servers.payments.models import WebhookEvent
from test_payment_gateway_contract import RazorpayHarness
from test_payout_provider_contract import RazorpayXPayoutHarness

pytestmark = pytest.mark.django_db

ENDPOINTS = {
    'razorpay': ('/api/v1/webhooks/razorpay/', rz.WEBHOOK_SECRET, RazorpayHarness,
                 '_handle_payment_event', 'payment.failed', 'payment'),
    'razorpayx': ('/api/v1/webhooks/razorpayx/', rz.X_WEBHOOK_SECRET, RazorpayXPayoutHarness,
                  '_handle_payout_event', 'payout.failed', 'payout'),
}


class Endpoint:
    def __init__(self, provider, client, handler):
        self.provider = provider
        self.path, self.secret, _, _, self.event, self.entity = ENDPOINTS[provider]
        self.client = client
        self.handler = handler

    def body(self, entity_id='x_000000000000001', created_at=None):
        return rz.webhook_body(self.event, created_at=created_at,
                               **{self.entity: {'id': entity_id, 'reference_id': 'ref_1'}})

    def post(self, body, event_id=None):
        headers = {'HTTP_X_RAZORPAY_SIGNATURE': rz.sign(body, self.secret)}
        if event_id:
            headers['HTTP_X_RAZORPAY_EVENT_ID'] = event_id
        return self.client.post(self.path, data=body, content_type='application/json', **headers)

    def rows(self):
        return WebhookEvent.objects.filter(gateway=self.provider)


@pytest.fixture(params=sorted(ENDPOINTS))
def endpoint(request, settings, monkeypatch, api_client):
    provider = request.param
    _, _, harness, handler_name, _, _ = ENDPOINTS[provider]
    harness().prepare(settings, monkeypatch)
    handler = mock.Mock(return_value=JsonResponse({'status': 'ok'}, status=200))
    monkeypatch.setattr(f'servers.payments.razorpay_webhooks.{handler_name}', handler)
    return Endpoint(provider, api_client, handler)


class TestSignedBodyDedupe:
    def test_the_same_signed_body_under_a_new_event_id_header_is_deduplicated(self, endpoint):
        body = endpoint.body()
        assert endpoint.post(body, 'evt_original').json() == {'status': 'ok'}
        replay = endpoint.post(body, 'evt_forged_new_header')
        assert replay.status_code == 200 and replay.json()['status'] == 'already_processed'
        assert endpoint.handler.call_count == 1
        assert endpoint.rows().count() == 1

    def test_the_inbox_key_comes_from_the_body_not_the_header(self, endpoint):
        endpoint.post(endpoint.body(), 'evt_header_value')
        key = endpoint.rows().get().dedupe_key
        assert key.startswith('body:') and 'evt_header_value' not in key

    def test_distinct_fresh_events_are_each_processed(self, endpoint):
        assert endpoint.post(endpoint.body('x_000000000000001'), 'evt_1').status_code == 200
        assert endpoint.post(endpoint.body('x_000000000000002'), 'evt_2').status_code == 200
        assert endpoint.handler.call_count == 2
        assert endpoint.rows().count() == 2

    def test_a_row_keyed_by_the_old_header_scheme_still_dedupes_its_retry(self, endpoint):
        """Rows written before this change were keyed by the event id header."""
        WebhookEvent.objects.create(gateway=endpoint.provider, dedupe_key='evt_legacy',
                                    event_type=endpoint.event, raw_payload={})
        resp = endpoint.post(endpoint.body(), 'evt_legacy')
        assert resp.json()['status'] == 'already_processed'
        assert endpoint.handler.call_count == 0


class TestAgeWindow:
    def test_the_default_window_is_razorpays_24_hour_retry_window_plus_5_minutes(self):
        from django.conf import settings
        assert settings.RAZORPAY_WEBHOOK_MAX_AGE_SECONDS == 24 * 3600 + 300

    def test_a_stale_event_is_acknowledged_and_does_nothing(self, endpoint, settings):
        old = int(time.time()) - settings.RAZORPAY_WEBHOOK_MAX_AGE_SECONDS - 60
        resp = endpoint.post(endpoint.body(created_at=old), 'evt_stale')
        # 200 so Razorpay (which treats any non-2xx as a failure to retry) stops here.
        assert resp.status_code == 200
        assert resp.json() == {'status': 'ignored', 'reason': 'stale event'}
        assert endpoint.handler.call_count == 0
        assert endpoint.rows().count() == 0

    def test_an_event_inside_the_window_is_processed(self, endpoint, settings):
        recent = int(time.time()) - settings.RAZORPAY_WEBHOOK_MAX_AGE_SECONDS + 120
        assert endpoint.post(endpoint.body(created_at=recent), 'evt_recent').json() == {
            'status': 'ok'}
        assert endpoint.handler.call_count == 1

    def test_the_window_is_configurable(self, endpoint, settings):
        settings.RAZORPAY_WEBHOOK_MAX_AGE_SECONDS = 60
        resp = endpoint.post(endpoint.body(created_at=int(time.time()) - 120), 'evt_2min')
        assert resp.json()['status'] == 'ignored'
        assert endpoint.handler.call_count == 0

    @pytest.mark.parametrize('created_at', ['missing', 'yesterday', True, None])
    def test_a_body_without_a_usable_created_at_is_refused(self, endpoint, created_at):
        body = rz.webhook_body(endpoint.event, **{endpoint.entity: {'id': 'x_1'}})
        payload = json.loads(body)
        if created_at == 'missing':
            payload.pop('created_at')
        else:
            payload['created_at'] = created_at
        body = json.dumps(payload).encode()
        resp = endpoint.post(body, 'evt_no_time')
        assert resp.status_code == 400
        assert endpoint.handler.call_count == 0
        assert endpoint.rows().count() == 0
