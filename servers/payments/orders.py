"""Every gateway order a trip Payment was issued, and which Payment an order id belongs to.

See `PaymentOrder` (AUDIT 27.76). The Payment row keeps naming one order in `gateway_order_id` (the
latest issued while unpaid, then the one that paid); this module answers "whose order is this" for
every order ever issued, so a capture on an earlier checkout is not lost.
"""

from django.db import IntegrityError, models, transaction


def remember(payment, order_id, provider=''):
    """Record that `payment` was issued `order_id` (idempotent)."""
    from servers.payments.models import PaymentOrder

    if not order_id:
        return None
    order_id = str(order_id)
    existing = PaymentOrder.objects.filter(order_id=order_id).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            return PaymentOrder.objects.create(payment=payment, order_id=order_id,
                                               provider=provider or '')
    except IntegrityError:
        return PaymentOrder.objects.filter(order_id=order_id).first()


def payment_for_order(order_id, user=None):
    """The trip Payment an order id was issued to, or None. Every order ever issued is matched;
    a Payment from before the table existed is matched on its own order columns."""
    from servers.payments.models import Payment, PaymentOrder

    if not order_id:
        return None
    qs = Payment.objects.select_related('trip_id', 'trip_id__driver_id')
    if user is not None:
        qs = qs.filter(user_id=user)
    row = PaymentOrder.objects.filter(order_id=str(order_id)).values_list('payment_id', flat=True).first()
    if row is not None:
        return qs.filter(pk=row).first()
    return (qs.filter(models.Q(cashfree_order_id=order_id) | models.Q(gateway_order_id=order_id))
            .order_by('-created_at', '-id').first())


def order_ids(payment):
    """Every order id issued to `payment`, newest first (its own columns included)."""
    from servers.payments.models import PaymentOrder

    ids = list(PaymentOrder.objects.filter(payment=payment).order_by('-created_at', '-id')
               .values_list('order_id', flat=True))
    for own in (payment.gateway_order_id, payment.cashfree_order_id):
        if own and own not in ids:
            ids.insert(0, own)
    return ids


def is_late_capture(payment, order_id, gateway_payment_id=None):
    """A capture on a Payment that is already completed by ANOTHER order (or another gateway
    payment on the same order): money taken a second time for one trip."""
    if payment.status != 'completed':
        return False
    own = {payment.gateway_order_id, payment.cashfree_order_id} - {None, ''}
    if order_id and own and str(order_id) not in own:
        return True
    return bool(gateway_payment_id and payment.gateway_payment_id
                and str(gateway_payment_id) != payment.gateway_payment_id)


def settled_by(payment, order_id):
    """The order that paid becomes the one the Payment names (call before saving it completed)."""
    if order_id:
        payment.gateway_order_id = str(order_id)
        if payment.cashfree_order_id:
            payment.cashfree_order_id = str(order_id)
