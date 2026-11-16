"""Launch round 3, item 3: Connect onboarding on Stripe Accounts v2, behind STRIPE_ACCOUNTS_API.

Request shapes follow docs.stripe.com/api/v2/core/accounts and /api/v2/core/account-links (read
16 Nov 2026). No network: requests is replaced. Payouts are untouched (still /v1/transfers).
"""
from types import SimpleNamespace

import pytest

from integrations.payouts import driver_onboarding as vendor
from servers.driver import stripe_connect


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}
        self.ok = status < 400
        self.content = b'x'
        self.text = str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(response=self)


class _Driver(SimpleNamespace):
    def save(self, update_fields=None):
        self.saved = update_fields


def _driver(acct=''):
    return _Driver(pk=30, stripe_account_id=acct, stripe_onboarding_status='not_started',
                   user_id=SimpleNamespace(email='driver@example.test', full_name='Test Driver'))


@pytest.fixture
def stripe(monkeypatch, settings):
    settings.STRIPE_SECRET_KEY = 'sk_test_x'
    settings.FRONTEND_URL = 'https://www.qa.example.test'
    settings.STRIPE_V2_API_VERSION = '2026-10-28.endive'
    calls = []
    answers = {}

    def post(url, **kw):
        calls.append(('POST', url, kw))
        return answers.get(('POST', url), _Resp(200, {}))

    def get(url, **kw):
        calls.append(('GET', url, kw))
        return answers.get(('GET', url), _Resp(200, {}))

    monkeypatch.setattr(vendor.requests, 'post', post)
    monkeypatch.setattr(vendor.requests, 'get', get)
    return SimpleNamespace(calls=calls, answers=answers, settings=settings)


V2 = 'https://api.stripe.com/v2/core'
V1 = 'https://api.stripe.com/v1'


def test_default_is_v1(stripe):
    assert vendor.accounts_api() == 'v1'
    stripe.settings.STRIPE_ACCOUNTS_API = 'V2'
    assert vendor.accounts_api() == 'v2'


def test_v2_creates_a_recipient_with_express_dashboard(stripe):
    stripe.settings.STRIPE_ACCOUNTS_API = 'v2'
    stripe.answers[('POST', f'{V2}/accounts')] = _Resp(200, {'id': 'acct_v2new'})
    stripe.answers[('POST', f'{V2}/account_links')] = _Resp(200, {'url': 'https://connect.stripe.com/x'})
    d = _driver()
    assert stripe_connect.onboarding_link(d) == 'https://connect.stripe.com/x'
    assert d.stripe_account_id == 'acct_v2new' and d.stripe_onboarding_status == 'in_progress'

    (_, url, kw), (_, url2, kw2) = stripe.calls
    assert url == f'{V2}/accounts'
    body, headers = kw['json'], kw['headers']
    assert headers['Stripe-Version'] == '2026-10-28.endive'
    assert headers['Content-Type'] == 'application/json'
    assert headers['Idempotency-Key'] == 'sg-driver-account-30'
    assert body['dashboard'] == 'express'
    assert body['identity'] == {'country': 'us', 'entity_type': 'individual'}
    assert body['configuration']['recipient']['capabilities']['stripe_balance'][
        'stripe_transfers'] == {'requested': True}
    # Stripe refuses an Express dashboard unless the platform collects fees and losses.
    assert body['defaults']['responsibilities'] == {'fees_collector': 'application',
                                                    'losses_collector': 'application'}
    assert body['defaults']['currency'] == 'usd'
    assert body['metadata'] == {'driver_id': '30'}
    assert body['contact_email'] == 'driver@example.test'

    assert url2 == f'{V2}/account_links'
    link = kw2['json']
    assert link['account'] == 'acct_v2new'
    assert link['use_case']['type'] == 'account_onboarding'
    ob = link['use_case']['account_onboarding']
    assert ob['configurations'] == ['recipient']
    assert ob['refresh_url'] == ob['return_url'] == 'https://www.qa.example.test/driver/payouts?stripe=return'


def test_v1_still_makes_express_accounts(stripe):
    stripe.answers[('POST', f'{V1}/accounts')] = _Resp(200, {'id': 'acct_v1new'})
    stripe.answers[('POST', f'{V1}/account_links')] = _Resp(200, {'url': 'https://connect.stripe.com/v1'})
    d = _driver()
    assert stripe_connect.onboarding_link(d) == 'https://connect.stripe.com/v1'
    assert [c[1] for c in stripe.calls] == [f'{V1}/accounts', f'{V1}/account_links']


