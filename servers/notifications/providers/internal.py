"""The two providers that never leave the platform: `stub` and `in_app`."""

import logging
import uuid

from .base import Provider, SendResult

logger = logging.getLogger(__name__)


class StubProvider(Provider):
    """Records and sends nothing. The default for every external channel."""

    name = 'stub'
    channels = ('sms', 'whatsapp', 'email', 'push', 'in_app')
    external = False

    def send(self, outbound):
        # The message row IS the record. The log line carries ids only, never a contact.
        logger.info('notification_stub_send', extra={
            'event': 'notification_stub_send', 'message_id': outbound.message_id,
            'channel': outbound.channel, 'user_id': outbound.user_id})
        return SendResult(provider_message_id=f'stub-{uuid.uuid4().hex[:16]}', status='sent')


class InAppProvider(Provider):
    """Writes the in-app row (`rider.Notification`) the apps already list."""

    name = 'in_app'
    channels = ('in_app',)
    external = False

    def send(self, outbound):
        from servers.rider.models import Notification

        row = Notification.objects.create(
            user_id_id=outbound.user_id, title=(outbound.title or outbound.subject or '')[:256],
            message=outbound.body,
            notif_type='marketing' if outbound.category == 'promotional' else 'system',
            data={'notification_message_id': outbound.message_id, **(outbound.data or {})})
        return SendResult(provider_message_id=f'inapp-{row.pk}', status='delivered')
