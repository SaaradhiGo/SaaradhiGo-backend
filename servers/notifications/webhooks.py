"""Provider webhooks: POST /api/v1/notifications/webhooks/<provider>/

EVERY CALLBACK IS AUTHENTICATED BEFORE ANYTHING IS READ FROM IT
---------------------------------------------------------------
    twilio   X-Twilio-Signature (HMAC-SHA1 over the URL and the sorted form fields)
    ses      the SNS message signature (cert host, cached cert, topic allowlist)
    msg91    MSG91 does not sign its callbacks: a shared secret in the X-Webhook-Token header
             or the URL path (`/webhooks/msg91/<token>/`), compared in constant time

A callback that fails verification answers 403, writes NO event and logs a masked warning (the
provider and the caller's address; never the body). Status events are idempotent per
(provider message id, status): a provider that repeats itself changes nothing.

An SMS STOP opts the number out of all SMS; a permanent SES bounce or a complaint opts the
address out of all email. Both are written by the system actor and audited.

This view is deliberately NOT under `admin/`: it has no operator, only a signature.
"""

import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import dispatcher, preferences
from .models import NotificationMessage
from .providers import webhook_class

logger = logging.getLogger(__name__)


def _client_address(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR', ''))


def handle(provider, request, path_token=''):
    """`(http_status, body)` for one callback. Pure of the HTTP framework, for the tests."""
    adapter = webhook_class(provider)
    if adapter is None:
        return 404, {'error': 'unknown provider'}
    if not adapter.verify_webhook(request, path_token):
        logger.warning('notification_webhook_rejected provider=%s reason=bad_signature ip=%s',
                       provider, _client_address(request))
        return 403, {'error': 'signature verification failed'}

    applied = ignored = 0
    for event in adapter.parse_webhook(request):
        if _apply(provider, event):
            applied += 1
        else:
            ignored += 1
    return 200, {'applied': applied, 'ignored': ignored}


def _apply(provider, event):
    if event.kind == 'status':
        message = NotificationMessage.objects.filter(
            provider=provider, provider_message_id=event.provider_message_id).first()
        if message is None:
            logger.info('notification_webhook_unknown_message provider=%s', provider)
            return False
        return dispatcher.apply_status(
            message, event.status, provider=provider,
            provider_message_id=event.provider_message_id, detail=event.detail)

    if event.kind == 'stop':
        changed = False
        for user in preferences.users_for_phone(event.phone):
            changed |= preferences.opt_out_channel(
                user, 'sms', f'webhook:{provider}', 'notification_sms_stop_received',
                'The number replied STOP; opted out of all SMS.')
        return changed

    if event.kind in ('bounce', 'complaint'):
        message = NotificationMessage.objects.filter(
            provider=provider, provider_message_id=event.provider_message_id).first()
        if message is None:
            return False
        changed = False
        for user in preferences.users_for_message(message):
            changed |= preferences.opt_out_channel(
                user, 'email', f'webhook:{provider}', f'notification_email_{event.kind}_opt_out',
                f'SES reported a {event.kind}; opted out of all email.')
        return changed
    return False


@csrf_exempt
@require_POST
def webhook(request, provider, token=''):
    status, body = handle(provider, request, token)
    return JsonResponse(body, status=status)
