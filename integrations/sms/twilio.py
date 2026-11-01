"""Twilio: US SMS (Programmable Messaging REST API).

  POST https://api.twilio.com/2010-04-01/Accounts/<TWILIO_ACCOUNT_SID>/Messages.json
  basic auth (account sid, auth token); form fields To, Body, and MessagingServiceSid or From;
  reply 201 {"sid": "SM...", "status": "queued", ...}

SANDBOX ONLY, UNTIL THE OWNER CONFIRMS THE ACCOUNT. When the account exists, develop against
Twilio's documented TEST credentials (a test Account SID and test Auth Token from the console)
and the magic numbers: From +15005550006 is a valid test sender, To +15005550001 is rejected as
invalid, and +15005550009 simulates "cannot route". Test credentials never send a message and
cost nothing. They belong in the environment, never in this repository, and no value is written
here or in a test.

WEBHOOKS. Status callbacks (and inbound replies) are form-encoded and signed:
  X-Twilio-Signature = base64( HMAC-SHA1( auth token, URL + k1 + v1 + k2 + v2 ... ) )
with the form fields sorted by key and the URL exactly as configured in Twilio. The URL is built
from `NOTIFY_WEBHOOK_BASE_URL` when set (it is what sits behind a proxy), else from the request.
Compared in constant time.
"""

import base64
import hashlib
import hmac
import logging

import requests
from django.conf import settings

from servers.notifications.providers.base import (
    PermanentProviderError, Provider, SendResult, TransientProviderError, WebhookEvent, mask_for_log,
)

logger = logging.getLogger(__name__)

TIMEOUT = 10
_STATUS_MAP = {'delivered': 'delivered', 'sent': 'sent', 'undelivered': 'failed', 'failed': 'failed'}
_STOP_WORDS = {'stop', 'stopall', 'unsubscribe', 'cancel', 'end', 'quit'}
#: Twilio error codes that mean "do not retry".
_PERMANENT_CODES = {21211, 21214, 21408, 21610, 21612, 21614, 30003, 30005, 30006}


class TwilioSms(Provider):
    name = 'twilio'
    channels = ('sms',)

    def __init__(self):
        if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN):
            raise PermanentProviderError('Twilio credentials are not set.')
        if not (settings.TWILIO_MESSAGING_SERVICE_SID or settings.TWILIO_FROM_NUMBER):
            raise PermanentProviderError('Neither TWILIO_MESSAGING_SERVICE_SID nor '
                                         'TWILIO_FROM_NUMBER is set.')

    def send(self, outbound):
        form = {'To': outbound.to_phone, 'Body': outbound.body}
        if settings.TWILIO_MESSAGING_SERVICE_SID:
            form['MessagingServiceSid'] = settings.TWILIO_MESSAGING_SERVICE_SID
        else:
            form['From'] = settings.TWILIO_FROM_NUMBER
        callback = (getattr(settings, 'NOTIFY_WEBHOOK_BASE_URL', '') or '').rstrip('/')
        if callback:
            form['StatusCallback'] = f'{callback}/api/v1/notifications/webhooks/twilio/'
        url = (f'https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}'
               f'/Messages.json')
        try:
            response = requests.post(
                url, data=form, timeout=TIMEOUT,
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN))
        except requests.RequestException as exc:
            raise TransientProviderError(f'Twilio request failed: {type(exc).__name__}') from None
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientProviderError(f'Twilio answered HTTP {response.status_code}')
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            code = data.get('code')
            message = f'Twilio refused the message (HTTP {response.status_code}, code {code}): ' \
                      f'{mask_for_log(data.get("message", ""))}'
            if code in _PERMANENT_CODES or 400 <= response.status_code < 500:
                raise PermanentProviderError(message)
            raise TransientProviderError(message)
        sid = data.get('sid')
        if not sid:
            raise PermanentProviderError('Twilio accepted the request but returned no sid.')
        return SendResult(provider_message_id=sid, status='sent')

    @staticmethod
    def signature_for(url, params, token):
        """The expected X-Twilio-Signature for a form POST (exposed for the tests)."""
        payload = url + ''.join(f'{k}{params[k]}' for k in sorted(params))
        digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
        return base64.b64encode(digest).decode()

    @staticmethod
    def webhook_url(request):
        base = (getattr(settings, 'NOTIFY_WEBHOOK_BASE_URL', '') or '').rstrip('/')
        if base:
            return base + request.path
        return request.build_absolute_uri()

    @staticmethod
    def verify_webhook(request, path_token=''):
        token = getattr(settings, 'TWILIO_AUTH_TOKEN', '') or ''
        supplied = request.META.get('HTTP_X_TWILIO_SIGNATURE', '')
        if not token or not supplied:
            return False
        params = {k: request.POST.get(k, '') for k in request.POST.keys()}
        expected = TwilioSms.signature_for(TwilioSms.webhook_url(request), params, token)
        return hmac.compare_digest(supplied.encode(), expected.encode())

    @staticmethod
    def parse_webhook(request):
        sid = request.POST.get('MessageSid') or request.POST.get('SmsSid') or ''
        status = (request.POST.get('MessageStatus') or request.POST.get('SmsStatus') or '').lower()
        body = (request.POST.get('Body') or '').strip().lower()
        if request.POST.get('OptOutType', '').upper() == 'STOP' or body in _STOP_WORDS:
            return [WebhookEvent(kind='stop', phone=request.POST.get('From', ''),
                                 provider_message_id=sid, detail='inbound STOP')]
        mapped = _STATUS_MAP.get(status)
        if sid and mapped:
            return [WebhookEvent(kind='status', provider_message_id=sid, status=mapped,
                                 detail=mask_for_log(request.POST.get('ErrorCode', ''))[:100])]
        return []
