import logging

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


def _webhook_secret():
    # Cashfree PG webhooks are signed with the client secret key, so it stands in for an unset
    # CASHFREE_WEBHOOK_SECRET (see integrations.payments.cashfree.webhook_secret).
    return (getattr(settings, 'CASHFREE_WEBHOOK_SECRET', None)
            or getattr(settings, 'CASHFREE_SECRET_KEY', None))


def _configured_names():
    """Provider names selected for any country (payments or payouts), from configuration."""
    names = set()
    for table in ('PAYMENT_PROVIDERS', 'PAYOUT_PROVIDERS'):
        for name in (getattr(settings, table, None) or {}).values():
            if name:
                names.add(str(name).strip().lower())
    return names


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.payments'

    def ready(self):
        # A non-DEBUG deployment whose SELECTED provider is Cashfree but has no webhook secret would
        # accept any forged "payment success" event from the public internet. Refuse to start so the
        # operator sees it immediately. Cashfree is configured nowhere by default (India uses
        # Razorpay), so this guard now binds only when a table selects Cashfree explicitly.
        # Razorpay has the same property but must be able to BOOT before its keys exist: its
        # webhooks fail closed (400) while RAZORPAY_WEBHOOK_SECRET / RAZORPAYX_WEBHOOK_SECRET are
        # unset, and the integrations page shows that as a warning.
        names = _configured_names()
        if 'cashfree' in names and not settings.DEBUG and not _webhook_secret():
            raise ImproperlyConfigured(
                "CASHFREE_WEBHOOK_SECRET (or CASHFREE_SECRET_KEY) is required when Cashfree is a "
                "configured provider and DEBUG=False. Refusing to start because webhook signature "
                "verification would silently accept any payload."
            )

        if 'razorpay' in names:
            for setting in ('RAZORPAY_WEBHOOK_SECRET', 'RAZORPAYX_WEBHOOK_SECRET'):
                if not getattr(settings, setting, ''):
                    logger.warning(
                        "%s is not set; the matching Razorpay webhook rejects every delivery "
                        "(fails closed) until it is.", setting)
