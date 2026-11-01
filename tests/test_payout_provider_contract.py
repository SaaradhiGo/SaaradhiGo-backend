"""PayoutProvider contract suite, parametrized over every IMPLEMENTED payout adapter.

HONEST STATUS: as with payments, there was no formal payout contract suite. The Cashfree path had
focused tests (`test_payout_unknown_outcome.py`, `test_payout_single_provider_call.py`) and none
stated the interface. This suite does, for Cashfree and RazorpayX:

  payout create (shape, the amount in the provider's unit), the same reference on a retry (one
  payout where the provider is modelled), an unknown outcome (timeout / 5xx) is
  `PayoutDispatchUnknown` and never a refund, a 4xx is a definite None, every webhook status ends in
  the right withdrawal state, a reversed payout takes the refund path exactly once, the provider
  contact / fund account is reused and tagged with its provider.

Recorded-fixture HTTP only; every id is a low-entropy placeholder. The RazorpayX-specific rules
(payload, bank payouts, key defaults and guard, webhook verification) follow. The LIVE test-mode
tests are skipped unless RAZORPAY_KEY_ID starts with rzp_test_ (never in CI).
"""

import json
import os
import time
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

import pytest
import requests
from django.contrib.auth import get_user_model

import razorpay_support as rz
from integrations.payments.cashfree import CashfreeGateway
from servers.driver.models import Driver, DriverUPIContact, WithdrawalRequest
from servers.payments.models import TransactionHistory, WebhookEvent
from servers.payments.payment_gateways.base_gateway import PayoutDispatchUnknown
from servers.payments.payment_gateways.factory import get_payment_gateway_for_payouts
from servers.rider.models import Wallet, WalletTransaction, get_wallet

pytestmark = pytest.mark.django_db
User = get_user_model()
AMOUNT_MINOR = 50000
REFERENCE = 'withdrawal_1_driver_1'


# ---------------------------------------------------------------------------
# Harnesses
# ---------------------------------------------------------------------------

class CashfreePayoutHarness:
    name = 'cashfree'
    webhook_path = '/api/v1/payments/payout-webhook/'
    secret = 'fake-cashfree-secret'

    def prepare(self, settings, monkeypatch):
        settings.PAYOUT_PROVIDERS = {'IN': 'cashfree', 'US': None}
        settings.CASHFREE_PAYOUTS_CLIENT_ID = 'payout-id-test'
        settings.CASHFREE_PAYOUTS_CLIENT_SECRET = 'payout-secret-test'
        settings.CASHFREE_WEBHOOK_SECRET = self.secret
        monkeypatch.setattr(CashfreeGateway, '_payout_token', None)
        monkeypatch.setattr(CashfreeGateway, '_payout_token_expires_at', 0)
        self.transfers, self.transfer_bodies = {}, []
        self.mode = 'ok'

        def post(url, headers=None, json=None, timeout=None, **kw):
            assert timeout
            if url.endswith('/authorize'):
                return _resp(200, {'data': {'token': 'token-fake'}})
            if self.mode == 'timeout':
                raise requests.exceptions.Timeout('slow')
            if self.mode == '5xx':
                return _resp(502, {})
            if self.mode == '4xx':
                return _resp(422, {'status': 'ERROR', 'message': 'bad vpa'})
            self.transfer_bodies.append(json)
            self.transfers.setdefault(json['transferId'], json)
            return _resp(200, {'status': 'SUCCESS', 'data': {'referenceId': 'cf_ref_1'}})

        monkeypatch.setattr(requests, 'post', post)

    def gateway(self):
        return get_payment_gateway_for_payouts('IN')

    def sent_amount(self):
        return self.transfer_bodies[-1]['amount']

    expected_amount = '500.00'

    def identifiers(self):
        return [b['transferId'] for b in self.transfer_bodies]

    def payouts_created(self):
        return len(self.transfers)

    def webhook(self, kind, reference, event_id):
        event = {'success': 'TRANSFER_SUCCESS', 'failed': 'TRANSFER_FAILED',
                 'reversed': 'TRANSFER_REVERSED', 'pending': 'PAYOUT_PENDING'}[kind]
        body = json.dumps({'event': event, 'transfer_id': reference, 'cf_transfer_id': 'cf_t_1',
                           'status': kind.upper(), 'failureReason': 'bank said no'}).encode()
        import base64, hashlib, hmac  # noqa: E401
        ts = str(int(time.time()))
        sig = base64.b64encode(hmac.new(self.secret.encode(), (ts + body.decode()).encode(),
                                        hashlib.sha256).digest()).decode()
        return body, {'HTTP_X_WEBHOOK_SIGNATURE': sig, 'HTTP_X_WEBHOOK_TIMESTAMP': ts}

    def forged(self, kind, reference, event_id):
        body, headers = self.webhook(kind, reference, event_id)
        headers['HTTP_X_WEBHOOK_SIGNATURE'] = 'forged'
        return body, headers

    states = ['success', 'failed', 'reversed', 'pending']


