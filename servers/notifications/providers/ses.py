"""AWS SES: email, delivery and bounce notifications through SNS.

SENDING uses the SESv2 API (`boto3` client `sesv2`, `send_email`) with credentials from the
SES_AWS_* settings only, never the platform's S3 credentials. SANDBOX SEMANTICS: while the SES
account is in the sandbox, SES refuses any recipient that is not a verified identity with
`MessageRejected` ("Email address is not verified"); that is a permanent failure for the
message, not a reason to retry. Throttling and service errors are transient.

WEBHOOKS arrive as SNS HTTP(S) posts (the body is JSON with content type text/plain). EVERY one
is signature-verified before anything is read from it:

  * SigningCertURL must be https and its host must be sns.<region>.amazonaws.com (or .cn);
  * the certificate is fetched once and cached;
  * the string to sign is built from the fields AWS documents for the message Type, and the
    RSA signature is checked with SHA1 (SignatureVersion 1) or SHA256 (SignatureVersion 2);
  * TopicArn must be one of SES_SNS_TOPIC_ARNS (fail closed when none is configured).

A SubscriptionConfirmation is LOGGED ONLY. The code never issues a GET to its SubscribeURL:
fetching a URL named by an inbound message is an SSRF and an auto-confirm of someone else's
topic. The operator confirms the subscription by hand from the AWS console (runbook step).
"""

import base64
import json
import logging
import re
from urllib.parse import urlparse

import requests
from django.conf import settings

from .base import (
    PermanentProviderError, Provider, SendResult, TransientProviderError, WebhookEvent,
)

logger = logging.getLogger(__name__)

_CERT_HOST = re.compile(r'^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$')
_CERT_CACHE = {}
_CERT_CACHE_SECONDS = 3600

_NOTIFICATION_FIELDS = ('Message', 'MessageId', 'Subject', 'Timestamp', 'TopicArn', 'Type')
_SUBSCRIPTION_FIELDS = ('Message', 'MessageId', 'SubscribeURL', 'Timestamp', 'Token', 'TopicArn',
                        'Type')
_TRANSIENT_CODES = {'Throttling', 'ThrottlingException', 'TooManyRequestsException',
                    'ServiceUnavailable', 'InternalFailure', 'RequestTimeout',
                    'LimitExceededException'}


class SesEmail(Provider):
    name = 'ses'
    channels = ('email',)

    def __init__(self):
        import boto3

        missing = [n for n in ('SES_AWS_ACCESS_KEY_ID', 'SES_AWS_SECRET_ACCESS_KEY',
                               'SES_AWS_REGION', 'SES_FROM_EMAIL') if not getattr(settings, n, '')]
        if missing:
            raise PermanentProviderError('SES settings are not set: ' + ', '.join(missing))
        self._client = boto3.client(
            'sesv2', region_name=settings.SES_AWS_REGION,
            aws_access_key_id=settings.SES_AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.SES_AWS_SECRET_ACCESS_KEY)

    def send(self, outbound):
        from botocore.exceptions import BotoCoreError, ClientError

        kwargs = {
            'FromEmailAddress': settings.SES_FROM_EMAIL,
            'Destination': {'ToAddresses': [outbound.to_email]},
            'Content': {'Simple': {
                'Subject': {'Data': outbound.subject or outbound.title or 'Notification',
                            'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': outbound.body, 'Charset': 'UTF-8'}}}},
        }
        if getattr(settings, 'SES_CONFIGURATION_SET', ''):
            kwargs['ConfigurationSetName'] = settings.SES_CONFIGURATION_SET
        try:
            reply = self._client.send_email(**kwargs)
        except ClientError as exc:
            code = (exc.response.get('Error') or {}).get('Code', '')
            text = f'SES refused the message ({code})'
            if code in _TRANSIENT_CODES:
                raise TransientProviderError(text) from None
            raise PermanentProviderError(text) from None
        except BotoCoreError as exc:
            raise TransientProviderError(f'SES request failed: {type(exc).__name__}') from None
        message_id = reply.get('MessageId')
        if not message_id:
            raise PermanentProviderError('SES accepted the request but returned no MessageId.')
        return SendResult(provider_message_id=message_id, status='sent')

    # --- webhooks (SNS) ------------------------------------------------------

    @staticmethod
    def verify_webhook(request, path_token=''):
        message = _sns_message(request)
        return message is not None and verify_sns_message(message)

    @staticmethod
    def parse_webhook(request):
        message = _sns_message(request)
        if message is None:
            return []
        kind = message.get('Type')
        if kind in ('SubscriptionConfirmation', 'UnsubscribeConfirmation'):
            # LOG ONLY. No GET to SubscribeURL, ever (see the module docstring).
            logger.warning(
                'ses_sns_subscription_confirmation_received', extra={
                    'event': 'ses_sns_subscription_confirmation_received',
                    'topic_arn': message.get('TopicArn', ''),
                    'note': 'Not confirmed automatically. Operator: confirm in the AWS console.'})
            return []
        if kind != 'Notification':
            return []
        try:
            inner = json.loads(message.get('Message') or '{}')
        except ValueError:
            return []
        event = str(inner.get('eventType') or inner.get('notificationType') or '').lower()
        mail = inner.get('mail') or {}
        message_id = mail.get('messageId', '')
        if event == 'delivery':
            return [WebhookEvent(kind='status', provider_message_id=message_id,
                                 status='delivered')]
        if event == 'bounce':
            bounce = inner.get('bounce') or {}
            permanent = str(bounce.get('bounceType', '')).lower() == 'permanent'
            events = [WebhookEvent(kind='status', provider_message_id=message_id, status='failed',
                                   detail=f'bounce:{bounce.get("bounceType", "")}'[:100])]
            if permanent:
                events.append(WebhookEvent(kind='bounce', provider_message_id=message_id,
                                           detail='permanent bounce'))
            return events
        if event == 'complaint':
            return [WebhookEvent(kind='complaint', provider_message_id=message_id,
                                 detail='recipient complaint'),
                    WebhookEvent(kind='status', provider_message_id=message_id, status='failed',
                                 detail='complaint')]
        return []


