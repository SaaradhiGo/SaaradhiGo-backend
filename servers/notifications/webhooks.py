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

from django.http import HttpResponse, JsonResponse
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import dispatcher, preferences
from .models import NotificationMessage
from .providers import webhook_class

logger = logging.getLogger(__name__)


def _client_address(request):
    # Logged on a rejected signature, so it must be the address that actually
    # connected, not one the caller wrote (AUDIT 27.78, M1).
    from base.client_ip import client_ip

    return client_ip(request, default='')


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
    reply = ''
    for event in adapter.parse_webhook(request):
        if event.kind == 'help':
            reply = help_text()
        if _apply(provider, event):
            applied += 1
        else:
            ignored += 1
    body = {'applied': applied, 'ignored': ignored}
    if reply:
        body['reply'] = reply
    return 200, body


def help_text():
    """The answer to an SMS HELP (13 Nov), from the US entity's configured contacts."""
    from base import legal_entity

    e = legal_entity.entity('US')
    contact = ' or '.join(x for x in (e.get('support_email'), e.get('support_phone')) if x)
    contact = contact or 'www.saaradhigo.com/contact'
    return (f'SaaradhiGo: trip and sign-in messages from {e["legal_name"]}. Help: {contact}. '
            'Msg & data rates may apply. Reply STOP to opt out.')


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

    if event.kind == 'help':
        # Recorded (13 Nov) against every account on the number; answered by the view.
        users = preferences.users_for_phone(event.phone)
        for user in users:
            preferences._system_audit('notification_sms_help_received', user, f'webhook:{provider}',
                                      'The number replied HELP; help text sent.')
        return bool(users)

    if event.kind == 'start':
        changed = False
        for user in preferences.users_for_phone(event.phone):
            _, did = preferences.set_preference(user, 'sms', 'transactional', True,
                                                f'webhook:{provider}')
            if did:
                preferences._system_audit('notification_sms_start_received', user,
                                          f'webhook:{provider}',
                                          'The number replied START; trip and sign-in SMS back on.')
            changed |= did
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
    if provider == 'twilio' and status == 200 and request.POST.get('Body') is not None:
        # An inbound message gets TwiML: HELP its answer, anything else an empty response.
        message = f'<Message>{escape(body["reply"])}</Message>' if body.get('reply') else ''
        return HttpResponse(f'<?xml version="1.0" encoding="UTF-8"?><Response>{message}</Response>',
                            content_type='text/xml')
    return JsonResponse(body, status=status)