def _resp(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body, text=str(body),
                           content=b'{}', raise_for_status=lambda: None)


class RazorpayXPayoutHarness:
    name = 'razorpay'
    webhook_path = '/api/v1/webhooks/razorpayx/'

    def prepare(self, settings, monkeypatch):
        settings.PAYOUT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET = rz.KEY_ID, rz.KEY_SECRET
        settings.RAZORPAYX_KEY_ID = settings.RAZORPAYX_KEY_SECRET = ''  # fall back to the above
        settings.RAZORPAYX_ACCOUNT_NUMBER = rz.ACCOUNT
        settings.RAZORPAYX_WEBHOOK_SECRET = rz.X_WEBHOOK_SECRET
        settings.IS_PRODUCTION = False
        self.server = rz.FakeRazorpay().install(monkeypatch)
        self.mode = 'ok'
        real = self.server._request

        def request(method, url, **kw):
            if method == 'POST' and url.endswith('/payouts'):
                if self.mode == 'timeout':
                    self.server.calls.append({'method': method, 'path': '/payouts', 'json': kw.get('json'),
                                              'headers': kw.get('headers')})
                    raise requests.exceptions.ReadTimeout('slow')
                if self.mode == '5xx':
                    return rz._Response(502, {})
                if self.mode == '4xx':
                    return rz._Response(400, {'error': {'code': 'BAD_REQUEST_ERROR',
                                                        'description': 'invalid vpa'}})
            return real(method, url, **kw)

        monkeypatch.setattr('integrations.razorpay_http.requests.request', request)

    def gateway(self):
        return get_payment_gateway_for_payouts('IN')

    def sent_amount(self):
        return self.server.calls_to('POST', '/payouts')[-1]['json']['amount']

    expected_amount = AMOUNT_MINOR

    def identifiers(self):
        return [c['headers']['X-Payout-Idempotency'] for c in self.server.calls_to('POST', '/payouts')]

    def payouts_created(self):
        return len(self.server.payouts)

    def webhook(self, kind, reference, event_id):
        event, status = {'success': ('payout.processed', 'processed'),
                         'failed': ('payout.failed', 'failed'),
                         'rejected': ('payout.rejected', 'rejected'),
                         'reversed': ('payout.reversed', 'reversed'),
                         'pending': ('payout.pending', 'pending'),
                         'queued': ('payout.queued', 'queued')}[kind]
        entity = {'id': 'pout_000000000000001', 'reference_id': reference, 'status': status,
                  'status_details': {'description': 'bank said no'}}
        body = rz.webhook_body(event, payout=entity)
        return body, {'HTTP_X_RAZORPAY_SIGNATURE': rz.sign(body, rz.X_WEBHOOK_SECRET),
                      'HTTP_X_RAZORPAY_EVENT_ID': event_id}

    def forged(self, kind, reference, event_id):
        body, headers = self.webhook(kind, reference, event_id)
        headers['HTTP_X_RAZORPAY_SIGNATURE'] = rz.sign(body, rz.WEBHOOK_SECRET)  # the PG secret
        return body, headers

    states = ['success', 'failed', 'rejected', 'reversed', 'pending', 'queued']