def _sns_message(request):
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def cert_url_ok(url):
    """https on an sns.<region>.amazonaws.com host, a .pem path, no credentials or odd port."""
    try:
        parsed = urlparse(str(url or ''))
    except ValueError:
        return False
    return (parsed.scheme == 'https' and bool(parsed.hostname)
            and bool(_CERT_HOST.match(parsed.hostname)) and parsed.path.endswith('.pem')
            and parsed.username is None and parsed.port in (None, 443))


def string_to_sign(message):
    fields = _NOTIFICATION_FIELDS if message.get('Type') == 'Notification' else _SUBSCRIPTION_FIELDS
    parts = []
    for key in fields:
        if key == 'Subject' and key not in message:
            continue
        if key not in message:
            return None
        parts.append(f'{key}\n{message[key]}\n')
    return ''.join(parts)


def _certificate(url):
    now_cached = _CERT_CACHE.get(url)
    if now_cached is not None:
        return now_cached
    from cryptography import x509

    response = requests.get(url, timeout=5)
    response.raise_for_status()
    cert = x509.load_pem_x509_certificate(response.content)
    _CERT_CACHE[url] = cert
    return cert


def verify_sns_message(message):
    """True only for an authentic SNS message from an allowed topic."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        if message.get('Type') not in ('Notification', 'SubscriptionConfirmation',
                                       'UnsubscribeConfirmation'):
            return False
        if not cert_url_ok(message.get('SigningCertURL')):
            logger.warning('ses_sns_rejected reason=bad_cert_url')
            return False
        version = str(message.get('SignatureVersion', ''))
        algorithm = {'1': hashes.SHA1, '2': hashes.SHA256}.get(version)
        if algorithm is None:
            return False
        topics = list(getattr(settings, 'SES_SNS_TOPIC_ARNS', []) or [])
        if message.get('Type') == 'Notification' and message.get('TopicArn') not in topics:
            # Fail closed: with no allowlist configured no notification is accepted.
            logger.warning('ses_sns_rejected reason=topic_not_allowed')
            return False
        if topics and message.get('TopicArn') not in topics:
            logger.warning('ses_sns_rejected reason=topic_not_allowed')
            return False
        signed = string_to_sign(message)
        if signed is None:
            return False
        signature = base64.b64decode(message.get('Signature', ''))
        public_key = _certificate(message['SigningCertURL']).public_key()
        public_key.verify(signature, signed.encode('utf-8'), padding.PKCS1v15(), algorithm())
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError, requests.RequestException):
        logger.warning('ses_sns_rejected reason=signature')
        return False
    except Exception:  # noqa: BLE001 - an unreadable certificate is "not authentic"
        logger.warning('ses_sns_rejected reason=verify_error')
        return False
