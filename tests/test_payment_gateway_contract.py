"""PaymentGateway contract suite, parametrized over every IMPLEMENTED payments adapter.

HONEST STATUS: before this file there was NO formal gateway contract suite. The Cashfree adapter
was covered by scattered webhook / reconciliation tests, and nothing stated what "a payment
gateway" must do. This suite states it once and runs it against Cashfree and Razorpay:

  create_order shape, a failed order is None, a timeout is None, order status mapping, signature
  verification true / false, refund full and partial, an unconfigured provider is falsy and sends
  nothing, and the webhook endpoint contract (valid settles, invalid is a 400 with no side effect,
  a duplicate delivery is idempotent).

The HTTP is recorded-fixture only (`tests/razorpay_support.py` for Razorpay; a small fake of the
Cashfree REST calls below); no network, no real credentials, every id a low-entropy placeholder.
Razorpay-specific rules (paise payloads, capture, duplicate-receipt refunds, the key guard, the
verify endpoint, the create_order response) follow the parametrized part. The LIVE test-mode
tests at the bottom are skipped unless RAZORPAY_KEY_ID starts with rzp_test_ (never in CI).
"""

import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

import pytest
import requests
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

import razorpay_support as rz
from servers.payments.models import Payment, TransactionHistory, WebhookEvent
from servers.payments.payment_gateways.factory import get_payment_gateway

pytestmark = pytest.mark.django_db
AMOUNT_MINOR = 19500  # Rs 195.00


# ---------------------------------------------------------------------------
# One harness per implemented adapter
# ---------------------------------------------------------------------------

class CashfreeHarness:
    name = 'cashfree'
    webhook_path = '/api/v1/payments/webhook/'
    secret = 'fake-cashfree-secret'

    def prepare(self, settings, monkeypatch):
        settings.PAYMENT_PROVIDERS = {'IN': 'cashfree', 'US': None}
        settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY = 'app-id-test', self.secret
        settings.CASHFREE_WEBHOOK_SECRET = self.secret
        settings.BACKEND_URL = 'https://backend.example.test'
        self.orders, self.paid, self.timeout = {}, set(), False
        self.created_bodies = []

        def post(url, headers=None, json=None, timeout=None, **kw):
            assert timeout, 'every call must carry a timeout'
            if self.timeout:
                raise requests.exceptions.Timeout('slow')
            if url.endswith('/orders'):
                self.created_bodies.append(json)
                if self.reject_orders:
                    return self._resp(400, {'message': 'bad'})
                self.orders[json['order_id']] = json
                return self._resp(200, {'payment_session_id': 'session_fake_1', 'cf_order_id': 1})
            return self._resp(404, {})

        def get(url, headers=None, timeout=None, **kw):
            assert timeout
            oid = url.rsplit('/', 1)[1]
            order = self.orders.get(oid)
            if not order:
                return self._resp(404, {})
            return self._resp(200, {
                'order_status': 'PAID' if oid in self.paid else 'ACTIVE',
                'order_amount': order['order_amount'], 'order_currency': 'INR'})

        self.reject_orders = False
        monkeypatch.setattr(requests, 'post', post)
        monkeypatch.setattr(requests, 'get', get)

    @staticmethod
    def _resp(status, body):
        def raise_for_status():
            if status >= 400:
                raise requests.exceptions.HTTPError(f'{status}', response=resp)
        resp = SimpleNamespace(status_code=status, json=lambda: body, text=str(body),
                               content=b'{}', raise_for_status=raise_for_status)
        return resp

    def gateway(self):
        gateway = get_payment_gateway(self.name)
        self.refund_calls = []

        def refund(version, order_id, request):
            self.refund_calls.append((order_id, request.refund_amount))
            return SimpleNamespace(data=SimpleNamespace(status='PENDING', cf_refund_id=7))

        gateway.pg_client = SimpleNamespace(PGOrderCreateRefund=refund)
        return gateway

    def pay(self, gateway, order_id):
        self.paid.add(order_id)
        return 'cf_payment_1'

    def checkout_proof(self, order_id, payment_id, valid=True):
        """Kwargs for verify_payment_signature: Cashfree confirms through the order status."""
        if valid:
            self.paid.add(order_id)
        return {'order_id': order_id}

    def webhook_request(self, order_id, payment_id, event_id):
        body = json.dumps({'type': 'PAYMENT_SUCCESS_WEBHOOK', 'data': {
            'order': {'order_id': order_id}, 'payment': {'cf_payment_id': payment_id}}}).encode()
        ts = str(int(time.time()))
        sig = base64.b64encode(hmac.new(self.secret.encode(), (ts + body.decode()).encode(),
                                        hashlib.sha256).digest()).decode()
        return body, {'HTTP_X_WEBHOOK_SIGNATURE': sig, 'HTTP_X_WEBHOOK_TIMESTAMP': ts}

    def forged_request(self, order_id, payment_id, event_id):
        body, headers = self.webhook_request(order_id, payment_id, event_id)
        headers['HTTP_X_WEBHOOK_SIGNATURE'] = 'forged'
        return body, headers

    def refund_amount_seen(self):
        return self.refund_calls[-1][1]


