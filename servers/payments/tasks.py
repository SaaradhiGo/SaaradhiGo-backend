"""Periodic reconciliation tasks for the payments domain.

Why this exists: Cashfree's webhook delivery is at-least-once but can also
silently drop a notification (network issue, our handler bug, signature
verify temporarily off, etc.). Without an active sweeper, a Payment can
sit in 'processing' forever even though Cashfree shows it PAID — meaning
the rider was charged, the trip never gets marked paid, and the driver
never gets credited.

The sweeper runs every 5 minutes, looks at locally-stuck payments older
than a small grace window, asks Cashfree what the truth is, and converges
local state. The same atomic-and-locked settle path used by the webhook
handler is reused so concurrent webhook delivery doesn't double-credit.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Don't bother the gateway for very fresh rows — the webhook may still
# be on its way. 5 minutes is well past Cashfree's typical settle time.
RECON_GRACE_MINUTES = 5

# Only look back so far so a one-time backlog doesn't make the task
# unbounded; the sweeper runs every 5 min so 24h gives us plenty of
# re-attempts on any given row.
RECON_LOOKBACK_HOURS = 24

# Cap how many rows we touch per run so the task is bounded.
RECON_BATCH_SIZE = 100


@shared_task(name='payments.reconcile_stuck_payments')
def reconcile_stuck_payments():
    """Find local payments that are stuck mid-flight and converge state.

    Two kinds of stuckness:
      1. Payment.status in (pending, processing) — webhook never arrived
         or our verify endpoint never ran. If Cashfree says PAID we
         settle locally (and credit driver) via the same path the
         webhook would use.
      2. WalletTransaction.status == 'pending' — wallet top-up where the
         user finished checkout but neither verify nor webhook landed.
    """
    from servers.payments.models import Payment, TransactionHistory, PaymentGateway
    from servers.payments.payment_gateways.factory import get_payment_gateway
    from servers.rider.models import WalletTransaction, Wallet
    from decimal import Decimal, InvalidOperation

    now = timezone.now()
    grace_cutoff = now - timedelta(minutes=RECON_GRACE_MINUTES)
    lookback_cutoff = now - timedelta(hours=RECON_LOOKBACK_HOURS)

    try:
        gateway = get_payment_gateway()
    except Exception as e:
        logger.error(f"reconcile_stuck_payments: cannot get gateway: {e}")
        return {'ok': False, 'reason': 'no gateway'}
    if not gateway:
        logger.warning("reconcile_stuck_payments: gateway unavailable, skipping")
        return {'ok': False, 'reason': 'no gateway'}

    payments_settled = 0
    wallets_settled = 0
    skipped = 0

    # --- Trip payments stuck in pending/processing ---
    stuck_payments = (
        Payment.objects.filter(
            status__in=['pending', 'processing'],
            created_at__lt=grace_cutoff,
            created_at__gt=lookback_cutoff,
        )
        .exclude(Q(cashfree_order_id__isnull=True) | Q(cashfree_order_id=''))
        .order_by('created_at')[:RECON_BATCH_SIZE]
    )

    for payment in stuck_payments:
        order_id = payment.cashfree_order_id or payment.gateway_order_id
        if not order_id:
            skipped += 1
            continue
        try:
            info = gateway.get_order_status(order_id)
        except Exception as e:
            logger.warning(f"recon: get_order_status failed for {order_id}: {e}")
            skipped += 1
            continue
        if not info:
            skipped += 1
            continue

        order_status = info.get('order_status')
        if order_status != 'PAID':
            # Cashfree may converge later; skip until then. We do not flip
            # 'pending' → 'failed' here because the rider could still be
            # mid-checkout.
            skipped += 1
            continue

        # Settle inside an atomic block with select_for_update so a
        # concurrent webhook landing can't double-credit. Same gate the
        # webhook handler uses (payment.status == 'completed').
        try:
            with transaction.atomic():
                p = Payment.objects.select_for_update().select_related(
                    'trip_id', 'trip_id__driver_id', 'user_id'
                ).get(pk=payment.pk)
                if p.status == 'completed':
                    continue
                p.status = 'completed'
                p.save(update_fields=['status', 'updated_at'])
                trip = p.trip_id
                trip.payment_status = 'completed'
                trip.payment_method = 'online'
                trip.save(update_fields=['payment_status', 'payment_method'])
                if trip.driver_id:
                    TransactionHistory.objects.get_or_create(
                        trip_id=trip,
                        gateway_payment_id=(
                            p.cashfree_payment_id or p.gateway_payment_id or order_id
                        ),
                        defaults={
                            'user_id': p.user_id,
                            'driver_id': trip.driver_id,
                            'amount': p.amount,
                            'method': 'online',
                            'payment_gateway': p.payment_gateway,
                            'cashfree_payment_id': p.cashfree_payment_id,
                            'user_name': p.user_id.full_name or p.user_id.phone_number,
                            'status': 'completed',
                            'txn_type': 'payment',
                        },
                    )
                    from servers.driver.utils import credit_driver_wallet
                    credit_driver_wallet(trip)
            payments_settled += 1
            logger.info(
                f"recon: settled Payment {payment.pk} (trip {payment.trip_id_id}) "
                f"via gateway poll"
            )
        except Exception as e:
            logger.error(f"recon: failed to settle Payment {payment.pk}: {e}")
            skipped += 1

    # --- Wallet top-ups stuck in pending ---
    stuck_topups = (
        WalletTransaction.objects.filter(
            status='pending',
            txn_type='credit',
            created_at__lt=grace_cutoff,
            created_at__gt=lookback_cutoff,
        )
        .exclude(Q(gateway_order_id__isnull=True) | Q(gateway_order_id=''))
        .order_by('created_at')[:RECON_BATCH_SIZE]
    )

    for txn in stuck_topups:
        order_id = txn.gateway_order_id
        try:
            info = gateway.get_order_status(order_id)
        except Exception as e:
            logger.warning(f"recon: get_order_status failed for wallet {order_id}: {e}")
            skipped += 1
            continue
        if not info or info.get('order_status') != 'PAID':
            skipped += 1
            continue
        try:
            gateway_amount = Decimal(str(info.get('order_amount'))).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError):
            logger.warning(f"recon: malformed amount on wallet {order_id}")
            skipped += 1
            continue

        try:
            with transaction.atomic():
                t = WalletTransaction.objects.select_for_update().get(pk=txn.pk)
                if t.status == 'completed':
                    continue
                if t.amount != gateway_amount:
                    logger.error(
                        f"recon: wallet amount mismatch order={order_id} "
                        f"txn={t.amount} gateway={gateway_amount}; refusing"
                    )
                    skipped += 1
                    continue
                t.status = 'completed'
                t.save(update_fields=['status'])
                wallet, _ = Wallet.objects.select_for_update().get_or_create(user_id=t.user_id)
                wallet.balance = wallet.balance + gateway_amount
                wallet.save(update_fields=['balance'])
            wallets_settled += 1
            logger.info(
                f"recon: settled WalletTransaction {txn.pk} for user {txn.user_id_id} "
                f"amount={gateway_amount}"
            )
        except Exception as e:
            logger.error(f"recon: failed to settle WalletTransaction {txn.pk}: {e}")
            skipped += 1

    logger.info(
        f"recon: payments={payments_settled} wallets={wallets_settled} skipped={skipped}"
    )
    return {
        'ok': True,
        'payments_settled': payments_settled,
        'wallets_settled': wallets_settled,
        'skipped': skipped,
    }
