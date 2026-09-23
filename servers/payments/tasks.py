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
                wallet, _ = Wallet.objects.select_for_update().get_or_create(
                    user_id=t.user_id, scope=Wallet.SCOPE_RIDER,
                )
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


# --- Withdrawal (driver payout) reconciliation -----------------------------

WITHDRAWAL_RECON_GRACE_MINUTES = 5
WITHDRAWAL_RECON_LOOKBACK_HOURS = 48
WITHDRAWAL_RECON_BATCH_SIZE = 100

# The one status a dispatched payout can get stuck in.
#
# 'processed' means the gateway accepted the transfer; the webhook is what moves it
# to 'completed' or 'failed'. A missed webhook therefore leaves it here, and
# nowhere else. The previous filter looked for 'processing' -- which is not a
# status this system has: it is absent from WithdrawalRequest.STATUS_CHOICES and no
# code writes it -- and for 'approved', which is the state BEFORE dispatch and so
# has no payout reference to poll. The two conditions were mutually exclusive, so
# the queryset could never match anything.
WITHDRAWAL_STUCK_STATUS = 'processed'

# Whether reconciliation may mark a payout FAILED.
#
# Deliberately off. The webhook failure path refunds the driver wallet; this task
# does not. Enabling the failure branch without settling that asymmetry would
# create a path that leaves a driver debited with a failed withdrawal and no
# refund. See runbooks/payout-reconciliation-contract.md step 3 -- it is a policy
# decision, not an implementation detail.
WITHDRAWAL_RECON_FAILURE_HANDLING_ENABLED = False


def stuck_withdrawal_candidates(now=None, grace_minutes=None, lookback_hours=None,
                                limit=None):
    """Dispatched payouts that a missed webhook may have left unresolved.

    Provider-independent: it reads only our own state machine, so it is fully
    testable without any gateway.

    The window is measured on processed_at, not requested_at. Maker/checker
    approval is a human step, so a withdrawal requested on Friday and approved on
    Monday was already outside a 48-hour window measured from the request the
    moment it was dispatched -- it could never have been reconciled even once the
    other defects were fixed.

    processed_at is safe to use here even though the column is overloaded
    (approval, rejection and reconciliation all stamp it) because the selection is
    pinned to status='processed', and both code paths that set that status set
    processed_at in the same save. Within this selected set it therefore means
    exactly "the transfer was dispatched at". Outside it the meaning differs, which
    is why the status filter is not optional.
    """
    from datetime import timedelta

    from servers.driver.models import WithdrawalRequest

    now = now or timezone.now()
    grace_minutes = (WITHDRAWAL_RECON_GRACE_MINUTES if grace_minutes is None
                     else grace_minutes)
    lookback_hours = (WITHDRAWAL_RECON_LOOKBACK_HOURS if lookback_hours is None
                      else lookback_hours)
    limit = WITHDRAWAL_RECON_BATCH_SIZE if limit is None else limit

    grace_cutoff = now - timedelta(minutes=grace_minutes)
    lookback_cutoff = now - timedelta(hours=lookback_hours)

    return (
        WithdrawalRequest.objects.filter(
            status=WITHDRAWAL_STUCK_STATUS,
            processed_at__lt=grace_cutoff,
            processed_at__gt=lookback_cutoff,
        )
        # No reference means nothing to ask the provider about. Defensive: dispatch
        # writes the reference and the status together, so a 'processed' row
        # without one is itself an anomaly, better seen in the logs than polled.
        .exclude(Q(payout_reference_id__isnull=True) | Q(payout_reference_id=''))
        .order_by('processed_at')[:limit]
    )


