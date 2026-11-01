"""MSG91: India SMS (Flow API) and WhatsApp (outbound template message).

DOCUMENTED ENDPOINTS (to be verified against the MSG91 sandbox once the owner confirms the
account; nothing here has ever been called live):

  SMS       POST https://control.msg91.com/api/v5/flow/
            header  authkey: <MSG91_AUTH_KEY>
            body    {"template_id": <flow id>, "short_url": "0",
                     "recipients": [{"mobiles": "919876543210", "<var>": "<value>", ...}]}
            reply   {"type": "success", "message": "<request id>"}
            The flow carries the DLT template id and the sender id; the flow's variables are
            named exactly like the template's placeholders, which is what the 1:1 DLT mapping
            rule guarantees.

  WhatsApp  POST https://control.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/bulk/
            header  authkey
            body    {"integrated_number": <MSG91_WHATSAPP_NUMBER>, "content_type": "template",
                     "payload": {"messaging_product": "whatsapp", "type": "template",
                       "template": {"name": ..., "namespace": ..., "language": {...},
                         "to_and_components": [{"to": ["9198..."], "components": {...}}]}}}

WEBHOOKS. MSG91 does not sign its callbacks. They are authenticated by a secret token
(`MSG91_WEBHOOK_SECRET`) in the `X-Webhook-Token` header or in the URL path
(`/webhooks/msg91/<token>/`), compared in constant time. The payload shapes below follow MSG91's
delivery-report format as documented and are parsed tolerantly; confirm them in the sandbox.
"""

import hmac
import json
import logging

import requests
from django.conf import settings

from servers.notifications.providers.base import (
    PermanentProviderError, Provider, SendResult, TransientProviderError, WebhookEvent, mask_for_log,
)

logger = logging.getLogger(__name__)

SMS_URL = 'https://control.msg91.com/api/v5/flow/'
WHATSAPP_URL = 'https://control.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/bulk/'
TIMEOUT = 10

#: MSG91 delivery report codes / words. 1 = delivered; 2 = failed; 9 = NDNC reject; 16 = rejected.
_DELIVERED = {'1', 'delivered', 'success', 'read'}
_FAILED = {'2', '9', '16', 'failed', 'undelivered', 'rejected', 'expired', 'ndnc', 'blocked'}
_STOP_WORDS = {'stop', 'stopall', 'unsubscribe', 'cancel', 'end', 'quit'}


def _digits(phone):
    return ''.join(ch for ch in str(phone or '') if ch.isdigit())


def _post(url, payload):
    try:
        response = requests.post(
            url, json=payload, timeout=TIMEOUT,
            headers={'authkey': settings.MSG91_AUTH_KEY, 'accept': 'application/json',
                     'content-type': 'application/json'})
    except requests.RequestException as exc:
        raise TransientProviderError(f'MSG91 request failed: {type(exc).__name__}') from None
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientProviderError(f'MSG91 answered HTTP {response.status_code}')
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or str(data.get('type', '')).lower() == 'error':
        raise PermanentProviderError(
            f'MSG91 refused the message (HTTP {response.status_code}): '
            f'{mask_for_log(data.get("message") or data.get("errors") or "")}')
    return data


class Msg91Sms(Provider):
    name = 'msg91'
    channels = ('sms',)

    def __init__(self):
        if not settings.MSG91_AUTH_KEY:
            raise PermanentProviderError('MSG91_AUTH_KEY is not set.')

    def send(self, outbound):
        reg = outbound.registration or {}
        template_id = reg.get('provider_template_id')
        if not template_id:
            raise PermanentProviderError('No MSG91 flow id is registered for this template.')
        recipient = {'mobiles': _digits(outbound.to_phone)}
        # Flow variables carry the placeholder names, in body order.
        for name, value in zip(outbound.data.get('variable_names') or [], outbound.values, strict=False):
            recipient[name] = value
        data = _post(SMS_URL, {'template_id': template_id, 'short_url': '0',
                               'recipients': [recipient]})
        request_id = str(data.get('message') or '')
        if not request_id:
            raise PermanentProviderError('MSG91 accepted the request but returned no id.')
        return SendResult(provider_message_id=request_id, status='sent')

    @staticmethod
    def verify_webhook(request, path_token=''):
        return verify_token(request, path_token)

    @staticmethod
    def parse_webhook(request):
        return parse_report(request)


class Msg91WhatsApp(Provider):
    name = 'msg91'
    channels = ('whatsapp',)

    def __init__(self):
        if not settings.MSG91_AUTH_KEY:
            raise PermanentProviderError('MSG91_AUTH_KEY is not set.')

    def send(self, outbound):
        reg = outbound.registration or {}
        if not reg.get('whatsapp_template_name'):
            raise PermanentProviderError('No WhatsApp template name is registered.')
        components = {f'body_{i}': {'type': 'text', 'value': v}
                      for i, v in enumerate(outbound.values, start=1)}
        payload = {
            'integrated_number': settings.MSG91_WHATSAPP_NUMBER,
            'content_type': 'template',
            'payload': {
                'messaging_product': 'whatsapp', 'type': 'template',
                'template': {
                    'name': reg['whatsapp_template_name'],
                    'namespace': reg.get('whatsapp_namespace', ''),
                    'language': {'code': 'en', 'policy': 'deterministic'},
                    'to_and_components': [{'to': [_digits(outbound.to_phone)],
                                           'components': components}],
                },
            },
        }
        data = _post(WHATSAPP_URL, payload)
        request_id = str(data.get('request_id') or data.get('message') or '')
        if not request_id:
            raise PermanentProviderError('MSG91 accepted the request but returned no id.')
        return SendResult(provider_message_id=request_id, status='sent')

    @staticmethod
    def verify_webhook(request, path_token=''):
        return verify_token(request, path_token)

    @staticmethod
    def parse_webhook(request):
        return parse_report(request)


def verify_token(request, path_token=''):
    """Constant-time check of the shared secret (header, else the path segment)."""
    secret = getattr(settings, 'MSG91_WEBHOOK_SECRET', '') or ''
    if not secret:
        return False   # fail closed: with no secret configured nothing is authentic
    supplied = request.META.get('HTTP_X_WEBHOOK_TOKEN', '') or path_token or ''
    return hmac.compare_digest(supplied.encode(), secret.encode())


def _status_of(raw):
    word = str(raw).strip().lower()
    if word in _DELIVERED:
        return 'delivered'
    if word in _FAILED:
        return 'failed'
    return ''


def parse_report(request):
    """MSG91 delivery reports and inbound replies -> WebhookEvents. Unknown shapes -> []."""
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    events = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Inbound reply (STOP): {"mobile"/"sender": "...", "text"/"message": "STOP"}.
        text = str(item.get('text') or item.get('keyword') or item.get('incoming_message') or '')
        phone = str(item.get('mobile') or item.get('sender') or item.get('from') or '')
        if text.strip().lower() in _STOP_WORDS and phone:
            events.append(WebhookEvent(kind='stop', phone=phone, detail='inbound STOP'))
            continue
        reports = item.get('report') if isinstance(item.get('report'), list) else [item]
        request_id = str(item.get('requestId') or item.get('request_id') or item.get('UUID') or '')
        for report in reports:
            if not isinstance(report, dict):
                continue
            status = _status_of(report.get('status') or report.get('Status') or '')
            rid = str(report.get('requestId') or report.get('request_id') or request_id)
            if rid and status:
                events.append(WebhookEvent(
                    kind='status', provider_message_id=rid, status=status,
                    detail=mask_for_log(report.get('desc') or report.get('reason') or '')[:200]))
    return events
