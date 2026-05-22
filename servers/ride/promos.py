"""Promo code service.

Two operations:

1. **Apply (preview)** -- the rider enters a code on the fare-estimate
   screen. We validate it against the current quote and return the
   computed discount; we do NOT yet decrement the global counter.

2. **Redeem (commit)** -- once the trip is created, we record a
   PromoRedemption row that ties the code to the trip. The atomic
   increment + per-user cap check happens here. If two riders race
   on the last available redemption, the second one gets
   `PROMO_LIMIT_REACHED`.

Validation rules (in this order; first failure wins):
  PROMO_NOT_FOUND       -- no such code, or inactive
  PROMO_NOT_YET_VALID   -- valid_from in the future
  PROMO_EXPIRED         -- valid_to in the past
  PROMO_WRONG_ZONE      -- code is zone-scoped and the pickup is
                           outside it
  PROMO_MIN_FARE        -- quoted fare < promo.min_fare
  PROMO_USER_LIMIT      -- this user already used the code N times
  PROMO_LIMIT_REACHED   -- global cap hit
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)


@dataclass
class PromoResult:
    ok: bool
    code: str = ''
    discount_amount: Decimal = Decimal('0.00')
    final_fare: Decimal = Decimal('0.00')
    description: str = ''
    reason: str = ''   # error code; '' on success

    def to_dict(self):
        return {
            'ok': self.ok,
            'code': self.code,
            'discount_amount': str(self.discount_amount),
            'final_fare': str(self.final_fare),
            'description': self.description,
            'reason': self.reason,
        }


def _two(v: Decimal) -> Decimal:
    return v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _compute_discount(promo, fare: Decimal) -> Decimal:
    if promo.discount_type == 'flat':
        return min(promo.discount_value, fare)
    # percent
    pct = promo.discount_value / Decimal('100.00')
    raw = (fare * pct).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if promo.max_discount_amount is not None:
        raw = min(raw, promo.max_discount_amount)
    # Discount can never exceed the fare itself.
    return min(raw, fare)


def apply_promo(code: str, user, fare: Decimal, zone=None, at=None) -> PromoResult:
    """Compute the discount for a code WITHOUT redeeming it.

    Called from the fare-estimate screen so the rider can preview the
    final price before committing to the ride. The atomic redemption
    happens later via `redeem_promo`.
    """
    from servers.ride.models import PromoCode, PromoRedemption

    at = at or timezone.now()
    fare = Decimal(str(fare))

    promo = PromoCode.objects.filter(
        code__iexact=(code or '').strip(), is_active=True,
    ).first()
    if not promo:
        return PromoResult(ok=False, code=code, reason='PROMO_NOT_FOUND')

    if promo.valid_from > at:
        return PromoResult(ok=False, code=code, reason='PROMO_NOT_YET_VALID')
    if promo.valid_to <= at:
        return PromoResult(ok=False, code=code, reason='PROMO_EXPIRED')

    if promo.zone_id and zone is not None and zone.id != promo.zone_id:
        return PromoResult(ok=False, code=code, reason='PROMO_WRONG_ZONE')
    if promo.zone_id and zone is None:
        return PromoResult(ok=False, code=code, reason='PROMO_WRONG_ZONE')

    if fare < promo.min_fare:
        return PromoResult(
            ok=False, code=code, reason='PROMO_MIN_FARE',
            description=f'Minimum fare Rs.{promo.min_fare} required.',
        )

    user_count = PromoRedemption.objects.filter(promo=promo, user=user).count()
    if user_count >= promo.max_per_user_redemptions:
        return PromoResult(ok=False, code=code, reason='PROMO_USER_LIMIT')

    if promo.max_total_redemptions is not None and promo.redemption_count >= promo.max_total_redemptions:
        return PromoResult(ok=False, code=code, reason='PROMO_LIMIT_REACHED')

    discount = _compute_discount(promo, fare)
    final = max(Decimal('0.00'), _two(fare - discount))
    return PromoResult(
        ok=True, code=promo.code,
        discount_amount=_two(discount), final_fare=final,
        description=promo.description, reason='',
    )


def redeem_promo(code: str, user, trip, fare: Decimal, zone=None, at=None) -> PromoResult:
    """Atomically claim a redemption for an existing trip.

    Re-validates the code (rules can have changed since apply_promo),
    then within a single transaction:
      * row-locks the promo
      * checks the global cap
      * inserts the PromoRedemption row
      * increments redemption_count

    The trip's final_fare is NOT updated here; the caller wires the
    discount into the trip's fare pricing.
    """
    from servers.ride.models import PromoCode, PromoRedemption

    at = at or timezone.now()
    fare = Decimal(str(fare))

    with transaction.atomic():
        try:
            promo = PromoCode.objects.select_for_update().get(
                code__iexact=(code or '').strip(),
            )
        except PromoCode.DoesNotExist:
            return PromoResult(ok=False, code=code, reason='PROMO_NOT_FOUND')

        if not promo.is_active:
            return PromoResult(ok=False, code=code, reason='PROMO_NOT_FOUND')
        if promo.valid_from > at:
            return PromoResult(ok=False, code=code, reason='PROMO_NOT_YET_VALID')
        if promo.valid_to <= at:
            return PromoResult(ok=False, code=code, reason='PROMO_EXPIRED')
        if promo.zone_id and (zone is None or zone.id != promo.zone_id):
            return PromoResult(ok=False, code=code, reason='PROMO_WRONG_ZONE')
        if fare < promo.min_fare:
            return PromoResult(ok=False, code=code, reason='PROMO_MIN_FARE')

        user_count = PromoRedemption.objects.filter(promo=promo, user=user).count()
        if user_count >= promo.max_per_user_redemptions:
            return PromoResult(ok=False, code=code, reason='PROMO_USER_LIMIT')

        if (
            promo.max_total_redemptions is not None
            and promo.redemption_count >= promo.max_total_redemptions
        ):
            return PromoResult(ok=False, code=code, reason='PROMO_LIMIT_REACHED')

        discount = _compute_discount(promo, fare)
        final = max(Decimal('0.00'), _two(fare - discount))

        PromoRedemption.objects.create(
            promo=promo, user=user, trip=trip,
            discount_amount=_two(discount),
        )
        promo.redemption_count = F('redemption_count') + 1
        promo.save(update_fields=['redemption_count', 'updated_at'])

    logger.info(
        'promo redeemed code=%s user=%s trip=%s amount=%s',
        promo.code, user.id, getattr(trip, 'id', None), discount,
    )
    return PromoResult(
        ok=True, code=promo.code,
        discount_amount=_two(discount), final_fare=final,
        description=promo.description, reason='',
    )
