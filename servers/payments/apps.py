import logging

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.payments'

    def ready(self):
        # A non-DEBUG deployment without the Cashfree webhook secret would accept
        # any forged "payment success" event from the public internet. Refuse to
        # start so the operator sees the misconfiguration immediately instead of
        # discovering it after a fraudulent payout.
        if not settings.DEBUG and not getattr(settings, 'CASHFREE_WEBHOOK_SECRET', None):
            raise ImproperlyConfigured(
                "CASHFREE_WEBHOOK_SECRET is required when DEBUG=False. "
                "Refusing to start because webhook signature verification would "
                "silently accept any payload."
            )

        if settings.DEBUG and not getattr(settings, 'CASHFREE_WEBHOOK_SECRET', None):
            logger.warning(
                "CASHFREE_WEBHOOK_SECRET is not set; webhook signature checks "
                "will fail closed. Set it in your .env to test the webhook path."
            )