@pytest.fixture(params=[CashfreePayoutHarness, RazorpayXPayoutHarness], ids=['cashfree', 'razorpayx'])
def world(request, settings, monkeypatch):
    harness = request.param()
    harness.prepare(settings, monkeypatch)
    return harness


@pytest.fixture
def rx(settings, monkeypatch):
    harness = RazorpayXPayoutHarness()
    harness.prepare(settings, monkeypatch)
    return harness


@pytest.fixture
def driver(db):
    user = User.objects.create_user(phone_number='+919800000951', role='driver',
                                    full_name='Test Driver')
    d = Driver.objects.create(user_id=user, approved=True, upi_id='qa-test@upi')
    wallet = get_wallet(user, Wallet.SCOPE_DRIVER)
    wallet.balance = D('5000.00')
    wallet.save(update_fields=['balance_minor'])
    return d


def _withdrawal(driver, status='approved', amount='1000.00'):
    return WithdrawalRequest.objects.create(driver=driver, amount=D(amount), payout_method='upi',
                                            status=status)


def _refunds(driver):
    return WalletTransaction.objects.filter(user_id=driver.user_id,
                                            purpose='refund_failed_withdrawal', txn_type='credit')


def _balance(driver):
    return get_wallet(driver.user_id, Wallet.SCOPE_DRIVER).balance


# ---------------------------------------------------------------------------
# The contract (adapter level)
# ---------------------------------------------------------------------------

class TestPayoutProviderContract:
    def test_selectable_and_truthy_when_configured(self, world):
        gateway = world.gateway()
        assert gateway and gateway.get_name() == world.name

    def test_payout_create_shape(self, world):
        result = world.gateway().create_upi_payout(
            upi_id='qa-test@upi', amount_minor=AMOUNT_MINOR, purpose='payout', currency='INR',
            reference_id=REFERENCE, name='Test Driver')
        assert result['payout_id'] == REFERENCE  # our stable reference, what the webhook returns
        assert result['status']
        assert 'secret' not in json.dumps(result, default=str).lower()

    def test_the_amount_goes_out_in_the_providers_unit(self, world):
        world.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE,
                                          name='Test Driver')
        assert world.sent_amount() == world.expected_amount

    def test_a_retry_sends_the_same_identifier_and_creates_one_payout(self, world):
        gateway = world.gateway()
        for _ in range(2):
            gateway.create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE,
                                      name='Test Driver')
        assert set(world.identifiers()) == {REFERENCE}  # stable across retries
        assert world.payouts_created() == 1

    def test_a_timeout_is_an_unknown_outcome_never_a_failure(self, world):
        world.mode = 'timeout'
        with pytest.raises(PayoutDispatchUnknown):
            world.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE,
                                              name='Test Driver')

    def test_a_5xx_is_an_unknown_outcome(self, world):
        world.mode = '5xx'
        with pytest.raises(PayoutDispatchUnknown):
            world.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE,
                                              name='Test Driver')

    def test_a_4xx_is_a_definite_rejection(self, world):
        world.mode = '4xx'
        assert world.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR,
                                                 reference_id=REFERENCE, name='Test Driver') is None

    def test_a_bad_amount_or_upi_id_sends_nothing(self, world):
        gateway = world.gateway()
        for amount in (0, -1, 12.5, True):
            assert gateway.create_upi_payout('qa-test@upi', amount, reference_id=REFERENCE) is None
        assert gateway.create_upi_payout('', AMOUNT_MINOR, reference_id=REFERENCE) is None
        assert world.payouts_created() == 0

    def test_unconfigured_is_falsy_and_makes_no_call(self, world, settings, monkeypatch):
        sent = mock.Mock()
        monkeypatch.setattr(requests, 'post', sent)
        monkeypatch.setattr(requests, 'request', sent)
        settings.CASHFREE_PAYOUTS_CLIENT_ID = settings.CASHFREE_PAYOUTS_CLIENT_SECRET = ''
        settings.RAZORPAYX_ACCOUNT_NUMBER = ''
        gateway = world.gateway()
        assert not gateway
        assert gateway.create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE) is None
        sent.assert_not_called()