def test_a_v1_account_keeps_the_v1_link_after_the_switch(stripe):
    stripe.settings.STRIPE_ACCOUNTS_API = 'v2'
    stripe.answers[('GET', f'{V2}/accounts/acct_old')] = _Resp(200, {'id': 'acct_old', 'configuration': {}})
    stripe.answers[('POST', f'{V1}/account_links')] = _Resp(200, {'url': 'https://connect.stripe.com/v1'})
    assert stripe_connect.onboarding_link(_driver('acct_old')) == 'https://connect.stripe.com/v1'


def _v2(transfers, entries=()):
    return {'id': 'acct_v2', 'configuration': {'recipient': {'capabilities': {
        'stripe_balance': {'stripe_transfers': {'status': transfers}}}}},
            'requirements': {'entries': list(entries)}}


@pytest.mark.parametrize('transfers,entries,expected', [
    ('active', [], 'ready'),
    ('active', [{'minimum_deadline': {'status': 'currently_due'}}], 'in_progress'),
    ('active', [{'minimum_deadline': {'status': 'eventually_due'}}], 'ready'),
    ('pending', [], 'in_progress'),
    ('restricted', [{'minimum_deadline': {'status': 'past_due'}}], 'in_progress'),
    ('rejected', [], 'in_progress'),
])
def test_status_of_v2(transfers, entries, expected):
    assert stripe_connect.status_of_v2(_v2(transfers, entries)) == expected


def test_status_of_v2_before_anything_is_asked():
    assert stripe_connect.status_of_v2({'configuration': {'recipient': {}}}) == 'not_started'


def test_ready_also_needs_stripe_to_allow_payouts(stripe):
    stripe.settings.STRIPE_ACCOUNTS_API = 'v2'
    stripe.answers[('GET', f'{V2}/accounts/acct_v2')] = _Resp(200, _v2('active'))
    stripe.answers[('GET', f'{V1}/accounts/acct_v2')] = _Resp(200, {'payouts_enabled': False})
    d = _driver('acct_v2')
    assert stripe_connect.refresh_status(d) == 'in_progress'
    stripe.answers[('GET', f'{V1}/accounts/acct_v2')] = _Resp(200, {'payouts_enabled': True})
    assert stripe_connect.refresh_status(d) == 'ready'


def test_the_v2_read_asks_for_recipient_and_requirements(stripe):
    stripe.settings.STRIPE_ACCOUNTS_API = 'v2'
    stripe.answers[('GET', f'{V2}/accounts/acct_v2')] = _Resp(200, _v2('pending'))
    stripe_connect.refresh_status(_driver('acct_v2'))
    _, url, kw = stripe.calls[0]
    assert url == f'{V2}/accounts/acct_v2'
    assert kw['params'] == [('include[0]', 'configuration.recipient'), ('include[1]', 'requirements')]


def test_a_v1_account_keeps_v1_status_under_v2(stripe):
    stripe.settings.STRIPE_ACCOUNTS_API = 'v2'
    stripe.answers[('GET', f'{V2}/accounts/acct_old')] = _Resp(200, {'id': 'acct_old', 'configuration': {}})
    stripe.answers[('GET', f'{V1}/accounts/acct_old')] = _Resp(200, {
        'payouts_enabled': True, 'capabilities': {'transfers': 'active'}, 'details_submitted': True})
    assert stripe_connect.refresh_status(_driver('acct_old')) == 'ready'


def test_unknown_v2_account_starts_again(stripe):
    stripe.settings.STRIPE_ACCOUNTS_API = 'v2'
    stripe.answers[('GET', f'{V2}/accounts/acct_gone')] = _Resp(404, {'error': {'code': 'not_found'}})
    d = _driver('acct_gone')
    assert stripe_connect.refresh_status(d) == 'not_started' and d.stripe_account_id == ''


def test_stripe_silent_leaves_status_alone(stripe, monkeypatch):
    import requests
    stripe.settings.STRIPE_ACCOUNTS_API = 'v2'

    def boom(*a, **k):
        raise requests.ConnectionError()
    monkeypatch.setattr(vendor.requests, 'get', boom)
    d = _driver('acct_v2')
    d.stripe_onboarding_status = 'in_progress'
    assert stripe_connect.refresh_status(d) == 'in_progress'


def test_a_v2_refusal_is_reported_not_crashed(stripe):
    stripe.settings.STRIPE_ACCOUNTS_API = 'v2'
    stripe.answers[('POST', f'{V2}/accounts')] = _Resp(400, {'error': {'code': 'accounts_v2_access_blocked'}})
    with pytest.raises(stripe_connect.StripeRefused):
        stripe_connect.onboarding_link(_driver())