class RazorpayHarness:
    name = 'razorpay'
    webhook_path = '/api/v1/webhooks/razorpay/'

    def prepare(self, settings, monkeypatch):
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        settings.RAZORPAY_WEBHOOK_SECRET = rz.WEBHOOK_SECRET
        settings.IS_PRODUCTION = False
        self.server = rz.FakeRazorpay().install(monkeypatch)
        self.timeout = False
        self.reject_orders = False
        self._real_request = self.server._request

        def request(*a, **kw):
            if self.timeout:
                raise requests.exceptions.Timeout('slow')
            if self.reject_orders and a[0] == 'POST' and a[1].endswith('/orders'):
                self.server.calls.append({'method': 'POST', 'path': '/orders', 'json': kw.get('json')})
                return rz._Response(400, {'error': {'code': 'BAD_REQUEST_ERROR', 'description': 'bad'}})
            return self._real_request(*a, **kw)

        monkeypatch.setattr('integrations.razorpay_http.requests.request', request)

    @property
    def orders(self):
        return self.server.orders

    @property
    def created_bodies(self):
        return [c['json'] for c in self.server.calls_to('POST', '/orders')]

    def gateway(self):
        return get_payment_gateway(self.name)

    def pay(self, gateway, order_id):
        return self.server.add_payment(order_id, 'captured')['id']

    def checkout_proof(self, order_id, payment_id, valid=True):
        sig = rz.sign(f'{order_id}|{payment_id}'.encode(), rz.KEY_SECRET)
        if not valid:
            sig = sig[:-1] + ('0' if sig[-1] != '0' else '1')
        return {'order_id': order_id, 'payment_id': payment_id, 'signature': sig}

    def webhook_request(self, order_id, payment_id, event_id):
        pay = self.server.payments[payment_id]
        body = rz.webhook_body('payment.captured', payment=pay)
        return body, {'HTTP_X_RAZORPAY_SIGNATURE': rz.sign(body, rz.WEBHOOK_SECRET),
                      'HTTP_X_RAZORPAY_EVENT_ID': event_id}

    def forged_request(self, order_id, payment_id, event_id):
        body, headers = self.webhook_request(order_id, payment_id, event_id)
        headers['HTTP_X_RAZORPAY_SIGNATURE'] = rz.sign(body, 'some-other-secret')
        return body, headers

    def refund_amount_seen(self):
        return self.server.calls_to('POST', '/payments/')[-1]['json'].get('amount')


@pytest.fixture(params=[CashfreeHarness, RazorpayHarness], ids=['cashfree', 'razorpay'])
def world(request, settings, monkeypatch):
    harness = request.param()
    harness.prepare(settings, monkeypatch)
    return harness


@pytest.fixture
def paid_trip(db):
    """A completed trip owned by a rider, with a processing ONLINE payment to attach an order to."""
    from servers.driver.models import Driver
    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Rider

    User = get_user_model()
    rider = User.objects.create_user(phone_number='+919990003001', role='rider',
                                     username='+919990003001')
    Rider.objects.create(user_id=rider)
    driver_user = User.objects.create_user(phone_number='+918880003001', role='driver',
                                           username='+918880003001')
    driver = Driver.objects.create(user_id=driver_user)
    completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, pickup_lat=D('17.4450'), pickup_long=D('78.3800'),
        destination_lat=D('17.4650'), destination_long=D('78.4200'),
        pickup_address='Banjara Hills', destination_address='Hitech City',
        estimated_fare=D('195.00'), final_fare=D('195.00'), status_id=completed,
        payment_method='online', payment_status='pending')
    return SimpleNamespace(trip=trip, rider=rider, driver=driver)