# ---------------------------------------------------------------------------
# The contract (through the withdrawal flow and the webhook endpoint)
# ---------------------------------------------------------------------------

def _dispatch(driver):
    from servers.driver.services import trigger_payout_creation
    w = _withdrawal(driver)
    assert trigger_payout_creation(w) is True
    w.refresh_from_db()
    return w


class TestWithdrawalFlow:
    def test_a_dispatched_payout_is_processed_and_tagged_with_its_provider(self, world, driver):
        w = _dispatch(driver)
        assert w.status == 'processed' and w.payout_reference_id == f'withdrawal_{w.id}_driver_{driver.id}'
        contact = DriverUPIContact.objects.get(driver=driver)
        assert contact.gateway_provider == world.name  # a provider's ids are never anonymous

    def test_an_unknown_outcome_holds_the_money(self, world, driver):
        from servers.driver.services import trigger_payout_creation
        world.mode = 'timeout'
        before = _balance(driver)
        w = _withdrawal(driver)
        trigger_payout_creation(w)
        w.refresh_from_db()
        assert w.status == 'unresolved'
        assert _balance(driver) == before and _refunds(driver).count() == 0

    def test_a_definite_rejection_refunds_exactly_once(self, world, driver):
        from servers.driver.services import trigger_payout_creation
        world.mode = '4xx'
        w = _withdrawal(driver)
        trigger_payout_creation(w)
        w.refresh_from_db()
        assert w.status == 'failed' and _refunds(driver).count() == 1


class TestPayoutWebhookStatuses:
    def _deliver(self, client, world, kind, reference, event_id):
        body, headers = world.webhook(kind, reference, event_id)
        return client.post(world.webhook_path, data=body, content_type='application/json', **headers)

    def test_every_status_ends_in_the_right_state(self, world, driver, api_client):
        expected = {'success': ('completed', 0), 'failed': ('failed', 1), 'rejected': ('failed', 1),
                    'reversed': ('failed', 1), 'pending': ('processed', 0), 'queued': ('processed', 0)}
        for kind in world.states:
            WithdrawalRequest.objects.all().delete()
            WalletTransaction.objects.filter(purpose='refund_failed_withdrawal').delete()
            w = _withdrawal(driver, status='processed')
            w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
            w.save(update_fields=['payout_reference_id'])
            resp = self._deliver(api_client, world, kind, w.payout_reference_id, f'evt_{kind}')
            assert resp.status_code == 200, (kind, resp.content)
            w.refresh_from_db()
            status, refunds = expected[kind]
            assert (w.status, _refunds(driver).count()) == (status, refunds), kind

    def test_reversed_takes_the_refund_path_exactly_once(self, world, driver, api_client):
        w = _withdrawal(driver, status='processed')
        w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
        w.save(update_fields=['payout_reference_id'])
        before = _balance(driver)
        self._deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_r1')
        self._deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_r2')  # a re-send
        assert _refunds(driver).count() == 1
        assert _balance(driver) == before + D('1000.00')
        history = TransactionHistory.objects.get(withdrawal_request=w)
        assert history.status == 'reversed' and history.payment_gateway == world.name

    def test_success_records_the_payout_under_its_provider(self, world, driver, api_client):
        w = _withdrawal(driver, status='processed')
        w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
        w.save(update_fields=['payout_reference_id'])
        self._deliver(api_client, world, 'success', w.payout_reference_id, 'evt_s')
        history = TransactionHistory.objects.get(withdrawal_request=w)
        assert history.status == 'completed' and history.payment_gateway == world.name
        driver.refresh_from_db()
        assert driver.last_withdrawal_at is not None

    def test_a_forged_delivery_is_400_with_no_side_effect(self, world, driver, api_client):
        w = _withdrawal(driver, status='processed')
        w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
        w.save(update_fields=['payout_reference_id'])
        body, headers = world.forged('failed', w.payout_reference_id, 'evt_x')
        resp = api_client.post(world.webhook_path, data=body, content_type='application/json',
                               **headers)
        assert resp.status_code == 400
        w.refresh_from_db()
        assert w.status == 'processed' and _refunds(driver).count() == 0
        assert WebhookEvent.objects.count() == 0

    def test_a_duplicate_delivery_is_idempotent(self, world, driver, api_client):
        w = _withdrawal(driver, status='processed')
        w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
        w.save(update_fields=['payout_reference_id'])
        body, headers = world.webhook('failed', w.payout_reference_id, 'evt_dup')
        first = api_client.post(world.webhook_path, data=body, content_type='application/json', **headers)
        second = api_client.post(world.webhook_path, data=body, content_type='application/json', **headers)
        assert first.status_code == 200 and second.json()['status'] == 'already_processed'
        assert _refunds(driver).count() == 1


