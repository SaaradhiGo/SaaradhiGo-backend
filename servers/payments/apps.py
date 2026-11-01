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


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.payments'

    def ready(self):
        # A non-DEBUG deployment without the Cashfree webhook secret would accept
        # any forged "payment success" event from the public internet. Refuse to
        # start so the operator sees the misconfiguration immediately instead of
        # discovering it after a fraudulent payout.
        if not settings.DEBUG and not _webhook_secret():
            raise ImproperlyConfigured(
                "CASHFREE_WEBHOOK_SECRET (or CASHFREE_SECRET_KEY) is required when DEBUG=False. "
                "Refusing to start because webhook signature verification would "
                "silently accept any payload."
            )

        if settings.DEBUG and not _webhook_secret():
            logger.warning(
                "CASHFREE_WEBHOOK_SECRET is not set; webhook signature checks "
                "will fail closed. Set it in your .env to test the webhook path."
            )
