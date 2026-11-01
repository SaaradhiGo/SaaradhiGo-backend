"""AWS SNS as an SMS provider (alternative adapter; SANDBOX-ONLY until the owner confirms).

  sns.publish(PhoneNumber=<E.164>, Message=<body>, MessageAttributes={SMSType, SenderID?})

Selected per country with NOTIFY_PROVIDERS (`{"sms": {"US": "sns"}}`). It never calls AWS unless
NOTIFY_LIVE_SEND and SNS_LIVE_CONFIRMED are both true (see `integrations.aws_sns`). There is no
per-message delivery webhook (delivery status needs CloudWatch delivery-status logging, a go-live
item), so a message is `sent` once SNS accepts it.
"""

from django.conf import settings

from integrations import aws_sns
from servers.notifications.providers.base import PermanentProviderError, Provider, SendResult


class SnsSms(Provider):
    name = 'sns'
    channels = ('sms',)

    def __init__(self):
        self._client = aws_sns.client()

    def send(self, outbound):
        if not outbound.to_phone:
            raise PermanentProviderError('The recipient has no phone number.')
        attributes = {'AWS.SNS.SMS.SMSType': {
            'DataType': 'String',
            'StringValue': 'Promotional' if outbound.category == 'promotional' else 'Transactional'}}
        sender = getattr(settings, 'SNS_SMS_SENDER_ID', '')
        if sender:
            attributes['AWS.SNS.SMS.SenderID'] = {'DataType': 'String', 'StringValue': sender}
        reply = aws_sns.call(self._client.publish, 'SMS', PhoneNumber=outbound.to_phone,
                             Message=outbound.body, MessageAttributes=attributes)
        message_id = reply.get('MessageId')
        if not message_id:
            raise PermanentProviderError('SNS accepted the request but returned no MessageId.')
        return SendResult(provider_message_id=message_id, status='sent')
