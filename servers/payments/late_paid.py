"""An online order paid AFTER the trip was paid another way (independent review HIGH-3, AUDIT 27.75).

The sequence: an online order is opened for a trip; the rider switches to cash (the driver confirms
it) or pays from the wallet, which marks that online Payment failed but cannot close the order at the
gateway; then the rider's earlier checkout completes (a UPI collect approved late) and the gateway
reports the old order PAID. Every online-completion path then tried to mark it completed, hit the
one-completed-payment-per-trip constraint and raised: the rider had paid twice and nothing recorded
or refunded it. (Without that constraint the driver would also have been credited on top of the cash.)

Now each path asks `superseded(payment)` under its row lock first. A superseded payment is never
completed and never settled. Instead, in the same transaction, the late payment is recorded on the
row (`gateway_metadata.late_paid`) and one CRITICAL Finance alert is raised
(`finance.paid_after_settled`); after the commit the gateway payment is refunded in full through the
provider it was taken on. When the refund succeeds the row becomes `refunded` and the alert says so;
when it cannot be made (no gateway payment id, the gateway refused) the alert stays as the to-do for
a manual refund. The provider is answered 200, so it stops retrying.
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


def record(payment, *, gateway_name, gateway_payment_id=None, source=''):
    """Inside the caller's transaction, with `payment` locked. Writes nothing that moves money."""
    from servers.command_centre.models import CRITICAL, FINANCE_PAID_AFTER_SETTLED
    from servers.command_centre.services import raise_alert
    from servers.payments.unsettled import city_node_of

    meta = dict(payment.gateway_metadata or {})
    seen = dict(meta.get('late_paid') or {})
    seen.update({'at': seen.get('at') or timezone.now().isoformat(), 'source': source,
                 'gateway': gateway_name or payment.payment_gateway or '',
                 'refund': seen.get('refund') or 'pending'})
    meta['late_paid'] = seen
    payment.gateway_metadata = meta
    fields = ['gateway_metadata']
    if gateway_payment_id and not payment.gateway_payment_id:
        payment.gateway_payment_id = str(gateway_payment_id)
        fields.append('gateway_payment_id')
    payment.save(update_fields=fields)

    trip = payment.trip_id
    city = city_node_of(trip)
    raise_alert(
        FINANCE_PAID_AFTER_SETTLED, severity=CRITICAL,
        title=(f'Trip {trip.id}: an old online order of '
               f'{format_money(payment.amount, payment.currency)} was paid after the trip was paid '
               f'another way; refunding')[:200],
        payload={'trip_id': trip.id, 'payment_id': payment.id, 'amount_minor': payment.amount_minor,
                 'currency': payment.currency, 'gateway': seen['gateway'], 'source': source,
                 'refund': 'pending', 'city': city.code if city is not None else None},
        source_type=ALERT_SOURCE_TYPE, source_id=str(payment.id), city=city)
    payment_id = payment.id
    transaction.on_commit(lambda: refund(payment_id))
    logger.error('online_payment_paid_after_settled', extra={
        'event': 'online_payment_paid_after_settled', 'payment_id': payment.id,
        'trip_id': trip.id, 'source': source})


def _gateway(name):
    """The provider the late payment was taken on."""
    from servers.payments.payment_gateways.factory import get_payment_gateway

    return get_payment_gateway(name or None)


def refund(payment_id):
    """After the commit: refund the late payment in full through the provider it was taken on."""
    from servers.command_centre.models import OperatorAlert
    from servers.payments.models import Payment

    payment = Payment.objects.filter(pk=payment_id).first()
    if payment is None or payment.status == 'refunded':
        return None
    outcome, refund_id = 'manual_refund_needed', None
    if payment.gateway_payment_id:
        try:
            gateway = _gateway((payment.gateway_metadata or {}).get('late_paid', {})
                               .get('gateway') or payment.payment_gateway)
            result = gateway.create_refund(payment.gateway_payment_id,
                                           amount_minor=payment.amount_minor,
                                           currency=payment.currency) if gateway else None
        except Exception:  # noqa: BLE001 - a refund failure is the alert's job, never an error page
            logger.exception('late paid refund failed for payment %s', payment_id)
            result = None
        if result:
            outcome = 'refunded'
            refund_id = result.get('refund_id') or result.get('id')
    with transaction.atomic():
        payment = Payment.objects.select_for_update(of=('self',)).get(pk=payment_id)
        meta = dict(payment.gateway_metadata or {})
        seen = dict(meta.get('late_paid') or {})
        seen.update({'refund': outcome, 'refund_id': refund_id})
        meta['late_paid'] = seen
        payment.gateway_metadata = meta
        fields = ['gateway_metadata']
        if outcome == 'refunded':
            payment.status = 'refunded'
            fields.append('status')
        payment.save(update_fields=fields)
        alert = OperatorAlert.objects.filter(source_type=ALERT_SOURCE_TYPE,
                                             source_id=str(payment_id)).order_by('-created_at').first()
        if alert is not None:
            alert.payload = {**(alert.payload or {}), 'refund': outcome, 'refund_id': refund_id}
            if outcome == 'refunded':
                alert.title = alert.title.replace('; refunding', '; refunded in full')[:200]
            else:
                alert.title = alert.title.replace('; refunding', '; REFUND IT MANUALLY')[:200]
            alert.save(update_fields=['payload', 'title'])
    return outcome