@shared_task(name='payments.reconcile_stuck_withdrawals')
def reconcile_stuck_withdrawals():
    """Sweep dispatched driver payouts a missed webhook may have left unresolved.

    Twin to reconcile_stuck_payments. A missed Cashfree payout webhook leaves the
    withdrawal in 'processed' forever even though the gateway has long since
    settled or failed the transfer.

    Selection lives in stuck_withdrawal_candidates, which is provider-independent
    and separately tested. Provider polling is NOT implemented: the contract has
    not been verified, so this task currently reports how many payouts are stuck
    and resolves none. The failure branch is additionally gated behind
    WITHDRAWAL_RECON_FAILURE_HANDLING_ENABLED, because the webhook refunds the
    wallet on failure and this task does not.

    The driver wallet is debited at REQUEST time, never here. Nothing in this task
    moves money.
    """
    from servers.driver.models import WithdrawalRequest
    from servers.payments.payment_gateways.factory import (
        get_payment_gateway_for_payouts,
    )
    from datetime import timedelta

    now = timezone.now()

    try:
        gateway = get_payment_gateway_for_payouts()
    except Exception as e:
        logger.error(f"reconcile_stuck_withdrawals: cannot get payout gateway: {e}")
        return {'ok': False, 'reason': 'no gateway'}
    if not gateway:
        logger.warning("reconcile_stuck_withdrawals: payout gateway unavailable")
        return {'ok': False, 'reason': 'no gateway'}

    stuck = list(stuck_withdrawal_candidates(now=now))

    settled = 0
    failed = 0
    skipped = 0

    # PROVIDER BOUNDARY -- explicitly unimplemented.
    #
    # Cashfree's payout-status endpoint is not abstracted on CashfreeGateway, and
    # the contract (endpoint, identifier, request shape, response envelope, status
    # vocabulary) has not been verified against the provider. Guessing it would be
    # guessing with real money, so nothing is polled until it is proven in sandbox.
    #
    # The candidate count is reported anyway, and that is the operational point of
    # this change: ops can now see HOW MANY payouts are sitting unresolved. The
    # previous version could not report that, because its queryset never matched
    # anything.
    status_fn = getattr(gateway, 'get_payout_status', None)
    if not callable(status_fn):
        logger.warning(
            'recon-withdrawal: provider status polling is unimplemented; '
            'selected %s stuck payout(s) and resolved none',
            len(stuck),
            extra={'event': 'withdrawal_recon_unimplemented',
                   'candidates': len(stuck)},
        )
        return {
            'ok': False,
            'reason': 'provider_polling_unimplemented',
            'candidates': len(stuck),
            'candidate_ids': [w.id for w in stuck],
        }

    for w in stuck:
        try:
            info = status_fn(w.payout_reference_id)
        except Exception as e:
            logger.warning(
                f"recon-withdrawal: get_payout_status failed for {w.payout_reference_id}: {e}"
            )
            skipped += 1
            continue
        if not info:
            skipped += 1
            continue

        gateway_state = str(info.get('status', '')).upper()
        try:
            with transaction.atomic():
                row = WithdrawalRequest.objects.select_for_update().get(pk=w.pk)
                if row.status not in ('processing', 'approved'):
                    continue  # someone else moved it
                if gateway_state in ('SUCCESS', 'COMPLETED', 'PROCESSED'):
                    row.status = 'completed'
                    row.payout_status = gateway_state
                    row.processed_at = timezone.now()
                    row.save(update_fields=['status', 'payout_status', 'processed_at'])
                    settled += 1
                elif (
                    gateway_state in ('FAILED', 'REVERSED', 'CANCELLED', 'REJECTED')
                    and not WITHDRAWAL_RECON_FAILURE_HANDLING_ENABLED
                ):
                    # The provider says this failed, but the webhook failure path
                    # refunds the wallet and this task does not. Marking it failed
                    # here would leave the driver debited with nothing returned, so
                    # it is left for a human until the refund policy is settled.
                    logger.warning(
                        'recon-withdrawal: withdrawal %s looks failed at the '
                        'provider (%s) but failure handling is disabled; leaving '
                        'it for manual review',
                        row.pk, gateway_state,
                    )
                    skipped += 1
                elif gateway_state in ('FAILED', 'REVERSED', 'CANCELLED', 'REJECTED'):
                    row.status = 'failed'
                    row.payout_status = gateway_state
                    row.failure_reason = (info.get('failure_reason') or 'gateway reported failure')[:255]
                    row.failure_count = (row.failure_count or 0) + 1
                    row.last_failure_at = timezone.now()
                    row.save(update_fields=[
                        'status', 'payout_status', 'failure_reason',
                        'failure_count', 'last_failure_at',
                    ])
                    failed += 1
                else:
                    skipped += 1
        except Exception as e:
            logger.error(f"recon-withdrawal: failed to settle {w.pk}: {e}")
            skipped += 1

    logger.info(
        f"recon-withdrawal: settled={settled} failed={failed} skipped={skipped}"
    )
    return {
        'ok': True,
        'settled': settled,
        'failed': failed,
        'skipped': skipped,
    }