# ---------------------------------------------------------------------------
# RazorpayX specifics
# ---------------------------------------------------------------------------

class TestRazorpayXPayouts:
    def test_the_payload_headers_and_chain(self, rx, driver):
        w = _dispatch(driver)
        server = rx.server
        contact = server.calls_to('POST', '/contacts')[0]['json']
        assert contact == {'name': 'Test Driver', 'type': 'vendor', 'reference_id': str(driver.id)}
        fund = server.calls_to('POST', '/fund_accounts')[0]['json']
        assert fund['account_type'] == 'vpa' and fund['vpa'] == {'address': 'qa-test@upi'}
        call = server.calls_to('POST', '/payouts')[0]
        assert call['headers']['X-Payout-Idempotency'] == w.payout_reference_id
        assert call['auth'] == (rz.KEY_ID, rz.KEY_SECRET) and call['timeout']
        body = call['json']
        assert body['account_number'] == rz.ACCOUNT and body['mode'] == 'UPI'
        assert body['purpose'] == 'payout' and body['queue_if_low_balance'] is True
        assert body['currency'] == 'INR' and body['amount'] == 100000
        assert body['reference_id'] == w.payout_reference_id
        assert body['fund_account_id'] == list(server.fund_accounts.values())[0]['id']

    def test_the_contact_and_fund_account_are_reused_for_the_next_payout(self, rx, driver):
        _dispatch(driver)
        contacts, funds = len(rx.server.calls_to('POST', '/contacts')), len(rx.server.calls_to('POST', '/fund_accounts'))
        _dispatch(driver)
        assert len(rx.server.calls_to('POST', '/contacts')) == contacts
        assert len(rx.server.calls_to('POST', '/fund_accounts')) == funds
        assert len(rx.server.payouts) == 2

    def test_a_changed_upi_id_gets_a_new_fund_account_on_the_same_contact(self, rx, driver):
        _dispatch(driver)
        driver.upi_id = 'other-id@upi'
        driver.save(update_fields=['upi_id'])
        _dispatch(driver)
        assert len(rx.server.calls_to('POST', '/contacts')) == 1
        assert len(rx.server.calls_to('POST', '/fund_accounts')) == 2

    def test_a_cashfree_id_is_never_reused_for_razorpay(self, rx, driver):
        DriverUPIContact.objects.create(
            driver=driver, gateway_contact_id='cf_contact_1', gateway_fund_account_id='cf_fa_1',
            upi_id='qa-test@upi', gateway_provider='cashfree')
        _dispatch(driver)
        sent = json.dumps([c['json'] for c in rx.server.calls])
        assert 'cf_contact_1' not in sent and 'cf_fa_1' not in sent
        assert len(rx.server.calls_to('POST', '/contacts')) == 1
        contact = DriverUPIContact.objects.get(driver=driver)
        assert contact.gateway_provider == 'razorpay' and contact.gateway_contact_id.startswith('cont_')

    def test_a_row_from_before_the_provider_field_is_not_trusted(self, rx, driver):
        DriverUPIContact.objects.create(
            driver=driver, gateway_contact_id='legacy_1', gateway_fund_account_id='legacy_fa',
            upi_id='qa-test@upi', gateway_provider=None)
        _dispatch(driver)
        assert 'legacy_1' not in json.dumps([c['json'] for c in rx.server.calls])

    def test_a_failure_before_the_payout_is_a_definite_no_and_refunds(self, rx, driver):
        from servers.driver.services import trigger_payout_creation
        rx.server.fail('POST', r'^/contacts$', 400)
        w = _withdrawal(driver)
        trigger_payout_creation(w)
        w.refresh_from_db()
        assert w.status == 'failed' and _refunds(driver).count() == 1
        assert rx.server.calls_to('POST', '/payouts') == []

    def test_a_timeout_creating_the_contact_is_not_an_unknown_payout(self, rx, driver):
        from servers.driver.services import trigger_payout_creation
        rx.server.fail('POST', r'^/contacts$', requests.exceptions.Timeout('slow'))
        w = _withdrawal(driver)
        trigger_payout_creation(w)
        w.refresh_from_db()
        assert w.status == 'failed'  # no payout was ever requested: refundable

    @pytest.mark.parametrize('status', ['failed', 'rejected', 'cancelled'])
    def test_a_payout_created_already_dead_is_a_rejection(self, rx, status):
        rx.server.payout_status = status
        assert rx.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE,
                                              name='Test Driver') is None

    @pytest.mark.parametrize('status', ['queued', 'pending', 'processing', 'processed'])
    def test_a_live_status_is_accepted(self, rx, status):
        rx.server.payout_status = status
        result = rx.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE,
                                                name='Test Driver')
        assert result['status'] == status and result['gateway_reference'].startswith('pout_')

    def test_below_the_minimum_amount_is_refused(self, rx):
        assert rx.gateway().create_upi_payout('qa-test@upi', 99, reference_id=REFERENCE) is None

    def test_a_long_reference_is_cut_to_forty_characters_consistently(self, rx):
        long_ref = 'withdrawal_' + '9' * 60
        rx.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=long_ref, name='Test Driver')
        rx.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=long_ref, name='Test Driver')
        assert len(rx.server.payouts) == 1 and len(rx.identifiers()[0]) == 40

    def test_odd_names_are_made_acceptable_to_the_contact_rules(self, rx):
        rx.gateway().ensure_contact('  A$%^&*!  ', 'ref')
        rx.gateway().ensure_contact('Rao, Srinivasa & Sons', 'ref2')
        names = [c['json']['name'] for c in rx.server.calls_to('POST', '/contacts')]
        assert names[0] == 'SaaradhiGo Driver' and names[1] == 'Rao Srinivasa  Sons'

    def test_status_lookup_by_id_and_by_reference(self, rx):
        result = rx.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE,
                                                name='Test Driver')
        by_id = rx.gateway().get_payout_status(result['gateway_reference'])
        by_ref = rx.gateway().get_payout_status(REFERENCE)
        assert by_id['status'] == by_ref['status'] == 'PROCESSING' and by_id['bucket'] == 'inflight'
        assert rx.gateway().get_payout_status('nope') is None
        assert rx.server.calls_to('GET', '/payouts')[-1]['params']['account_number'] == rz.ACCOUNT

    def test_the_status_vocabulary_maps_to_withdrawal_buckets(self):
        from integrations.payouts.razorpayx import STATUS_BUCKET
        assert STATUS_BUCKET == {
            'queued': 'inflight', 'pending': 'inflight', 'processing': 'inflight',
            'processed': 'completed', 'failed': 'failed', 'rejected': 'failed',
            'cancelled': 'failed', 'reversed': 'reversed'}

    def test_bank_payout_chain_uses_imps_and_the_bank_account_fund_account(self, rx):
        result = rx.gateway().create_bank_payout(
            '000111222333', 'abcd0123456', AMOUNT_MINOR, reference_id='withdrawal_7_driver_1',
            name='Test Driver')
        assert result['status'] == 'processing'
        fund = rx.server.calls_to('POST', '/fund_accounts')[0]['json']
        assert fund['account_type'] == 'bank_account'
        assert fund['bank_account'] == {'name': 'Test Driver', 'ifsc': 'ABCD0123456',
                                        'account_number': '000111222333'}
        assert rx.server.calls_to('POST', '/payouts')[0]['json']['mode'] == 'IMPS'
        assert rx.gateway().create_bank_payout('1', 'X', AMOUNT_MINOR, reference_id='r', mode='UPI') is None

    def test_the_withdrawal_flow_still_refuses_bank_payouts(self, rx, driver):
        """Bank payouts are wired at the adapter only; the service path is deliberately unchanged."""
        from servers.driver.services import trigger_payout_creation
        w = WithdrawalRequest.objects.create(driver=driver, amount=D('1000.00'),
                                             payout_method='bank', status='approved')
        trigger_payout_creation(w)
        w.refresh_from_db()
        assert w.status == 'failed' and rx.server.calls_to('POST', '/payouts') == []

    def test_payout_webhook_lookup_by_our_reference_not_the_pout_id(self, rx, driver, api_client):
        w = _dispatch(driver)
        server = rx.server
        pout = list(server.payouts.values())[0]
        entity = dict(pout, status='processed')
        body = rz.webhook_body('payout.processed', payout=entity)
        resp = api_client.post('/api/v1/webhooks/razorpayx/', data=body, content_type='application/json',
                               HTTP_X_RAZORPAY_SIGNATURE=rz.sign(body, rz.X_WEBHOOK_SECRET),
                               HTTP_X_RAZORPAY_EVENT_ID='evt_proc')
        assert resp.status_code == 200
        w.refresh_from_db()
        assert w.status == 'completed' and w.payout_status == 'processed'
        assert TransactionHistory.objects.get(withdrawal_request=w).gateway_transaction_id == pout['id']

    def test_noise_events_are_acknowledged_and_change_nothing(self, rx, driver, api_client):
        w = _withdrawal(driver, status='processed')
        w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
        w.save(update_fields=['payout_reference_id'])
        for event in ('payout.updated', 'payout.downtime.started', 'transaction.created'):
            body = rz.webhook_body(event, payout={'id': 'pout_1', 'reference_id': w.payout_reference_id,
                                                  'status': 'processing'})
            resp = api_client.post('/api/v1/webhooks/razorpayx/', data=body, content_type='application/json',
                                   HTTP_X_RAZORPAY_SIGNATURE=rz.sign(body, rz.X_WEBHOOK_SECRET),
                                   HTTP_X_RAZORPAY_EVENT_ID=f'evt_{event}')
            assert resp.status_code == 200
        w.refresh_from_db()
        assert w.status == 'processed' and _refunds(driver).count() == 0

    def test_a_payout_for_an_unknown_reference_is_acknowledged(self, rx, api_client):
        body = rz.webhook_body('payout.failed', payout={'id': 'pout_1', 'reference_id': 'nothing_1',
                                                         'status': 'failed'})
        resp = api_client.post('/api/v1/webhooks/razorpayx/', data=body, content_type='application/json',
                               HTTP_X_RAZORPAY_SIGNATURE=rz.sign(body, rz.X_WEBHOOK_SECRET),
                               HTTP_X_RAZORPAY_EVENT_ID='evt_unk')
        assert resp.status_code == 200 and resp.json()['reason'] == 'withdrawal not found'

    def test_the_payments_webhook_secret_does_not_verify_payout_deliveries(self, rx):
        gateway = rx.gateway()
        body = b'{"event":"payout.processed"}'
        assert gateway.verify_webhook_signature(body, rz.sign(body, rz.X_WEBHOOK_SECRET)) is True
        assert gateway.verify_webhook_signature(body, rz.sign(body, rz.WEBHOOK_SECRET)) is False
        assert gateway.verify_webhook_signature(body, '') is False

    def test_the_payout_webhook_fails_closed_without_its_secret(self, rx, settings, api_client):
        settings.RAZORPAYX_WEBHOOK_SECRET = ''
        body = rz.webhook_body('payout.failed', payout={'id': 'x', 'reference_id': 'y'})
        resp = api_client.post('/api/v1/webhooks/razorpayx/', data=body, content_type='application/json',
                               HTTP_X_RAZORPAY_SIGNATURE=rz.sign(body, ''))
        assert resp.status_code == 400


