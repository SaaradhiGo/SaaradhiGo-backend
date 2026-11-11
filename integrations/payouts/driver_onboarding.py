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
    """The account's JSON, or None when Stripe does not answer."""
    r = requests.get(f'{STRIPE_API}/accounts/{account_id}', auth=(stripe_key(), ''), timeout=15)
    return r.json() if r.ok else None


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
