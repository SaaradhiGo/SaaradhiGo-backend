"""A recorded-fixture Razorpay / RazorpayX server for the contract tests (no network).

`FakeRazorpay` replaces `requests.request` inside `integrations.razorpay_http` and answers the
endpoints the adapters call, with response bodies shaped like the examples in the Razorpay docs
(orders, payments, refunds, contacts, fund accounts, payouts). Behaviour that the contract depends
on is modelled: a repeated `X-Payout-Idempotency` key returns the SAME payout, a repeated refund
`receipt` answers 400 "Duplicate receipt", and the same contact / fund account details return the
existing object. Every value is an obviously fake, low-entropy placeholder.
"""

import re

import requests

KEY_ID = 'rzp_test_fakefakefake'
KEY_SECRET = 'fake-key-secret'
WEBHOOK_SECRET = 'fake-webhook-secret'
X_WEBHOOK_SECRET = 'fake-x-webhook-secret'
ACCOUNT = 'fake-account-0001'


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = b'{}' if body is not None else b''
        self.text = str(body)

    def json(self):
        if self._body is None:
            raise ValueError('no body')
        return self._body


class FakeRazorpay:
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.payments = {}
        self.refunds = {}
        self.contacts = {}
        self.fund_accounts = {}
        self.payouts = {}
        self.idempotency = {}
        self.payout_status = 'processing'
        self._next = 0
        #: (method, path-regex) -> exception instance, or an int HTTP status to answer with
        self.faults = []

    # -- plumbing -----------------------------------------------------------------------
    def install(self, monkeypatch):
        monkeypatch.setattr('integrations.razorpay_http.requests.request', self._request)
        return self

    def _id(self, prefix):
        self._next += 1
        return f'{prefix}_{self._next:014d}'

    def fail(self, method, path_regex, fault):
        self.faults.append((method, re.compile(path_regex), fault))

    def calls_to(self, method, path_prefix):
        return [c for c in self.calls if c['method'] == method and c['path'].startswith(path_prefix)]

    def _request(self, method, url, auth=None, json=None, params=None, headers=None, timeout=None):
        path = url.split('/v1', 1)[1]
        self.calls.append({'method': method, 'path': path, 'json': json, 'params': params,
                           'headers': dict(headers or {}), 'auth': auth, 'timeout': timeout})
        assert timeout, 'every Razorpay call must carry a timeout'
        for fmethod, rx, fault in self.faults:
            if fmethod == method and rx.search(path):
                if isinstance(fault, Exception):
                    raise fault
                return _Response(fault, {'error': {'code': 'SERVER_ERROR' if fault >= 500 else 'BAD_REQUEST_ERROR',
                                                   'description': 'injected'}})
        handler = self._route(method, path)
        status, body = handler(path, json or {}, params or {}, headers or {})
        return _Response(status, body)

    def _route(self, method, path):
        table = [
            ('POST', r'^/orders$', self._create_order),
            ('GET', r'^/orders/[^/]+/payments$', self._order_payments),
            ('GET', r'^/orders/[^/]+$', self._get_order),
            ('GET', r'^/payments/[^/]+/refunds$', self._list_refunds),
            ('GET', r'^/payments/[^/]+$', self._get_payment),
            ('POST', r'^/payments/[^/]+/capture$', self._capture),
            ('POST', r'^/payments/[^/]+/refund$', self._refund),
            ('POST', r'^/contacts$', self._contact),
            ('POST', r'^/fund_accounts$', self._fund_account),
            ('POST', r'^/payouts$', self._payout),
            ('GET', r'^/payouts/[^/]+$', self._get_payout),
            ('GET', r'^/payouts$', self._list_payouts),
        ]
        for m, rx, fn in table:
            if m == method and re.match(rx, path):
                return fn
        return lambda *a: (404, {'error': {'code': 'BAD_REQUEST_ERROR', 'description': 'not found'}})

    # -- orders and payments --------------------------------------------------------------
    def _create_order(self, path, body, params, headers):
        order = {'id': self._id('order'), 'entity': 'order', 'amount': body['amount'],
                 'amount_paid': 0, 'currency': body['currency'], 'receipt': body.get('receipt'),
                 'status': 'created', 'notes': body.get('notes', {})}
        self.orders[order['id']] = order
        return 200, order

    def _get_order(self, path, *_):
        order = self.orders.get(path.rsplit('/', 1)[1])
        return (200, order) if order else (400, {'error': {'code': 'BAD_REQUEST_ERROR',
                                                           'description': 'no such order'}})

    def add_payment(self, order_id, status='captured'):
        order = self.orders[order_id]
        pay = {'id': self._id('pay'), 'entity': 'payment', 'order_id': order_id,
               'amount': order['amount'], 'currency': order['currency'], 'status': status}
        self.payments[pay['id']] = pay
        if status == 'captured':
            order['status'], order['amount_paid'] = 'paid', order['amount']
        return pay

    def _order_payments(self, path, *_):
        oid = path.split('/')[2]
        return 200, {'items': [p for p in self.payments.values() if p['order_id'] == oid]}

    def _get_payment(self, path, *_):
        pay = self.payments.get(path.rsplit('/', 1)[1])
        return (200, pay) if pay else (400, {'error': {'code': 'BAD_REQUEST_ERROR',
                                                       'description': 'no such payment'}})

    def _capture(self, path, body, *_):
        pay = self.payments[path.split('/')[2]]
        if pay['status'] != 'authorized':
            return 400, {'error': {'code': 'BAD_REQUEST_ERROR', 'description': 'not authorized'}}
        pay['status'] = 'captured'
        self.orders[pay['order_id']].update(status='paid', amount_paid=pay['amount'])
        return 200, pay

    def _refund(self, path, body, *_):
        pid = path.split('/')[2]
        receipt = body.get('receipt')
        if any(r['receipt'] == receipt and r['payment_id'] == pid for r in self.refunds.values()):
            return 400, {'error': {'code': 'BAD_REQUEST_ERROR', 'description': 'Duplicate receipt'}}
        pay = self.payments[pid]
        refund = {'id': self._id('rfnd'), 'entity': 'refund', 'payment_id': pid,
                  'amount': body.get('amount', pay['amount']), 'receipt': receipt,
                  'status': 'processed', 'speed_processed': 'instant'}
        self.refunds[refund['id']] = refund
        return 200, refund

    def _list_refunds(self, path, *_):
        pid = path.split('/')[2]
        return 200, {'items': [r for r in self.refunds.values() if r['payment_id'] == pid]}

    # -- RazorpayX ------------------------------------------------------------------------
    def _contact(self, path, body, *_):
        key = (body.get('name'), body.get('type'), body.get('reference_id'))
        if key not in self.contacts:
            self.contacts[key] = {'id': self._id('cont'), 'entity': 'contact', **body}
        return 201, self.contacts[key]

    def _fund_account(self, path, body, *_):
        key = (body['contact_id'], body['account_type'], str(body.get('vpa') or body.get('bank_account')))
        if key not in self.fund_accounts:
            self.fund_accounts[key] = {'id': self._id('fa'), 'entity': 'fund_account', **body,
                                       'active': True}
        return 201, self.fund_accounts[key]

    def _payout(self, path, body, params, headers):
        key = headers.get('X-Payout-Idempotency')
        if not key:
            return 400, {'error': {'code': 'BAD_REQUEST_ERROR',
                                   'description': 'X-Payout-Idempotency header is required'}}
        if key in self.idempotency:
            return 200, self.payouts[self.idempotency[key]]
        payout = {'id': self._id('pout'), 'entity': 'payout', 'status': self.payout_status,
                  'reference_id': body.get('reference_id'), 'mode': body.get('mode'),
                  'amount': body['amount'], 'fund_account_id': body['fund_account_id'],
                  'status_details': {'description': None}}
        self.payouts[payout['id']] = payout
        self.idempotency[key] = payout['id']
        return 200, payout

    def _get_payout(self, path, *_):
        payout = self.payouts.get(path.rsplit('/', 1)[1])
        return (200, payout) if payout else (400, {'error': {'code': 'BAD_REQUEST_ERROR',
                                                             'description': 'no such payout'}})

    def _list_payouts(self, path, body, params, headers):
        items = [p for p in self.payouts.values()
                 if p['reference_id'] == params.get('reference_id')]
        return 200, {'items': items}


def webhook_body(event, **entities):
    """A webhook body as Razorpay sends it: {"entity":"event","event":..., "payload":{...}}."""
    import json
    return json.dumps({'entity': 'event', 'event': event,
                       'payload': {k: {'entity': v} for k, v in entities.items()}}).encode()


def sign(body, secret):
    from integrations.razorpay_http import hmac_hex
    return hmac_hex(secret, body)


assert requests  # the fake replaces requests.request; keep the import explicit
