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


class StripeRefused(Exception):
    """Stripe answered with an error when creating the account or the onboarding link."""


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
    import requests
    v2 = vendor.accounts_api() == 'v2'
    made_before = bool(driver.stripe_account_id)
    try:
        if not driver.stripe_account_id:
            if v2:
                user = getattr(driver, 'user_id', None)
                driver.stripe_account_id = vendor.stripe_v2_recipient_account(
                    driver.pk, email=getattr(user, 'email', '') or '',
                    name=getattr(user, 'full_name', '') or '')
            else:
                driver.stripe_account_id = vendor.stripe_express_account(driver.pk)
        # A v1 account onboards through the v1 link either way: v2 links need a v2 configuration.
        if v2 and not (made_before and _made_with_v1(driver)):
            url = vendor.stripe_v2_account_link(driver.stripe_account_id, back)
        else:
            url = vendor.stripe_account_link(driver.stripe_account_id, back)
    except requests.HTTPError as exc:
        body = getattr(exc.response, 'text', '') or ''
        raise StripeRefused(body[:300] or str(exc)) from None
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
    if vendor.accounts_api() == 'v2':
        acct = vendor.stripe_v2_account(driver.stripe_account_id)
        if acct is None:
            return driver.stripe_onboarding_status
        if acct.get('_unknown_account') or (acct.get('configuration') or {}).get('recipient'):
            return _refresh_v2(driver, acct)
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


def _made_with_v1(driver):
    """An account made before the switch (driver 30's acct_1UcCaG…) has the v1 `transfers`
    capability and no recipient configuration: it keeps the v1 link and status."""
    acct = vendor.stripe_v2_account(driver.stripe_account_id)
    if not isinstance(acct, dict) or acct.get('_unknown_account'):
        return False
    return not ((acct.get('configuration') or {}).get('recipient'))


def _refresh_v2(driver, acct):
    if acct.get('_unknown_account'):
        driver.stripe_account_id, driver.stripe_onboarding_status = '', 'not_started'
        driver.save(update_fields=['stripe_account_id', 'stripe_onboarding_status'])
        return driver.stripe_onboarding_status
    status = status_of_v2(acct)
    if status == 'ready':
        # v2 has no payouts status for a recipient: Stripe's v1 view of the same account still
        # says whether it can be paid out, so "ready" means exactly what it meant on v1.
        v1 = vendor.stripe_account(driver.stripe_account_id)
        if v1 is None:
            return driver.stripe_onboarding_status
        if not (isinstance(v1, dict) and v1.get('payouts_enabled')):
            status = 'in_progress'
    driver.stripe_onboarding_status = status
    driver.save(update_fields=['stripe_onboarding_status'])
    return status


def status_of_v2(acct):
    """From a v2 account: ready when the recipient's stripe_transfers capability is active and
    nothing is currently due; not_started when nothing has been asked yet."""
    rec = ((acct.get('configuration') or {}).get('recipient') or {})
    transfers = (((rec.get('capabilities') or {}).get('stripe_balance') or {})
                 .get('stripe_transfers') or {})
    entries = ((acct.get('requirements') or {}).get('entries') or [])
    due = [e for e in entries
           if ((e.get('minimum_deadline') or {}).get('status') in ('currently_due', 'past_due'))]
    if transfers.get('status') == 'active' and not due:
        return 'ready'
    if transfers.get('status') in ('rejected', 'unsupported'):
        return 'in_progress'
    return 'in_progress' if (transfers or entries) else 'not_started'


def status_of(acct):
    """Ready only when Stripe says the account can receive transfers and be paid out."""
    caps = acct.get('capabilities') or {}
    if acct.get('payouts_enabled') and caps.get('transfers') == 'active':
        return 'ready'
    return 'in_progress' if acct.get('details_submitted') is not None else 'not_started'
