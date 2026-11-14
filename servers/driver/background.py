"""Driver background checks through a provider (14 Nov, USA from day 1; Idaho Code 49-3710(1)(b)-(c):
a local and national criminal check including the national sex offender registry, and a driving
record).

The provider is the owner's choice (Checkr is the recommendation); until it is named, only the
fake provider exists, and it runs only outside production. SaaradhiGo never stores the SSN: the
driver enters it in the provider's own form, reached from `start`.

Statuses arrive by webhook: pending, clear, consider (staff review in the console), suspended.
`failed` is what a reviewer decides on a `consider`. A driver whose check is not `clear` cannot be
approved or go online: `background_check` is a blocking checklist item in the market.

    BACKGROUND_CHECK_PROVIDERS = {"US": "fake"}       # settings; production: the real provider
    BACKGROUND_CHECK_WEBHOOK_SECRET                    # signs the webhook (HMAC-SHA256, hex)
"""
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

STATUSES = ('pending', 'clear', 'consider', 'suspended')
REVIEW_DECISIONS = ('clear', 'failed')


@dataclass
class Started:
    ref: str
    #: Where the driver enters their SSN and authorises the provider (the provider's own page).
    form_url: str


class Provider:
    name = ''

    def start(self, driver) -> Started:  # pragma: no cover - interface
        raise NotImplementedError

    def verify(self, body: bytes, signature: str) -> bool:
        secret = getattr(settings, 'BACKGROUND_CHECK_WEBHOOK_SECRET', '') or ''
        if not secret:
            return False
        good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, (signature or '').strip())

    def parse(self, body: bytes):
        """`(ref, status)` from a webhook body."""
        data = json.loads(body or b'{}')
        return str(data.get('ref') or ''), str(data.get('status') or '')


class FakeProvider(Provider):
    """QA only: no real check, no SSN form. A status is set by the QA endpoint or the webhook."""
    name = 'fake'

    def start(self, driver):
        return Started(ref=f'fake-{driver.id}-{int(timezone.now().timestamp())}', form_url='')


PROVIDERS = {'fake': FakeProvider}


def provider_for(country):
    name = (getattr(settings, 'BACKGROUND_CHECK_PROVIDERS', None) or {}).get(str(country or '').upper())
    if not name or name not in PROVIDERS:
        return None
    if name == 'fake' and getattr(settings, 'IS_PRODUCTION', True):
        return None  # never a fake check in production
    return PROVIDERS[name]()


def _country(driver):
    from .records import market_of

    return market_of(driver)[0]


def record_disclosure(driver):
    """The FCRA disclosure was shown on its own page (before any authorisation)."""
    if not driver.background_disclosure_at:
        driver.background_disclosure_at = timezone.now()
        driver.save(update_fields=['background_disclosure_at'])
    return driver


def authorise_and_start(driver):
    """The driver authorised the check after reading the disclosure: start it with the provider.
    Returns `Started`, or raises ValueError with a reason a driver can read."""
    if not driver.background_disclosure_at:
        raise ValueError('Read the background check disclosure first.')
    provider = provider_for(_country(driver))
    if provider is None:
        raise ValueError('Background checks are not available yet. We will tell you when they are.')
    started = provider.start(driver)
    now = timezone.now()
    driver.background_consent_at = driver.background_consent_at or now
    driver.background_check_provider = provider.name
    driver.background_check_ref = started.ref
    driver.background_check_status = 'pending'
    driver.save(update_fields=['background_consent_at', 'background_check_provider',
                               'background_check_ref', 'background_check_status'])
    return started


def apply_status(driver, status, *, actor=None, note=''):
    """Record a status from the provider (or a reviewer's decision on `consider`)."""
    from servers.admin_audit.services import record_admin_action

    if status not in STATUSES + REVIEW_DECISIONS:
        raise ValueError(f'Unknown background check status {status!r}.')
    before = driver.background_check_status
    if before == status:
        return driver
    driver.background_check_status = status
    fields = ['background_check_status']
    if status in ('clear', 'consider', 'failed', 'suspended'):
        driver.background_checked_at = timezone.now()
        fields.append('background_checked_at')
    driver.save(update_fields=fields)
    record_admin_action(None, action='background_check_status', target_type='driver',
                        target_id=str(driver.id), before={'status': before},
                        after={'status': status, 'provider': driver.background_check_provider,
                               'by': getattr(actor, 'email', '') or 'provider'},
                        reason=note or f'Background check {status}')
    if status in ('failed', 'suspended') and driver.approved:
        # Off the road at once; the console's suspension holds the reason.
        from .admin_drivers import operator_suspend

        operator_suspend(driver.id, reason_code='background_check',
                         note=note or f'Background check {status}', actor=actor)
    return driver


def handle_webhook(provider_name, body, signature):
    """`(http_status, message)`."""
    from .models import Driver

    provider_cls = PROVIDERS.get(provider_name)
    if provider_cls is None:
        return 404, 'Unknown provider.'
    provider = provider_cls()
    if not provider.verify(body, signature):
        return 401, 'Bad signature.'
    try:
        ref, status = provider.parse(body)
    except (ValueError, TypeError):
        return 400, 'Unreadable body.'
    if status not in STATUSES:
        return 400, 'Unknown status.'
    driver = Driver.objects.filter(background_check_provider=provider.name, background_check_ref=ref).first()
    if driver is None:
        return 404, 'No such check.'
    apply_status(driver, status)
    return 200, 'ok'


def recheck_due(now=None):
    """Drivers whose last check is older than the market's re-check period (yearly by default in
    Idaho). Started again with the provider; their status is pending until it answers."""
    from datetime import timedelta

    from servers.pricing.regulation import market_rules

    from .models import Driver
    from .records import market_of

    now = now or timezone.now()
    started = 0
    for driver in Driver.objects.filter(background_check_status='clear', background_checked_at__isnull=False):
        country, state = market_of(driver)
        months = market_rules(country or 'IN', state)['background_recheck_months'] or 0
        if not months or driver.background_checked_at > now - timedelta(days=30 * months):
            continue
        provider = provider_for(country)
        if provider is None:
            continue
        try:
            got = provider.start(driver)
        except Exception:  # noqa: BLE001 - one driver never stops the others
            logger.exception('background re-check failed to start driver=%s', driver.id)
            continue
        driver.background_check_ref = got.ref
        driver.background_check_provider = provider.name
        driver.save(update_fields=['background_check_ref', 'background_check_provider'])
        started += 1
    return started