class TestRazorpayXConfiguration:
    def test_the_x_keys_default_to_the_payment_gateway_pair(self, rx, settings):
        settings.RAZORPAYX_KEY_ID = settings.RAZORPAYX_KEY_SECRET = ''
        rx.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE, name='Test Driver')
        assert rx.server.calls_to('POST', '/payouts')[0]['auth'] == (rz.KEY_ID, rz.KEY_SECRET)

    def test_dedicated_x_keys_win_when_set(self, rx, settings):
        settings.RAZORPAYX_KEY_ID, settings.RAZORPAYX_KEY_SECRET = 'rzp_test_xxxxxxxxxxxxxx', 'x-secret'
        rx.gateway().create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE, name='Test Driver')
        assert rx.server.calls_to('POST', '/payouts')[0]['auth'] == ('rzp_test_xxxxxxxxxxxxxx', 'x-secret')

    def test_the_account_number_is_required(self, rx, settings):
        settings.RAZORPAYX_ACCOUNT_NUMBER = ''
        gateway = get_payment_gateway_for_payouts('IN')
        assert not gateway and 'RAZORPAYX_ACCOUNT_NUMBER' in gateway.reason

    def test_the_live_key_is_refused_outside_production(self, rx, settings):
        settings.RAZORPAY_KEY_ID = 'rzp_live_fakefakefake'
        gateway = get_payment_gateway_for_payouts('IN')
        assert not gateway and 'rzp_live_' in gateway.reason and 'fakefake' not in gateway.reason
        assert gateway.create_upi_payout('qa-test@upi', AMOUNT_MINOR, reference_id=REFERENCE) is None
        assert rx.server.calls == []
        settings.IS_PRODUCTION = True
        assert get_payment_gateway_for_payouts('IN')

    def test_the_alias_resolves_to_the_same_provider(self, rx):
        from integrations.payments import registry
        assert registry.canonical('razorpayx') == 'razorpay'
        assert get_payment_gateway_for_payouts('IN', gateway_name='razorpayx').get_name() == 'razorpay'

    def test_the_payments_adapter_does_not_pay_out(self, rx, settings):
        from servers.payments.payment_gateways.factory import get_payment_gateway
        settings.PAYMENT_PROVIDERS = {'IN': 'razorpay'}
        assert get_payment_gateway('razorpay').create_upi_payout('a@b', AMOUNT_MINOR) is None


