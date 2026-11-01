"""AWS SNS as a push provider (SNS -> FCM / APNs). SANDBOX-ONLY until the owner confirms.

DESIGN
  * One SNS platform application per platform, by ARN from settings BY NAME:
    SNS_PLATFORM_APPLICATION_ARN_FCM (Android, and iOS through FCM) and
    SNS_PLATFORM_APPLICATION_ARN_APNS (iOS direct). `outbound.data['platform']` picks one
    (`fcm` by default, or `apns`).
  * ENDPOINT REGISTRATION. `register_endpoint(platform, device_token)` calls
    `create_platform_endpoint`, which SNS makes idempotent per (application, token). `send`
    registers on the way (no stored endpoint ARN, so no migration), then publishes to the
    endpoint with a per-platform JSON payload.
  * Exercised only in tests today (botocore Stubber). It never calls AWS unless NOTIFY_LIVE_SEND
    and SNS_LIVE_CONFIRMED are both true (see `integrations.aws_sns`).
"""

import json

from django.conf import settings

from integrations import aws_sns
from servers.notifications.providers.base import PermanentProviderError, Provider, SendResult

#: platform -> (settings name of the application ARN, SNS payload key)
PLATFORMS = {
    'fcm': ('SNS_PLATFORM_APPLICATION_ARN_FCM', 'GCM'),
    'apns': ('SNS_PLATFORM_APPLICATION_ARN_APNS', 'APNS'),
}


def application_arn(platform):
    try:
        setting, _ = PLATFORMS[platform]
    except KeyError:
        raise PermanentProviderError(f'Unknown push platform {platform!r}.') from None
    arn = getattr(settings, setting, '')
    if not arn:
        raise PermanentProviderError(f'{setting} is not set.')
    return arn


class SnsPush(Provider):
    name = 'sns'
    channels = ('push',)

    def __init__(self):
        self._client = aws_sns.client()

    def register_endpoint(self, platform, device_token):
        """The endpoint ARN for a device token (created on first sight, then reused)."""
        if not device_token:
            raise PermanentProviderError('The recipient has no push token.')
        reply = aws_sns.call(self._client.create_platform_endpoint, 'endpoint registration',
                             PlatformApplicationArn=application_arn(platform),
                             Token=device_token)
        arn = reply.get('EndpointArn')
        if not arn:
            raise PermanentProviderError('SNS returned no EndpointArn.')
        return arn

    def send(self, outbound):
        platform = (outbound.data or {}).get('platform') or 'fcm'
        endpoint = self.register_endpoint(platform, outbound.push_token)
        data = {str(k): str(v) for k, v in (outbound.data or {}).items()
                if k not in ('variable_names', 'platform')}
        data.update({'notification_message_id': str(outbound.message_id),
                     'category': outbound.category})
        if platform == 'apns':
            inner = {'aps': {'alert': {'title': outbound.title or '', 'body': outbound.body}},
                     **data}
        else:
            inner = {'notification': {'title': outbound.title or '', 'body': outbound.body},
                     'data': data}
        key = PLATFORMS[platform][1]
        reply = aws_sns.call(
            self._client.publish, 'push', TargetArn=endpoint, MessageStructure='json',
            Message=json.dumps({'default': outbound.body, key: json.dumps(inner)}))
        message_id = reply.get('MessageId')
        if not message_id:
            raise PermanentProviderError('SNS accepted the request but returned no MessageId.')
        return SendResult(provider_message_id=message_id, status='sent')
