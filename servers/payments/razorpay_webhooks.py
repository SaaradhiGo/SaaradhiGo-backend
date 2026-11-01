"""Razorpay (payments) and RazorpayX (payouts) webhook endpoints.

    POST /api/v1/webhooks/razorpay/    payments   (webhook secret: RAZORPAY_WEBHOOK_SECRET)
    POST /api/v1/webhooks/razorpayx/   payouts    (webhook secret: RAZORPAYX_WEBHOOK_SECRET)

Order of operations, per delivery:
  1. Verify X-Razorpay-Signature (HMAC-SHA256 of the RAW body under the webhook secret, constant
     time). Missing or invalid -> 400 and NOTHING else happens (no inbox row, no side effect).
  2. Insert into the WebhookEvent inbox keyed (gateway, X-Razorpay-Event-Id). A duplicate returns
     200 "already_processed" and does nothing. (Razorpay documents the event id header as the
     idempotency key; if a delivery has none, a hash of the raw body is used instead.)
  3. Process through the SAME code paths the Cashfree webhooks use: the delivery is normalised
     into Cashfree's event shape and handed to `views._handle_cashfree_webhook` /
     `views._handle_cashfree_payout_webhook`. Money still moves only after the order is
     re-read from the provider (`get_order_status`), never on the webhook body alone.
  4. If processing raises, the inbox row is removed so Razorpay's retry is processed, not
     swallowed as a duplicate, and 500 is returned.

Events (subscribe to exactly these in each dashboard):
  razorpay   payment.authorized, payment.captured, payment.failed, order.paid,
             refund.processed, refund.failed
  razorpayx  payout.queued, payout.pending, payout.initiated, payout.processed, payout.reversed,
             payout.failed, payout.rejected  (payout.failed is mandatory with the Payouts API)
"""

import hashlib
import json
import logging

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from servers.payments import views
from servers.payments.models import Payment, WebhookEvent
from servers.payments.payment_gateways.factory import (
    get_payment_gateway, get_payment_gateway_for_payouts, get_webhook_verifier,
)

logger = logging.getLogger(__name__)

# Razorpay payout status -> the Cashfree-shaped event the shared handler knows.
_PAYOUT_EVENTS = {
    'payout.processed': 'PAYOUT_SUCCESS',
    'payout.failed': 'PAYOUT_FAILED',
    'payout.rejected': 'PAYOUT_FAILED',
    'payout.cancelled': 'PAYOUT_FAILED',
    'payout.reversed': 'PAYOUT_REVERSED',
    # Recorded as the in-flight status only (never moves a finished withdrawal backwards).
    'payout.queued': 'PAYOUT_PENDING',
    'payout.pending': 'PAYOUT_PENDING',
    'payout.initiated': 'PAYOUT_PENDING',
}


def _entity(payload, key):
    return (((payload.get('payload') or {}).get(key) or {}).get('entity')) or {}


def normalise_payment_event(payload):
    """A Razorpay payment event as the Cashfree-shaped dict the shared handler reads."""
    event = payload.get('event', '')
    payment = _entity(payload, 'payment')
    order = _entity(payload, 'order')
    order_id = payment.get('order_id') or order.get('id')
    if event in ('payment.captured', 'order.paid'):
        kind = 'PAYMENT_SUCCESS_WEBHOOK'
    elif event == 'payment.failed':
        kind = 'PAYMENT_FAILED_WEBHOOK'
    else:
        kind = event
    return {'type': kind, 'data': {'order': {'order_id': order_id},
                                    'payment': {'cf_payment_id': payment.get('id')}}}


def normalise_payout_event(payload):
    """A RazorpayX payout event as the Cashfree-shaped dict the shared handler reads."""
    entity = _entity(payload, 'payout')
    details = entity.get('status_details') or {}
    return {
        'event': _PAYOUT_EVENTS.get(payload.get('event', ''), payload.get('event', '')),
        'transfer_id': entity.get('reference_id'),    # our stable withdrawal reference
        'cf_transfer_id': entity.get('id'),           # Razorpay's pout_...
        'status': entity.get('status'),
        'failureReason': details.get('description') or details.get('reason') or '',
    }


