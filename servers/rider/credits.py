"""Closed-loop credit issuance.

Phase-0 operates the rider wallet as a *credit store*, not a PPI:

  * External top-ups are disabled (`settings.WALLET_TOPUPS_ENABLED = False`).
  * Money enters the wallet only via:
      - issue_refund_credit(...)   trip cancellations the rider opts to
                                   receive as instant credit instead of a
                                   5-7 day card/UPI refund
      - issue_promo_credit(...)    marketing / referral / first-ride
                                   cashback
      - issue_support_credit(...)  customer-service goodwill credits
  * Spends are unchanged (`wallet_payment` still debits the balance for a
    trip).

All three helpers below are:
  - idempotent: same idempotency_key never credits twice, even on retries.
  - cap-enforced: a credit that would push the balance above
    `RIDER_CREDIT_BALANCE_CAP` is refused with reason='cap_exceeded'.
  - atomic + row-locked: wallet + transaction commit together.

Use these helpers from anywhere in the codebase that needs to issue
credit. Do not write directly to Wallet.balance.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from servers.rider.models import Notification, Wallet, WalletTransaction

logger = logging.getLogger(__name__)

User = get_user_model()


@dataclass
class CreditResult:
    ok: bool
    transaction_id: Optional[int]
    new_balance: Decimal
    reason: str = ''  # empty on success; e.g. 'cap_exceeded', 'duplicate', 'invalid_amount'

    def to_dict(self):
        return {
            'ok': self.ok,
            'transaction_id': self.transaction_id,
            'new_balance': str(self.new_balance),
            'reason': self.reason,
        }


def _to_decimal(amount) -> Optional[Decimal]:
    try:
        v = Decimal(str(amount)).quantize(Decimal('0.01'))
        if v <= 0:
            return None
        return v
    except (InvalidOperation, TypeError, ValueError):
        return None


def _issue_credit(
    user,
    amount: Decimal,
    purpose: str,
    idempotency_key: str,
    reference_id: Optional[str] = None,
    notify_title: Optional[str] = None,
    notify_message: Optional[str] = None,
) -> CreditResult:
    """Generic credit issuer. Callers should use the typed wrappers below."""
    cap = Decimal(str(getattr(settings, 'RIDER_CREDIT_BALANCE_CAP', '2000.00')))

    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(
            user_id=user,
            scope=Wallet.SCOPE_RIDER,
            defaults={'balance': Decimal('0.00')},
        )
        current = Decimal(str(wallet.balance))
        projected = current + amount

        if projected > cap:
            return CreditResult(
                ok=False,
                transaction_id=None,
                new_balance=current,
                reason='cap_exceeded',
            )

        # Idempotency via the unique constraint on
        # WalletTransaction.idempotency_key. The create runs inside an
        # inner savepoint so a unique-violation does not poison the
        # outer transaction -- without the savepoint, subsequent ORM
        # queries inside the atomic block raise
        # TransactionManagementError ("can't execute queries until end
        # of atomic block").
        try:
            with transaction.atomic():
                txn = WalletTransaction.objects.create(
                    user_id=user,
                    amount=amount,
                    txn_type='credit',
                    status='completed',
                    purpose=purpose,
                    reference_id=reference_id or '',
                    idempotency_key=idempotency_key,
                    payment_gateway='cashfree',  # historical default; not used for closed-loop
                )
        except IntegrityError:
            existing = WalletTransaction.objects.filter(
                idempotency_key=idempotency_key,
                user_id=user,
            ).first()
            if existing:
                return CreditResult(
                    ok=True,
                    transaction_id=existing.id,
                    new_balance=current,
                    reason='duplicate',
                )
            raise

        wallet.balance = projected
        wallet.save(update_fields=['balance'])

    # Best-effort notification outside the transaction (don't fail the credit
    # if the notification table is unavailable for any reason).
    if notify_title and notify_message:
        try:
            Notification.objects.create(
                user_id=user,
                title=notify_title,
                message=notify_message,
                notif_type='wallet',
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('Credit notification create failed for user %s: %s', user.id, exc)

    logger.info(
        'Credit issued: user=%s amount=%s purpose=%s new_balance=%s',
        user.id, amount, purpose, projected,
    )
    return CreditResult(
        ok=True,
        transaction_id=txn.id,
        new_balance=projected,
        reason='',
    )


def issue_refund_credit(user, amount, trip_id, idempotency_key) -> CreditResult:
    """Credit a rider for a cancelled/refunded trip as VahanGo Credits.

    Prefer this over `gateway.create_refund(...)` when the rider has opted
    in to "instant credit" (vs the 5-7 day refund-to-original-method).
    """
    amt = _to_decimal(amount)
    if amt is None:
        return CreditResult(False, None, Decimal('0.00'), reason='invalid_amount')
    return _issue_credit(
        user=user,
        amount=amt,
        purpose='refund',
        idempotency_key=idempotency_key,
        reference_id=f'TRIP_{trip_id}',
        notify_title='Refund credited as VahanGo Credits',
        notify_message=(
            f"Rs.{amt} for Trip #{trip_id} has been added to your VahanGo "
            f"Credits. Use the balance towards your next ride."
        ),
    )


def issue_promo_credit(user, amount, campaign, idempotency_key) -> CreditResult:
    """Credit a rider with a promo / referral / cashback amount."""
    amt = _to_decimal(amount)
    if amt is None:
        return CreditResult(False, None, Decimal('0.00'), reason='invalid_amount')
    return _issue_credit(
        user=user,
        amount=amt,
        purpose=f'promo:{campaign}',
        idempotency_key=idempotency_key,
        reference_id=campaign,
        notify_title='You got VahanGo Credits!',
        notify_message=(
            f"Rs.{amt} promo credit added to your VahanGo Credits. "
            f"Apply at checkout on your next ride."
        ),
    )


def issue_support_credit(user, amount, ticket_ref, idempotency_key, reason_note='') -> CreditResult:
    """Customer-service goodwill credit (issued by support staff via admin)."""
    amt = _to_decimal(amount)
    if amt is None:
        return CreditResult(False, None, Decimal('0.00'), reason='invalid_amount')
    return _issue_credit(
        user=user,
        amount=amt,
        purpose=f'support:{ticket_ref}',
        idempotency_key=idempotency_key,
        reference_id=ticket_ref,
        notify_title='VahanGo Credits added by Support',
        notify_message=(
            f"Rs.{amt} credit applied to your account."
            + (f" ({reason_note})" if reason_note else "")
        ),
    )