def _client(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

class TestPaymentGatewayContract:
    def test_the_provider_is_selectable_and_truthy_when_configured(self, world):
        gateway = world.gateway()
        assert gateway and gateway.get_name() == world.name
        assert tuple(gateway.supported_currencies()) == ('INR',)

    def test_create_order_returns_the_common_shape(self, world):
        order = world.gateway().create_order(AMOUNT_MINOR, trip_id=42, currency='INR',
                                             customer_id='7', customer_phone='+919990000000')
        assert order['gateway'] == world.name
        assert order['order_id'] and order['order_id'] == order['gateway_order_id']
        assert order['order_id'] in world.orders

    def test_create_order_sends_the_amount_in_the_providers_unit(self, world):
        world.gateway().create_order(AMOUNT_MINOR, trip_id=42, currency='INR', customer_id='7')
        body = world.created_bodies[-1]
        if world.name == 'razorpay':
            assert body['amount'] == AMOUNT_MINOR  # paise, natively
        else:
            assert body['order_amount'] == 195.0  # Cashfree takes major units

    def test_a_rejected_order_is_none(self, world):
        world.reject_orders = True
        assert world.gateway().create_order(AMOUNT_MINOR, trip_id=1, currency='INR') is None

    def test_a_timeout_is_none_not_an_exception(self, world):
        gateway = world.gateway()
        world.timeout = True
        assert gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR') is None
        assert gateway.get_order_status('order_x') is None

    def test_a_non_positive_or_non_integer_amount_is_refused(self, world):
        gateway = world.gateway()
        for bad in (0, -5, 10.5, D('195.00'), True):
            assert gateway.create_order(bad, trip_id=1, currency='INR') is None

    def test_order_status_mapping(self, world):
        gateway = world.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR', customer_id='7')
        before = gateway.get_order_status(order['order_id'])
        assert before['order_status'] != 'PAID' and before['payment_status'] != 'SUCCESS'
        world.pay(gateway, order['order_id'])
        after = gateway.get_order_status(order['order_id'])
        assert after['order_status'] == 'PAID' and after['payment_status'] == 'SUCCESS'
        assert D(str(after['order_amount'])) == D('195.00')  # major units, for the shared handlers
        assert after['order_currency'] == 'INR'

    def test_an_unknown_order_has_no_status(self, world):
        assert world.gateway().get_order_status('order_does_not_exist') is None

    def test_signature_verification_true_and_false(self, world):
        gateway = world.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR', customer_id='7')
        payment_id = world.pay(gateway, order['order_id']) if world.name == 'razorpay' else 'cf_p'
        # false first: for Cashfree the order is not yet PAID, for Razorpay the signature is wrong
        bad = world.checkout_proof(order['order_id'], payment_id, valid=False)
        assert gateway.verify_payment_signature(**bad) is False
        good = world.checkout_proof(order['order_id'], payment_id, valid=True)
        assert gateway.verify_payment_signature(**good) is True

    def test_full_refund(self, world):
        gateway = world.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR', customer_id='7')
        payment_id = world.pay(gateway, order['order_id'])
        target = payment_id if world.name == 'razorpay' else order['order_id']
        refund = gateway.create_refund(target, amount_minor=AMOUNT_MINOR, currency='INR')
        assert refund['gateway'] == world.name and refund['refund_id'] and refund['status']

    def test_partial_refund(self, world):
        gateway = world.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR', customer_id='7')
        payment_id = world.pay(gateway, order['order_id'])
        target = payment_id if world.name == 'razorpay' else order['order_id']
        assert gateway.create_refund(target, amount_minor=5000, currency='INR')
        seen = world.refund_amount_seen()
        assert D(str(seen)) in (D(5000), D('50.00'))  # paise for Razorpay, rupees for Cashfree

    def test_a_bad_refund_amount_is_refused(self, world):
        gateway = world.gateway()
        for bad in (0, -1, 12.5):
            assert gateway.create_refund('pay_or_order', amount_minor=bad, currency='INR') is None

    def test_unconfigured_is_falsy_and_makes_no_call(self, world, settings, monkeypatch):
        sent = mock.Mock()
        monkeypatch.setattr(requests, 'post', sent)
        monkeypatch.setattr(requests, 'get', sent)
        monkeypatch.setattr(requests, 'request', sent)
        settings.CASHFREE_APP_ID = settings.CASHFREE_SECRET_KEY = ''
        settings.RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_SECRET = ''
        gateway = get_payment_gateway(world.name)
        assert not gateway
        assert gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR') is None
        assert gateway.create_refund('x', 100, 'INR') is None
        sent.assert_not_called()


# ---------------------------------------------------------------------------
# Webhook endpoint contract
# ---------------------------------------------------------------------------

@pytest.fixture
def settleable(world, paid_trip):
    """A processing Payment whose order exists at the provider and is already PAID there."""
    gateway = world.gateway()
    order = gateway.create_order(AMOUNT_MINOR, trip_id=paid_trip.trip.id, currency='INR',
                                 customer_id=str(paid_trip.rider.id))
    payment = Payment.objects.create(
        trip_id=paid_trip.trip, user_id=paid_trip.rider, amount=D('195.00'), method='online',
        status='processing', payment_gateway=world.name, gateway_order_id=order['order_id'],
        cashfree_order_id=order['order_id'])
    payment_id = world.pay(gateway, order['order_id'])
    return SimpleNamespace(payment=payment, order_id=order['order_id'], payment_id=payment_id,
                           trip=paid_trip.trip)


class TestWebhookEndpointContract:
    def test_a_valid_delivery_settles_the_payment(self, world, settleable, api_client):
        body, headers = world.webhook_request(settleable.order_id, settleable.payment_id, 'evt_1')
        resp = api_client.post(world.webhook_path, data=body, content_type='application/json',
                               **headers)
        assert resp.status_code == 200, resp.content
        settleable.payment.refresh_from_db()
        settleable.trip.refresh_from_db()
        assert settleable.payment.status == 'completed'
        assert settleable.trip.payment_status == 'completed'
        history = TransactionHistory.objects.get(trip_id=settleable.trip,
                                                 gateway_payment_id=settleable.payment_id)
        assert history.payment_gateway == world.name

    def test_an_invalid_signature_is_400_with_no_side_effect(self, world, settleable, api_client):
        body, headers = world.forged_request(settleable.order_id, settleable.payment_id, 'evt_2')
        resp = api_client.post(world.webhook_path, data=body, content_type='application/json',
                               **headers)
        assert resp.status_code == 400
        settleable.payment.refresh_from_db()
        assert settleable.payment.status == 'processing'
        assert WebhookEvent.objects.count() == 0

    def test_an_unsigned_delivery_is_400(self, world, settleable, api_client):
        body, _ = world.webhook_request(settleable.order_id, settleable.payment_id, 'evt_3')
        resp = api_client.post(world.webhook_path, data=body, content_type='application/json')
        assert resp.status_code == 400
        assert WebhookEvent.objects.count() == 0

    def test_a_duplicate_delivery_does_nothing(self, world, settleable, api_client):
        body, headers = world.webhook_request(settleable.order_id, settleable.payment_id, 'evt_4')
        first = api_client.post(world.webhook_path, data=body, content_type='application/json',
                                **headers)
        second = api_client.post(world.webhook_path, data=body, content_type='application/json',
                                 **headers)
        assert first.status_code == 200 and second.status_code == 200
        assert second.json()['status'] == 'already_processed'
        assert WebhookEvent.objects.filter(gateway=world.name).count() == 1
        assert TransactionHistory.objects.filter(trip_id=settleable.trip,
                                                 txn_type='payment').count() == 1


# ---------------------------------------------------------------------------
# Razorpay-specific rules
# ---------------------------------------------------------------------------

@pytest.fixture
def rzp(settings, monkeypatch):
    harness = RazorpayHarness()
    harness.prepare(settings, monkeypatch)
    return harness


class TestRazorpayPayments:
    def test_order_payload_carries_our_reference_and_notes(self, rzp):
        rzp.gateway().create_order(AMOUNT_MINOR, trip_id=42, currency='INR', customer_id='7')
        body = rzp.created_bodies[-1]
        assert body == {'amount': AMOUNT_MINOR, 'currency': 'INR', 'receipt': 'trip_42',
                        'notes': {'trip_id': '42', 'customer_id': '7'}}
        call = rzp.server.calls_to('POST', '/orders')[-1]
        assert call['auth'] == (rz.KEY_ID, rz.KEY_SECRET) and call['timeout']

    def test_the_order_carries_what_checkout_needs(self, rzp):
        order = rzp.gateway().create_order(AMOUNT_MINOR, trip_id=42, currency='INR')
        assert order['key_id'] == rz.KEY_ID and order['amount'] == AMOUNT_MINOR
        assert order['currency'] == 'INR' and order['id'] == order['order_id']
        assert 'secret' not in json.dumps(order).lower()

    def test_a_non_inr_order_is_refused(self, rzp):
        assert rzp.gateway().create_order(1000, trip_id=1, currency='USD') is None

    def test_checkout_signature_is_hmac_of_order_pipe_payment(self, rzp):
        gateway = rzp.gateway()
        expected = hmac.new(rz.KEY_SECRET.encode(), b'order_1|pay_1', hashlib.sha256).hexdigest()
        assert gateway.verify_payment_signature('order_1', 'pay_1', expected) is True
        assert gateway.verify_payment_signature('order_1', 'pay_2', expected) is False
        assert gateway.verify_payment_signature('order_1', 'pay_1', '') is False
        assert gateway.verify_payment_signature('order_1') is False  # no "trust the order id" path

    def test_an_authorized_payment_is_captured_explicitly(self, rzp):
        gateway = rzp.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR')
        pay = rzp.server.add_payment(order['order_id'], 'authorized')
        assert gateway.ensure_captured(pay['id'], AMOUNT_MINOR) == 'captured'
        capture = rzp.server.calls_to('POST', f"/payments/{pay['id']}/capture")[-1]
        assert capture['json'] == {'amount': AMOUNT_MINOR, 'currency': 'INR'}
        assert gateway.get_order_status(order['order_id'])['order_status'] == 'PAID'

    def test_a_captured_payment_is_not_captured_again(self, rzp):
        gateway = rzp.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR')
        pay = rzp.server.add_payment(order['order_id'], 'captured')
        assert gateway.ensure_captured(pay['id'], AMOUNT_MINOR) == 'captured'
        assert rzp.server.calls_to('POST', '/payments/') == []

    def test_refund_payload_and_idempotent_receipt(self, rzp):
        gateway = rzp.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR')
        pay = rzp.server.add_payment(order['order_id'], 'captured')
        first = gateway.create_refund(pay['id'], amount_minor=5000)
        again = gateway.create_refund(pay['id'], amount_minor=5000)  # same reference: no second refund
        assert first['rzp_refund_id'] == again['rzp_refund_id']
        assert len(rzp.server.refunds) == 1
        sent = rzp.server.calls_to('POST', f"/payments/{pay['id']}/refund")[0]['json']
        assert sent['amount'] == 5000 and sent['speed'] == 'optimum'
        assert sent['receipt'] == first['refund_id'] and sent['notes']['reference'] == sent['receipt']

    def test_a_full_refund_omits_the_amount_and_a_different_partial_is_a_new_refund(self, rzp):
        gateway = rzp.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR')
        pay = rzp.server.add_payment(order['order_id'], 'captured')
        gateway.create_refund(pay['id'], amount_minor=1000)
        gateway.create_refund(pay['id'], amount_minor=2000)
        assert len(rzp.server.refunds) == 2
        gateway.create_refund(pay['id'])
        assert 'amount' not in rzp.server.calls_to('POST', f"/payments/{pay['id']}/refund")[-1]['json']

    def test_an_order_id_is_resolved_to_its_captured_payment_for_a_refund(self, rzp):
        gateway = rzp.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR')
        pay = rzp.server.add_payment(order['order_id'], 'captured')
        assert gateway.create_refund(order['order_id'], amount_minor=AMOUNT_MINOR)
        assert rzp.server.refunds and list(rzp.server.refunds.values())[0]['payment_id'] == pay['id']

    def test_a_failed_refund_is_none(self, rzp):
        gateway = rzp.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR')
        pay = rzp.server.add_payment(order['order_id'], 'captured')
        rzp.server.fail('POST', r'/refund$', 400)
        assert gateway.create_refund(pay['id'], amount_minor=100) is None

    def test_webhook_signature_is_hmac_of_the_raw_body(self, rzp):
        gateway = rzp.gateway()
        body = b'{"event":"payment.captured"}'
        good = hmac.new(rz.WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert gateway.verify_webhook_signature(body, good) is True
        assert gateway.verify_webhook_signature(body + b' ', good) is False
        assert gateway.verify_webhook_signature(body, '') is False
        assert gateway.verify_webhook_signature(body, None) is False

    def test_webhook_fails_closed_without_a_secret(self, rzp, settings):
        settings.RAZORPAY_WEBHOOK_SECRET = ''
        body = b'{}'
        sig = rz.sign(body, '')
        assert rzp.gateway().verify_webhook_signature(body, sig) is False

    def test_the_live_key_is_refused_outside_production(self, rzp, settings, monkeypatch):
        settings.RAZORPAY_KEY_ID = 'rzp_live_fakefakefake'
        settings.IS_PRODUCTION = False
        gateway = get_payment_gateway('razorpay')
        assert not gateway
        assert 'rzp_live_' in gateway.reason and 'fakefake' not in gateway.reason
        before = len(rzp.server.calls)
        assert gateway.create_order(AMOUNT_MINOR, trip_id=1, currency='INR') is None
        assert len(rzp.server.calls) == before  # nothing was sent
        settings.IS_PRODUCTION = True
        assert get_payment_gateway('razorpay')

    def test_a_key_without_a_known_prefix_is_refused(self, rzp, settings):
        settings.RAZORPAY_KEY_ID = 'not-a-razorpay-key'
        assert not get_payment_gateway('razorpay')


# ---------------------------------------------------------------------------
# Razorpay through the endpoints
# ---------------------------------------------------------------------------

class TestRazorpayEndpoints:
    def test_create_order_response_has_what_checkout_needs(self, rzp, paid_trip):
        resp = _client(paid_trip.rider).post('/api/v1/payments/create-order/',
                                             {'trip_id': paid_trip.trip.id}, format='json')
        assert resp.status_code == 201, resp.content
        data = resp.json()['data']
        assert data['gateway'] == 'razorpay'
        assert data['order_id'] == data['gateway_order_id'] and data['order_id'].startswith('order_')
        assert data['key_id'] == rz.KEY_ID
        assert data['amount_paise'] == AMOUNT_MINOR and data['currency'] == 'INR'
        assert 'cashfree_payment_session_id' in data  # the existing keys are untouched
        assert rz.KEY_SECRET not in json.dumps(data)
        payment = Payment.objects.get(trip_id=paid_trip.trip)
        assert payment.payment_gateway == 'razorpay' and payment.gateway_order_id == data['order_id']

    def test_cashfree_response_shape_is_unchanged(self, settings, monkeypatch, paid_trip):
        harness = CashfreeHarness()
        harness.prepare(settings, monkeypatch)
        resp = _client(paid_trip.rider).post('/api/v1/payments/create-order/',
                                             {'trip_id': paid_trip.trip.id}, format='json')
        assert resp.status_code == 201, resp.content
        data = resp.json()['data']
        assert data['gateway'] == 'cashfree'
        assert 'key_id' not in data and 'order_id' not in data
        assert data['cashfree_payment_session_id'] == 'session_fake_1'

    def _order(self, rzp, paid_trip):
        resp = _client(paid_trip.rider).post('/api/v1/payments/create-order/',
                                             {'trip_id': paid_trip.trip.id}, format='json')
        return resp.json()['data']['order_id']

    def test_verify_accepts_the_razorpay_fields_and_settles(self, rzp, paid_trip):
        order_id = self._order(rzp, paid_trip)
        pay = rzp.server.add_payment(order_id, 'captured')
        proof = rzp.checkout_proof(order_id, pay['id'])
        resp = _client(paid_trip.rider).post('/api/v1/payments/verify/', {
            'razorpay_order_id': order_id, 'razorpay_payment_id': pay['id'],
            'razorpay_signature': proof['signature']}, format='json')
        assert resp.status_code == 200, resp.content
        payment = Payment.objects.get(trip_id=paid_trip.trip)
        assert payment.status == 'completed' and payment.gateway_payment_id == pay['id']
        assert payment.gateway_signature == proof['signature']

    def test_verify_with_a_bad_signature_is_400_and_changes_nothing(self, rzp, paid_trip):
        order_id = self._order(rzp, paid_trip)
        pay = rzp.server.add_payment(order_id, 'captured')
        resp = _client(paid_trip.rider).post('/api/v1/payments/verify/', {
            'razorpay_order_id': order_id, 'razorpay_payment_id': pay['id'],
            'razorpay_signature': 'bad'}, format='json')
        assert resp.status_code == 400 and resp.json()['error']['code'] == 'SIGNATURE_INVALID'
        assert Payment.objects.get(trip_id=paid_trip.trip).status == 'processing'

    def test_verify_without_the_payment_id_is_400(self, rzp, paid_trip):
        order_id = self._order(rzp, paid_trip)
        resp = _client(paid_trip.rider).post('/api/v1/payments/verify/', {
            'razorpay_order_id': order_id}, format='json')
        assert resp.status_code == 400 and resp.json()['error']['code'] == 'MISSING_FIELDS'

    def test_verify_captures_an_authorized_payment_first(self, rzp, paid_trip):
        order_id = self._order(rzp, paid_trip)
        pay = rzp.server.add_payment(order_id, 'authorized')
        proof = rzp.checkout_proof(order_id, pay['id'])
        resp = _client(paid_trip.rider).post('/api/v1/payments/verify/', {
            'razorpay_order_id': order_id, 'razorpay_payment_id': pay['id'],
            'razorpay_signature': proof['signature']}, format='json')
        assert resp.status_code == 200
        assert rzp.server.payments[pay['id']]['status'] == 'captured'

    def test_the_cashfree_verify_shape_still_works(self, settings, monkeypatch, paid_trip):
        harness = CashfreeHarness()
        harness.prepare(settings, monkeypatch)
        client = _client(paid_trip.rider)
        order_id = client.post('/api/v1/payments/create-order/', {'trip_id': paid_trip.trip.id},
                               format='json').json()['data']['gateway_order_id']
        harness.paid.add(order_id)
        resp = client.post('/api/v1/payments/verify/', {'gateway_order_id': order_id},
                           format='json')
        assert resp.status_code == 200, resp.content
        assert Payment.objects.get(trip_id=paid_trip.trip).status == 'completed'

    def test_a_cashfree_payment_cannot_be_verified_as_razorpay(self, settings, monkeypatch,
                                                                paid_trip):
        """The payment row, not the request, says which provider took the order."""
        harness = CashfreeHarness()
        harness.prepare(settings, monkeypatch)
        client = _client(paid_trip.rider)
        order_id = client.post('/api/v1/payments/create-order/', {'trip_id': paid_trip.trip.id},
                               format='json').json()['data']['gateway_order_id']
        resp = client.post('/api/v1/payments/verify/', {
            'gateway_order_id': order_id, 'gateway': 'razorpay',
            'razorpay_payment_id': 'pay_x', 'razorpay_signature': 'x'}, format='json')
        assert resp.status_code == 400  # Cashfree's order is not PAID; Razorpay was not consulted

    def test_refund_endpoint_uses_the_razorpay_payment_id(self, rzp, paid_trip):
        from servers.ride.models import TripStatus

        order_id = self._order(rzp, paid_trip)
        pay = rzp.server.add_payment(order_id, 'captured')
        Payment.objects.filter(trip_id=paid_trip.trip).update(
            status='completed', gateway_payment_id=pay['id'])
        cancelled, _ = TripStatus.objects.get_or_create(status_code='cancelled')
        paid_trip.trip.status_id = cancelled
        paid_trip.trip.save()
        resp = _client(paid_trip.rider).post('/api/v1/payments/refund/', {
            'trip_id': paid_trip.trip.id}, format='json')
        assert resp.status_code in (200, 201), resp.content
        refund = list(rzp.server.refunds.values())[0]
        assert refund['payment_id'] == pay['id'] and refund['amount'] == AMOUNT_MINOR

    def test_an_old_cashfree_row_refunds_through_cashfree_and_fails_cleanly(
            self, rzp, paid_trip, settings, monkeypatch):
        """Cashfree is configured nowhere: its rows still route to it, which answers 'not
        configured' (503). No Razorpay call is made and nothing crashes."""
        from servers.ride.models import TripStatus

        settings.CASHFREE_APP_ID = settings.CASHFREE_SECRET_KEY = ''
        Payment.objects.create(
            trip_id=paid_trip.trip, user_id=paid_trip.rider, amount=D('195.00'), method='online',
            status='completed', payment_gateway='cashfree', gateway_order_id='cf_order_1',
            cashfree_order_id='cf_order_1')
        cancelled, _ = TripStatus.objects.get_or_create(status_code='cancelled')
        paid_trip.trip.status_id = cancelled
        paid_trip.trip.save()
        resp = _client(paid_trip.rider).post('/api/v1/payments/refund/', {
            'trip_id': paid_trip.trip.id}, format='json')
        assert resp.status_code == 503
        assert resp.json()['error']['code'] == 'GATEWAY_UNAVAILABLE'
        assert rzp.server.calls == []


class TestRazorpayWebhookEvents:
    def _post(self, client, body, event_id, secret=rz.WEBHOOK_SECRET):
        return client.post('/api/v1/webhooks/razorpay/', data=body, content_type='application/json',
                           HTTP_X_RAZORPAY_SIGNATURE=rz.sign(body, secret),
                           HTTP_X_RAZORPAY_EVENT_ID=event_id)

    def _processing(self, rzp, paid_trip):
        gateway = rzp.gateway()
        order = gateway.create_order(AMOUNT_MINOR, trip_id=paid_trip.trip.id, currency='INR')
        payment = Payment.objects.create(
            trip_id=paid_trip.trip, user_id=paid_trip.rider, amount=D('195.00'), method='online',
            status='processing', payment_gateway='razorpay', gateway_order_id=order['order_id'],
            cashfree_order_id=order['order_id'])
        return order['order_id'], payment

    def test_order_paid_settles(self, rzp, paid_trip, api_client):
        order_id, payment = self._processing(rzp, paid_trip)
        pay = rzp.server.add_payment(order_id, 'captured')
        body = rz.webhook_body('order.paid', payment=pay, order=rzp.server.orders[order_id])
        assert self._post(api_client, body, 'evt_a').status_code == 200
        payment.refresh_from_db()
        assert payment.status == 'completed'

    def test_a_forged_paid_event_does_not_settle_an_unpaid_order(self, rzp, paid_trip, api_client):
        """A signed body alone is not proof: the order is re-read from Razorpay first."""
        order_id, payment = self._processing(rzp, paid_trip)
        pay = {'id': 'pay_000000000000099', 'order_id': order_id, 'status': 'captured'}
        body = rz.webhook_body('payment.captured', payment=pay)
        resp = self._post(api_client, body, 'evt_b')
        assert resp.status_code == 200 and resp.json()['reason'] == 'order not PAID at gateway'
        payment.refresh_from_db()
        assert payment.status == 'processing'

    def test_payment_failed_changes_nothing(self, rzp, paid_trip, api_client):
        order_id, payment = self._processing(rzp, paid_trip)
        pay = {'id': 'pay_000000000000098', 'order_id': order_id, 'status': 'failed'}
        assert self._post(api_client, rz.webhook_body('payment.failed', payment=pay),
                          'evt_c').status_code == 200
        payment.refresh_from_db()
        assert payment.status == 'processing'

    def test_payment_authorized_triggers_capture(self, rzp, paid_trip, api_client):
        order_id, payment = self._processing(rzp, paid_trip)
        pay = rzp.server.add_payment(order_id, 'authorized')
        assert self._post(api_client, rz.webhook_body('payment.authorized', payment=pay),
                          'evt_d').status_code == 200
        assert rzp.server.payments[pay['id']]['status'] == 'captured'

    @pytest.mark.parametrize('event', ['refund.processed', 'refund.failed'])
    def test_refund_events_are_recorded_on_the_payment(self, rzp, paid_trip, api_client, event):
        order_id, payment = self._processing(rzp, paid_trip)
        Payment.objects.filter(pk=payment.pk).update(gateway_payment_id='pay_000000000000077')
        refund = {'id': 'rfnd_000000000000001', 'payment_id': 'pay_000000000000077',
                  'status': 'failed' if event.endswith('failed') else 'processed', 'amount': 100}
        assert self._post(api_client, rz.webhook_body(event, refund=refund),
                          f'evt_{event}').status_code == 200
        payment.refresh_from_db()
        assert payment.gateway_metadata['refund']['status'] == refund['status']
        assert payment.status == 'processing'  # no money state moves from a refund event

    def test_an_error_while_processing_lets_the_retry_through(self, rzp, paid_trip, api_client):
        order_id, payment = self._processing(rzp, paid_trip)
        pay = rzp.server.add_payment(order_id, 'captured')
        body = rz.webhook_body('payment.captured', payment=pay)
        with mock.patch('servers.payments.razorpay_webhooks._handle_payment_event',
                        side_effect=RuntimeError('boom')):
            assert self._post(api_client, body, 'evt_e').status_code == 500
        assert WebhookEvent.objects.count() == 0  # not swallowed as a duplicate on retry
        assert self._post(api_client, body, 'evt_e').status_code == 200
        payment.refresh_from_db()
        assert payment.status == 'completed'

    def test_a_delivery_without_an_event_id_still_dedupes_by_body(self, rzp, paid_trip, api_client):
        body = rz.webhook_body('payment.failed', payment={'id': 'pay_1', 'order_id': 'order_1'})
        for _ in range(2):
            api_client.post('/api/v1/webhooks/razorpay/', data=body,
                            content_type='application/json',
                            HTTP_X_RAZORPAY_SIGNATURE=rz.sign(body, rz.WEBHOOK_SECRET))
        assert WebhookEvent.objects.filter(gateway='razorpay').count() == 1

    def test_unconfigured_api_keys_still_reject_a_forged_delivery_with_400(
            self, rzp, api_client, settings):
        settings.RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_SECRET = ''
        body = rz.webhook_body('payment.captured', payment={'id': 'pay_1', 'order_id': 'order_1'})
        resp = self._post(api_client, body, 'evt_f', secret='wrong')
        assert resp.status_code == 400
        resp = self._post(api_client, body, 'evt_g')  # validly signed, but no API keys to act with
        assert resp.status_code == 503 and WebhookEvent.objects.count() == 0

    def test_get_is_not_allowed(self, api_client):
        assert api_client.get('/api/v1/webhooks/razorpay/').status_code == 405


# ---------------------------------------------------------------------------
# LIVE test-mode tests: skipped unless RAZORPAY_KEY_ID starts with rzp_test_ (never run in CI)
# ---------------------------------------------------------------------------

import os  # noqa: E402

_LIVE = os.environ.get('RAZORPAY_KEY_ID', '').startswith('rzp_test_')


@pytest.mark.skipif(not _LIVE, reason='live Razorpay TEST-mode check: needs RAZORPAY_KEY_ID=rzp_test_...')
class TestRazorpayLiveTestMode:
    @pytest.fixture(autouse=True)
    def _real_keys(self, settings):
        settings.RAZORPAY_KEY_ID = os.environ['RAZORPAY_KEY_ID']
        settings.RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '')
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay', 'US': None}

    def test_create_and_fetch_a_test_order(self):
        gateway = get_payment_gateway('razorpay')
        order = gateway.create_order(100, trip_id=None, currency='INR', receipt='live_contract_check')
        assert order and gateway.get_order_status(order['order_id'])['order_status'] == 'CREATED'
