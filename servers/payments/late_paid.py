"""Money captured for a trip that is already paid: never settled, refunded in full (AUDIT 27.75, 27.76).

Two ways it happens:

* **The trip was paid another way first** (independent review HIGH-3). An online order is open; the
  rider switches to cash (the driver confirms it) or pays from the wallet, which marks that online
  Payment failed but cannot close the order at the gateway; then the earlier checkout completes. The
  Payment is `superseded`.
* **Checkout was opened twice** (review MEDIUM-1, owner decision). The rider pays one order, then the
  other one is paid too. The Payment is completed by the first; the second is a late capture
  (`orders.is_late_capture`).

Every online-completion path (verify, webhook, gateway poll, reconciliation task) checks both under
the Payment's row lock first. A late payment is never completed and never settled. Instead, in the
same transaction, it is recorded on its order's `PaymentOrder` row (the gateway payment taken) and
one CRITICAL Finance alert is raised per order (`finance.paid_after_settled`); after the commit THAT
gateway payment is refunded in full through the provider it was taken on. A superseded Payment also
becomes `refunded`; a completed one stays completed (its own capture stands). When the refund cannot
be made (no gateway payment id, the gateway refused) the alert stays as the to-do for a manual refund.
The provider is answered 200, so it stops retrying.
"""

import logging

from django.db import transaction
from django.utils import timezone

from base.money import format_money

logger = logging.getLogger(__name__)

ALERT_SOURCE_TYPE = 'payment_paid_after_settled'


def superseded(payment):
    """True when this online payment no longer stands for its trip: replaced by a switch, marked
    failed or refunded, or the trip already has another completed payment."""
    from servers.payments.models import Payment

    if payment.status in ('failed', 'refunded') or payment.replaced_by_id:
        return True
    return Payment.objects.filter(trip_id=payment.trip_id_id, status='completed').exclude(
        pk=payment.pk).exists()


def record(payment, *, gateway_name, gateway_payment_id=None, source='', order_id=None):
    """Inside the caller's transaction, with `payment` locked. Writes nothing that moves money.

    `order_id` is the order the money was captured on (defaults to the one the Payment names)."""
    from servers.command_centre.models import CRITICAL, FINANCE_PAID_AFTER_SETTLED
    from servers.command_centre.services import raise_alert
    from servers.payments.models import PaymentOrder
    from servers.payments.orders import remember
    from servers.payments.unsettled import city_node_of

    order = remember(payment, order_id or payment.gateway_order_id or payment.cashfree_order_id,
                     gateway_name or payment.payment_gateway)
    taken = str(gateway_payment_id or '')
    if order is not None:
        order = PaymentOrder.objects.select_for_update().get(pk=order.pk)
        if order.late_refund and (not taken or taken == order.late_payment_id):
            return order                                   # this capture is already in hand
        order.late_payment_id = taken or order.late_payment_id
        order.late_refund = PaymentOrder.REFUND_PENDING
        order.refund_id = ''
        order.save(update_fields=['late_payment_id', 'late_refund', 'refund_id'])

    gateway = gateway_name or payment.payment_gateway or ''
    if payment.status != 'completed':
        # The Payment itself no longer stands: it carries the late payment (its own capture).
        meta = dict(payment.gateway_metadata or {})
        seen = dict(meta.get('late_paid') or {})
        seen.update({'at': seen.get('at') or timezone.now().isoformat(), 'source': source,
                     'gateway': gateway, 'refund': PaymentOrder.REFUND_PENDING})
        meta['late_paid'] = seen
        payment.gateway_metadata = meta
        fields = ['gateway_metadata']
        if taken and not payment.gateway_payment_id:
            payment.gateway_payment_id = taken
            fields.append('gateway_payment_id')
        payment.save(update_fields=fields)

    trip = payment.trip_id
    city = city_node_of(trip)
    raise_alert(
        FINANCE_PAID_AFTER_SETTLED, severity=CRITICAL,
        title=(f'Trip {trip.id}: {format_money(payment.amount, payment.currency)} was captured '
               f'again after the trip was paid; refunding')[:200],
        payload={'trip_id': trip.id, 'payment_id': payment.id, 'amount_minor': payment.amount_minor,
                 'currency': payment.currency, 'gateway': gateway, 'source': source,
                 'order_id': order.order_id if order is not None else '',
                 'refund': PaymentOrder.REFUND_PENDING,
                 'city': city.code if city is not None else None},
        source_type=ALERT_SOURCE_TYPE,
        source_id=f'order:{order.pk}' if order is not None else str(payment.id), city=city)
    order_pk = order.pk if order is not None else None
    transaction.on_commit(lambda: refund(payment.id, order_pk))
    logger.error('online_payment_paid_after_settled', extra={
        'event': 'online_payment_paid_after_settled', 'payment_id': payment.id,
        'trip_id': trip.id, 'source': source})
    return order


