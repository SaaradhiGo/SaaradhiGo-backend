"""Send an approved card refund to the provider the trip was paid on (Stripe for now).

A refund is written as `pending_gateway` by `admin_trips.execute_refund`; `send` passes it to
Stripe with an idempotency key of the money action, so a retry never refunds twice. Providers not
yet tested end to end (Razorpay, Cashfree) stay pending for Finance, as before.
"""

import logging

from django.db import transaction

from servers.admin_audit.services import record_admin_action

logger = logging.getLogger(__name__)

#: Providers whose refund call is tested end to end (Stripe test mode, 15 Nov).
SENDS = ('stripe',)


def _payment(trip):
    from servers.payments.models import Payment

    return (Payment.objects.filter(trip_id=trip, status__in=('completed', 'refunded', 'partially_refunded'))
            .exclude(gateway_payment_id__isnull=True).exclude(gateway_payment_id='')
            .order_by('-id').first())


def send(action_id):
    """Send one pending refund. Returns 'sent', 'not_supported', 'failed' or 'skipped'."""
    from servers.payments.payment_gateways.factory import get_payment_gateway

    from .models import TripMoneyAction

    with transaction.atomic():
        row = (TripMoneyAction.objects.select_for_update().select_related('trip')
               .filter(pk=action_id, kind=TripMoneyAction.REFUND, method=TripMoneyAction.GATEWAY,
                       status=TripMoneyAction.PENDING_GATEWAY).first())
        if row is None:
            return 'skipped'
        payment = _payment(row.trip)
        provider = (getattr(payment, 'payment_gateway', '') or '').lower()
        if payment is None or provider not in SENDS:
            return 'not_supported'
        gateway = get_payment_gateway(provider)
        ref = payment.gateway_order_id if str(payment.gateway_order_id or '').startswith('pi_') \
            else payment.gateway_payment_id
        result = gateway.create_refund(ref, amount_minor=row.amount_minor, currency=row.currency,
                                       refund_reference=f'trip_refund_{row.id}') if gateway else None
        if not result:
            logger.warning('gateway_refund_failed', extra={'event': 'gateway_refund_failed',
                                                           'money_action_id': row.id})
            return 'failed'
        row.status = TripMoneyAction.COMPLETED
        row.gateway_refund_id = str(result.get('stripe_refund_id') or result.get('refund_id') or '')[:64]
        row.save(update_fields=['status', 'gateway_refund_id'])
        record_admin_action(
            None, action='trip_refund_sent', target_type='trip', target_id=row.trip_id,
            after={'money_action_id': row.id, 'amount_minor': row.amount_minor, 'currency': row.currency,
                   'provider': provider, 'gateway_refund_id': row.gateway_refund_id,
                   'provider_status': result.get('status')},
            reason=row.reason or 'approved trip refund')
    return 'sent'


def send_pending(limit=100):
    """Hourly backstop (and the path for refunds recorded before this existed)."""
    from .models import TripMoneyAction

    ids = list(TripMoneyAction.objects.filter(kind=TripMoneyAction.REFUND, method=TripMoneyAction.GATEWAY,
                                              status=TripMoneyAction.PENDING_GATEWAY)
               .order_by('id').values_list('id', flat=True)[:limit])
    from servers.admin_audit.context import audit_context

    out = {}
    for i in ids:
        try:
            with audit_context(actor=None, actor_label='system:gateway_refund', reason='send approved refund'):
                out[i] = send(i)
        except Exception:  # noqa: BLE001 - one refund never stops the others
            logger.exception('gateway refund send failed money_action=%s', i)
            out[i] = 'failed'
    return out
