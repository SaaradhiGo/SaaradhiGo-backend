"""Vendor calls for a driver's payout details (10 Nov): Stripe Connect Express onboarding (USA),
RazorpayX bank-account validation and the IFSC directory (India). Business code
(servers.driver.profile_views, servers.driver.stripe_connect) calls these and never a vendor host.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
STRIPE_API = 'https://api.stripe.com/v1'
IFSC_API = 'https://ifsc.razorpay.com'


def stripe_key():
    return getattr(settings, 'STRIPE_SECRET_KEY', '') or ''


def stripe_express_account(driver_id):
    """A new Express account id."""
    r = requests.post(f'{STRIPE_API}/accounts', auth=(stripe_key(), ''), timeout=15, data={
        'type': 'express', 'country': 'US', 'capabilities[transfers][requested]': 'true',
        'metadata[driver_id]': str(driver_id)})
    r.raise_for_status()
    return r.json()['id']


def stripe_account_link(account_id, back_url):
    r = requests.post(f'{STRIPE_API}/account_links', auth=(stripe_key(), ''), timeout=15, data={
        'account': account_id, 'type': 'account_onboarding', 'refresh_url': back_url, 'return_url': back_url})
    r.raise_for_status()
    return r.json()['url']


def stripe_account(account_id):
    """The account's JSON; {'_unknown_account': True} when Stripe says no such account (or not
    ours); None when Stripe does not answer."""
    try:
        r = requests.get(f'{STRIPE_API}/accounts/{account_id}', auth=(stripe_key(), ''), timeout=15)
    except requests.RequestException:
        return None
    if r.ok:
        return r.json()
    code = ((r.json() if r.content else {}).get('error') or {}).get('code') if r.status_code in (400, 403, 404) else None
    if code in ('account_invalid', 'resource_missing'):
        return {'_unknown_account': True}
    return None


# -- Stripe Accounts v2 (launch round 3) ---------------------------------------------------------
# Stripe steers new Connect platforms to /v2/core/accounts. Behind STRIPE_ACCOUNTS_API=v2; v1 stays
# the default and the way back. Fields checked against docs.stripe.com/api/v2/core/accounts and
# /api/v2/core/account-links on 16 Nov 2026. Payouts are unchanged: POST /v1/transfers to the same
# acct_ id (the recipient's `stripe_transfers` capability is Stripe's replacement for v1 `transfers`).
STRIPE_API_V2 = 'https://api.stripe.com/v2'


def accounts_api():
    return 'v2' if (getattr(settings, 'STRIPE_ACCOUNTS_API', 'v1') or 'v1').lower() == 'v2' else 'v1'


def _v2_headers(idempotency_key=None):
    h = {'Authorization': f'Bearer {stripe_key()}', 'Content-Type': 'application/json',
         'Stripe-Version': getattr(settings, 'STRIPE_V2_API_VERSION', '') or '2026-10-28.endive'}
    if idempotency_key:
        h['Idempotency-Key'] = idempotency_key
    return h


def v2_recipient_body(driver_id, email='', name=''):
    """An individual US recipient that receives /v1/transfers, with the Express dashboard (as v1).

    Stripe: an Express dashboard needs the platform as both fees and losses collector."""
    body = {
        'dashboard': 'express',
        'identity': {'country': 'us', 'entity_type': 'individual'},
        'configuration': {'recipient': {'capabilities': {
            'stripe_balance': {'stripe_transfers': {'requested': True}}}}},
        'defaults': {'currency': 'usd',
                     'responsibilities': {'fees_collector': 'application',
                                          'losses_collector': 'application'}},
        'include': ['configuration.recipient', 'requirements'],
        'metadata': {'driver_id': str(driver_id)},
    }
    if email:
        body['contact_email'] = email
    if name:
        body['display_name'] = name
    return body


def stripe_v2_recipient_account(driver_id, email='', name=''):
    """A new v2 account id. One idempotency key per driver: a retry never makes a second account."""
    r = requests.post(f'{STRIPE_API_V2}/core/accounts', timeout=15,
                      headers=_v2_headers(f'sg-driver-account-{driver_id}'),
                      json=v2_recipient_body(driver_id, email, name))
    r.raise_for_status()
    return r.json()['id']


def stripe_v2_account_link(account_id, back_url):
    r = requests.post(f'{STRIPE_API_V2}/core/account_links', timeout=15, headers=_v2_headers(), json={
        'account': account_id,
        'use_case': {'type': 'account_onboarding', 'account_onboarding': {
            'configurations': ['recipient'], 'refresh_url': back_url, 'return_url': back_url}}})
    r.raise_for_status()
    return r.json()['url']


def stripe_v2_account(account_id):
    """The v2 account with its recipient configuration and requirements. Same answers as
    stripe_account: {'_unknown_account': True} or None."""
    try:
        r = requests.get(f'{STRIPE_API_V2}/core/accounts/{account_id}', timeout=15, headers=_v2_headers(),
                         params=[('include[0]', 'configuration.recipient'), ('include[1]', 'requirements')])
    except requests.RequestException:
        return None
    if r.ok:
        return r.json()
    code = ((r.json() if r.content else {}).get('error') or {}).get('code') if r.status_code in (400, 403, 404) else None
    if r.status_code == 404 or code in ('not_found', 'account_invalid', 'resource_missing',
                                         'v1_account_instead_of_v2_account'):
        return {'_unknown_account': True}
    return None


def ifsc_lookup(code):
    """`{bank, branch, city}` from the public IFSC directory, {} when unknown, None when unreachable."""
    try:
        r = requests.get(f'{IFSC_API}/{code}', timeout=6)
    except Exception:  # noqa: BLE001
        logger.warning('ifsc lookup failed')
        return None
    data = r.json() if r.status_code == 200 else {}
    return {'bank': data.get('BANK') or '', 'branch': data.get('BRANCH') or '',
            'city': data.get('CITY') or ''} if data.get('BANK') else {}


def validate_bank_account(holder_name, ifsc, account_number):
    """RazorpayX fund-account validation (Rs 1 credit and a name match):
    `('verified', registered_name)`, `('failed', '')` or `('pending', '')` while it is running."""
    from .razorpayx import RazorpayXGateway, account_number as source_account

    gateway = RazorpayXGateway()
    try:
        _status, data = gateway._call('POST', '/fund_accounts/validations', json={
            'account_number': source_account(), 'amount': 100, 'currency': 'INR',
            'fund_account': {'account_type': 'bank_account',
                             'bank_account': {'name': holder_name, 'ifsc': ifsc, 'account_number': account_number},
                             'contact': {'name': holder_name}}})
    except Exception:  # noqa: BLE001
        logger.warning('bank validation request failed')
        return 'pending', ''
    results = (data or {}).get('results') or {}
    if (data or {}).get('status') == 'completed':
        if results.get('account_status') == 'active':
            return 'verified', results.get('registered_name') or ''
        return 'failed', ''
    return 'pending', ''