def _gateway(name):
    """The provider the late payment was taken on."""
    from servers.payments.payment_gateways.factory import get_payment_gateway

    return get_payment_gateway(name or None)


def refund(payment_id, order_pk=None):
    """After the commit: refund the late capture in full through the provider it was taken on."""
    from servers.command_centre.models import OperatorAlert
    from servers.payments.models import Payment, PaymentOrder

    payment = Payment.objects.filter(pk=payment_id).first()
    order = PaymentOrder.objects.filter(pk=order_pk).first() if order_pk else None
    if payment is None or (order is not None and order.late_refund == PaymentOrder.REFUND_DONE):
        return None
    if order is None and payment.status == 'refunded':
        return None
    taken = (order.late_payment_id if order is not None else '') or (
        payment.gateway_payment_id if payment.status != 'completed' else '')
    gateway_name = (order.provider if order is not None and order.provider else
                    (payment.gateway_metadata or {}).get('late_paid', {}).get('gateway')
                    or payment.payment_gateway)
    outcome, refund_id = PaymentOrder.REFUND_MANUAL, ''
    if taken:
        try:
            gateway = _gateway(gateway_name)
            result = gateway.create_refund(taken, amount_minor=payment.amount_minor,
                                           currency=payment.currency) if gateway else None
        except Exception:  # noqa: BLE001 - a refund failure is the alert's job, never an error page
            logger.exception('late paid refund failed for payment %s', payment_id)
            result = None
        if result:
            outcome = PaymentOrder.REFUND_DONE
            refund_id = str(result.get('refund_id') or result.get('id') or '')
    with transaction.atomic():
        payment = Payment.objects.select_for_update(of=('self',)).get(pk=payment_id)
        if order is not None:
            PaymentOrder.objects.filter(pk=order.pk).update(late_refund=outcome, refund_id=refund_id)
        if payment.status != 'completed':
            meta = dict(payment.gateway_metadata or {})
            seen = dict(meta.get('late_paid') or {})
            seen.update({'refund': outcome, 'refund_id': refund_id or None})
            meta['late_paid'] = seen
            payment.gateway_metadata = meta
            fields = ['gateway_metadata']
            if outcome == PaymentOrder.REFUND_DONE:
                payment.status = 'refunded'
                fields.append('status')
            payment.save(update_fields=fields)
        source_id = f'order:{order.pk}' if order is not None else str(payment_id)
        alert = OperatorAlert.objects.filter(source_type=ALERT_SOURCE_TYPE,
                                             source_id=source_id).order_by('-created_at').first()
        if alert is not None:
            alert.payload = {**(alert.payload or {}), 'refund': outcome, 'refund_id': refund_id}
            alert.title = alert.title.replace(
                '; refunding', '; refunded in full' if outcome == PaymentOrder.REFUND_DONE
                else '; REFUND IT MANUALLY')[:200]
            alert.save(update_fields=['payload', 'title'])
    return outcome