# ---------------------------------------------------------------------------
# LIVE test-mode tests: skipped unless RAZORPAY_KEY_ID starts with rzp_test_ (never run in CI)
# ---------------------------------------------------------------------------

_LIVE = os.environ.get('RAZORPAY_KEY_ID', '').startswith('rzp_test_') \
    and bool(os.environ.get('RAZORPAYX_ACCOUNT_NUMBER'))


@pytest.mark.skipif(not _LIVE, reason='live RazorpayX TEST-mode check: needs rzp_test_ keys and '
                                      'RAZORPAYX_ACCOUNT_NUMBER (use manage.py razorpay_test_mode_check)')
class TestRazorpayXLiveTestMode:
    def test_one_rupee_payout_is_idempotent(self, settings):
        settings.RAZORPAY_KEY_ID = os.environ['RAZORPAY_KEY_ID']
        settings.RAZORPAY_KEY_SECRET = os.environ['RAZORPAY_KEY_SECRET']
        settings.RAZORPAYX_ACCOUNT_NUMBER = os.environ['RAZORPAYX_ACCOUNT_NUMBER']
        settings.PAYOUT_PROVIDERS = {'IN': 'razorpay', 'US': None}
        gateway = get_payment_gateway_for_payouts('IN')
        ref = f'live_contract_{int(time.time())}'
        first = gateway.create_upi_payout('success@razorpay', 100, reference_id=ref, name='Test Driver')
        again = gateway.create_upi_payout('success@razorpay', 100, reference_id=ref, name='Test Driver')
        assert first and again and first['gateway_reference'] == again['gateway_reference']
