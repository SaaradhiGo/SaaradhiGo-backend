"""Stripe (payments) and Stripe Connect (payouts) webhook endpoints (13 Nov).

    POST /api/v1/webhooks/stripe/          STRIPE_WEBHOOK_SECRET
    POST /api/v1/webhooks/stripe-connect/  STRIPE_CONNECT_WEBHOOK_SECRET

Same order as the Razorpay endpoints (razorpay_webhooks.py): the Stripe-Signature first (nothing
is written for a forged delivery), then the signed `created` within the replay window, then the
WebhookEvent inbox keyed by the hash of the signed body, then the SAME shared handlers, which
re-read the payment from Stripe before any money moves.

Events to subscribe:
  payments  payment_intent.succeeded, payment_intent.payment_failed, charge.refunded
  connect   transfer.created, transfer.reversed
"""

import hashlib
import json
import logging
import time

from django.conf import settings
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


def _object(payload):
    return ((payload.get('data') or {}).get('object')) or {}


def normalise_payment_event(payload):
    event, obj = payload.get('type', ''), _object(payload)
    kind = {'payment_intent.succeeded': 'PAYMENT_SUCCESS_WEBHOOK',
            'payment_intent.payment_failed': 'PAYMENT_FAILED_WEBHOOK'}.get(event, event)
    return {'type': kind, 'data': {'order': {'order_id': obj.get('id')},
                                   'payment': {'cf_payment_id': obj.get('latest_charge') or obj.get('id')}}}


def normalise_payout_event(payload):
    event, obj = payload.get('type', ''), _object(payload)
    return {
        'event': {'transfer.created': 'PAYOUT_SUCCESS', 'transfer.reversed': 'PAYOUT_REVERSED'}.get(event, event),
        'transfer_id': (obj.get('metadata') or {}).get('reference_id'),
        'cf_transfer_id': obj.get('id'),
        'status': 'reversed' if obj.get('reversed') else 'processed',
        'failureReason': '',
    }


def _handle_payment(payload, gateway):
    if payload.get('type') == 'charge.refunded':
        charge = _object(payload)
        payment = Payment.objects.filter(gateway_payment_id__in=[
            charge.get('payment_intent'), charge.get('id')]).first()
        if payment is not None:
            meta = dict(payment.gateway_metadata or {})
            meta['refund'] = {'charge': charge.get('id'), 'amount_refunded': charge.get('amount_refunded')}
            payment.gateway_metadata = meta
            payment.save(update_fields=['gateway_metadata', 'updated_at'])
        return JsonResponse({'status': 'ok'}, status=200)
    if payload.get('type') not in ('payment_intent.succeeded', 'payment_intent.payment_failed'):
        return JsonResponse({'status': 'ignored'}, status=200)
    return views._handle_cashfree_webhook(normalise_payment_event(payload), gateway)


def _handle_payout(payload, gateway):
    if payload.get('type') not in ('transfer.created', 'transfer.reversed'):
        return JsonResponse({'status': 'ignored'}, status=200)
    return views._handle_cashfree_payout_webhook(normalise_payout_event(payload), gateway)


def _deliver(request, provider, kind, resolve_gateway, handler):
    body = request.body
    signature = request.headers.get('Stripe-Signature', '')
    if not signature or not get_webhook_verifier(kind, provider).verify_webhook_signature(body, signature):
        logger.warning('%s webhook rejected: missing or invalid signature', provider)
        return JsonResponse({'error': 'Invalid signature'}, status=400)
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return JsonResponse({'error': 'Invalid payload'}, status=400)
    created = payload.get('created')
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return JsonResponse({'error': 'Invalid payload'}, status=400)
    max_age = int(getattr(settings, 'STRIPE_WEBHOOK_MAX_AGE_SECONDS', 3 * 24 * 3600 + 300))
    if time.time() - created > max_age:
        return JsonResponse({'status': 'ignored', 'reason': 'stale event'}, status=200)
    gateway = resolve_gateway()
    if not gateway:
        return JsonResponse({'error': f'Gateway {provider} not configured'}, status=503)
    dedupe_key = 'body:' + hashlib.sha256(body).hexdigest()
    event_type = str(payload.get('type', ''))[:64]
    try:
        with transaction.atomic():
            row = WebhookEvent.objects.create(gateway=provider, dedupe_key=dedupe_key,
                                              event_type=event_type, raw_payload=payload)
    except IntegrityError:
        return JsonResponse({'status': 'already_processed'}, status=200)
    try:
        response = handler(payload, gateway)
    except Exception:
        logger.exception('%s webhook processing failed (event %s)', provider, event_type)
        row.delete()
        return JsonResponse({'error': 'Internal error'}, status=500)
    WebhookEvent.objects.filter(pk=row.pk).update(
        processed_at=timezone.now(), result='ok' if response.status_code == 200 else 'error')
    return response


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe payment webhooks. No JWT: authenticated by Stripe-Signature."""
    return _deliver(request, 'stripe', 'payments', lambda: get_payment_gateway('stripe'), _handle_payment)


@csrf_exempt
@require_POST
def stripe_connect_webhook(request):
    """Stripe Connect transfer webhooks. No JWT: authenticated by Stripe-Signature."""
    return _deliver(request, 'stripe_connect', 'payouts',
                    lambda: get_payment_gateway_for_payouts(gateway_name='stripe_connect'), _handle_payout)
