"""US payouts through Stripe Connect Express (10 Nov). SaaradhiGo never collects a US driver's bank
details, tax ID or SSN: Stripe's hosted onboarding does, and we keep only the account id and
whether it can be paid.

With STRIPE_SECRET_KEY: an Express account (created once) and an account link. Without it, and
only where PAYOUT_FAKE_ALLOWED (QA), a fake account and a link to the site's own return page.
"""
import secrets

import requests
from django.conf import settings

API = 'https://api.stripe.com/v1'


class NotConfigured(Exception):
    pass


def _key():
    return getattr(settings, 'STRIPE_SECRET_KEY', '') or ''


def _fake_allowed():
    return bool(getattr(settings, 'PAYOUT_FAKE_ALLOWED', False))


def _site():
    return (getattr(settings, 'FRONTEND_URL', '') or '').rstrip('/')


def onboarding_link(driver, return_url=''):
    # Only back to our own site: a client-chosen URL would be an open redirect.
    back = return_url if return_url and _site() and return_url.startswith(_site() + '/') else         f'{_site()}/driver/payouts?stripe=return'
    if not _key():
        if not _fake_allowed():
            raise NotConfigured()
        if not driver.stripe_account_id:
            driver.stripe_account_id = 'acct_fake_' + secrets.token_hex(8)
        driver.stripe_onboarding_status = 'in_progress'
        driver.save(update_fields=['stripe_account_id', 'stripe_onboarding_status'])
        sep = '&' if '?' in back else '?'
        return f'{back}{sep}fake=1'
    auth = (_key(), '')
    if not driver.stripe_account_id:
        r = requests.post(f'{API}/accounts', auth=auth, timeout=15, data={
            'type': 'express', 'country': 'US', 'capabilities[transfers][requested]': 'true',
            'metadata[driver_id]': str(driver.pk)})
        r.raise_for_status()
        driver.stripe_account_id = r.json()['id']
    r = requests.post(f'{API}/account_links', auth=auth, timeout=15, data={
        'account': driver.stripe_account_id, 'type': 'account_onboarding',
        'refresh_url': back, 'return_url': back})
    r.raise_for_status()
    driver.stripe_onboarding_status = 'in_progress'
    driver.save(update_fields=['stripe_account_id', 'stripe_onboarding_status'])
    return r.json()['url']


def refresh_status(driver, fake_done=False):
    if not driver.stripe_account_id:
        return driver.stripe_onboarding_status
    if driver.stripe_account_id.startswith('acct_fake_'):
        if fake_done and _fake_allowed():
            driver.stripe_onboarding_status = 'ready'
            driver.save(update_fields=['stripe_onboarding_status'])
        return driver.stripe_onboarding_status
    if not _key():
        return driver.stripe_onboarding_status
    r = requests.get(f'{API}/accounts/{driver.stripe_account_id}', auth=(_key(), ''), timeout=15)
    if r.ok:
        acct = r.json()
        driver.stripe_onboarding_status = ('ready' if acct.get('payouts_enabled')
                                           else 'in_progress' if acct.get('details_submitted') is not None
                                           else 'not_started')
        driver.save(update_fields=['stripe_onboarding_status'])
    return driver.stripe_onboarding_status
