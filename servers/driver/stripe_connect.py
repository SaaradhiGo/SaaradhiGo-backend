"""US payouts through Stripe Connect Express (10 Nov). SaaradhiGo never collects a US driver's bank
details, tax ID or SSN: Stripe's hosted onboarding does, and we keep only the account id and
whether it can be paid.

With STRIPE_SECRET_KEY: an Express account (created once) and an account link. Without it, and
only where PAYOUT_FAKE_ALLOWED (QA), a fake account and a link to the site's own return page.
"""
import secrets

from django.conf import settings

from integrations.payouts import driver_onboarding as vendor


class NotConfigured(Exception):
    pass


def _key():
    return vendor.stripe_key()


def _fake_allowed():
    return bool(getattr(settings, 'PAYOUT_FAKE_ALLOWED', False))


def _site():
    return (getattr(settings, 'FRONTEND_URL', '') or '').rstrip('/')


def onboarding_link(driver, return_url=''):
    # Only back to our own site: a client-chosen URL would be an open redirect.
    ours = bool(return_url and _site() and return_url.startswith(_site() + '/'))
    back = return_url if ours else f'{_site()}/driver/payouts?stripe=return'
    if not _key():
        if not _fake_allowed():
            raise NotConfigured()
        if not driver.stripe_account_id:
            driver.stripe_account_id = 'acct_fake_' + secrets.token_hex(8)
        driver.stripe_onboarding_status = 'in_progress'
        driver.save(update_fields=['stripe_account_id', 'stripe_onboarding_status'])
        sep = '&' if '?' in back else '?'
        return f'{back}{sep}fake=1'
    if not driver.stripe_account_id:
        driver.stripe_account_id = vendor.stripe_express_account(driver.pk)
    url = vendor.stripe_account_link(driver.stripe_account_id, back)
    driver.stripe_onboarding_status = 'in_progress'
    driver.save(update_fields=['stripe_account_id', 'stripe_onboarding_status'])
    return url


def confirmed_status(driver):
    """The stored status, re-checked with Stripe at most every 5 minutes per driver."""
    from django.core.cache import cache

    if not driver.stripe_account_id or driver.stripe_account_id.startswith('acct_fake_') or not _key():
        return driver.stripe_onboarding_status
    # Not ready yet: ask every time (the driver may be back from Stripe); ready: at most every 5 min.
    if driver.stripe_onboarding_status != 'ready' or cache.add(f'stripe-status:{driver.pk}', 1, 300):
        try:
            refresh_status(driver)
        except Exception:  # noqa: BLE001 - the page still loads; the next view re-checks
            cache.delete(f'stripe-status:{driver.pk}')
    return driver.stripe_onboarding_status


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
    acct = vendor.stripe_account(driver.stripe_account_id)
    if acct is None:
        return driver.stripe_onboarding_status            # Stripe did not answer: unchanged
    if acct.get('_unknown_account'):
        # Stripe does not know this id (a seeded placeholder): start again, never "ready".
        driver.stripe_account_id, driver.stripe_onboarding_status = '', 'not_started'
        driver.save(update_fields=['stripe_account_id', 'stripe_onboarding_status'])
        return driver.stripe_onboarding_status
    driver.stripe_onboarding_status = status_of(acct)
    driver.save(update_fields=['stripe_onboarding_status'])
    return driver.stripe_onboarding_status


def status_of(acct):
    """Ready only when Stripe says the account can receive transfers and be paid out."""
    caps = acct.get('capabilities') or {}
    if acct.get('payouts_enabled') and caps.get('transfers') == 'active':
        return 'ready'
    return 'in_progress' if acct.get('details_submitted') is not None else 'not_started'