def _handle_refund_event(payload):
    """Record a refund's final state on the Payment (no money moves here: the refund endpoint
    already settled our side when the refund was created)."""
    event = payload.get('event', '')
    refund = _entity(payload, 'refund')
    payment = Payment.objects.filter(gateway_payment_id=refund.get('payment_id')).first()
    if payment is None:
        return JsonResponse({'status': 'skipped', 'reason': 'payment not found'}, status=200)
    meta = dict(payment.gateway_metadata or {})
    meta['refund'] = {'id': refund.get('id'), 'status': refund.get('status'),
                      'amount': refund.get('amount'), 'event': event}
    payment.gateway_metadata = meta
    payment.save(update_fields=['gateway_metadata', 'updated_at'])
    if event == 'refund.failed':
        logger.error('razorpay refund FAILED for payment %s (refund %s): finance must follow up',
                     payment.pk, refund.get('id'))
    return JsonResponse({'status': 'ok'}, status=200)


def _handle_payment_event(payload, gateway):
    event = payload.get('event', '')
    if event in ('refund.processed', 'refund.failed'):
        return _handle_refund_event(payload)
    if event == 'payment.authorized':
        # Only an `authorized` payment needs an explicit capture; the captured event follows.
        payment = _entity(payload, 'payment')
        gateway.ensure_captured(payment.get('id'), payment.get('amount'),
                                payment.get('currency') or 'INR')
        return JsonResponse({'status': 'ok'}, status=200)
    return views._handle_cashfree_webhook(normalise_payment_event(payload), gateway)


def _handle_payout_event(payload, gateway):
    return views._handle_cashfree_payout_webhook(normalise_payout_event(payload), gateway)


def _inbox_delivery(request, provider, kind, resolve_gateway, handler):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    body = request.body
    signature = request.headers.get('X-Razorpay-Signature', '')

    # 1. Signature first: nothing is written or resolved for an unsigned / forged delivery.
    if not signature or not get_webhook_verifier(kind, provider).verify_webhook_signature(
            body, signature):
        logger.warning('%s webhook rejected: missing or invalid signature', provider)
        return JsonResponse({'error': 'Invalid signature'}, status=400)

    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError('payload is not an object')
    except ValueError:
        return JsonResponse({'error': 'Invalid payload'}, status=400)

    gateway = resolve_gateway()
    if not gateway:
        return JsonResponse({'error': f'Gateway {provider} not configured'}, status=503)

    # 2. Inbox, idempotent by event id.
    dedupe_key = (request.headers.get('X-Razorpay-Event-Id')
                  or 'body:' + hashlib.sha256(body).hexdigest())[:512]
    event_type = str(payload.get('event', ''))[:64]
    try:
        with transaction.atomic():
            row = WebhookEvent.objects.create(
                gateway=provider, dedupe_key=dedupe_key, event_type=event_type,
                raw_payload=payload)
    except IntegrityError:
        logger.info('%s webhook deduplicated (event %s)', provider, dedupe_key[:24])
        return JsonResponse({'status': 'already_processed'}, status=200)

    # 3. Process through the shared handlers.
    try:
        response = handler(payload, gateway)
    except Exception:
        logger.exception('%s webhook processing failed (event %s)', provider, event_type)
        row.delete()  # let Razorpay's retry run again instead of being deduplicated away
        return JsonResponse({'error': 'Internal error'}, status=500)
    try:
        WebhookEvent.objects.filter(pk=row.pk).update(
            processed_at=timezone.now(), result='ok' if response.status_code == 200 else 'error')
    except Exception:
        pass
    return response


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """Razorpay payment webhooks. No JWT: authenticated by X-Razorpay-Signature."""
    return _inbox_delivery(request, 'razorpay', 'payments',
                           lambda: get_payment_gateway('razorpay'), _handle_payment_event)


@csrf_exempt
@require_POST
def razorpayx_webhook(request):
    """RazorpayX payout webhooks. No JWT: authenticated by X-Razorpay-Signature."""
    return _inbox_delivery(
        request, 'razorpayx', 'payouts',
        lambda: get_payment_gateway_for_payouts(gateway_name='razorpay'), _handle_payout_event)
