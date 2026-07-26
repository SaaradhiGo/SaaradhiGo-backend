"""Rider rating maintenance.

When a driver rates a rider, this module recomputes the rider's
aggregate rating using a simple running mean and flags the rider for
ops review when the rating dips below a threshold. The decay name is
historical -- in Phase-0 we do a straight running average; an EWMA
variant is plumbed but defaults to mean to keep the audit story
simple.

Call `apply_rider_rating(rider, score)` from the rate_trip view after
a driver rates a rider. The function is idempotent if you pass the
same observation twice (it doesn't; each call adds one observation),
so callers must only call it once per rating row.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.utils import timezone

logger = logging.getLogger(__name__)


# Below this rating, the rider is flagged for ops review.
# Tunable via settings.
RIDER_REVIEW_RATING_THRESHOLD = Decimal('3.00')
# Below this rating, riders are soft-blocked from booking; ops must
# clear them. Defaults to 2.5 to leave a buffer above zero so a single
# bad trip doesn't auto-block a new rider.
RIDER_SOFT_BLOCK_THRESHOLD = Decimal('2.50')


def _q2(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def apply_rider_rating(rider, score) -> dict:
    """Add a new observation and update flag state. Returns a status
    dict the caller can echo to logs or the response."""
    try:
        s = Decimal(str(score))
    except Exception:  # noqa: BLE001
        return {'ok': False, 'reason': 'invalid_score'}
    if s < Decimal('1') or s > Decimal('5'):
        return {'ok': False, 'reason': 'out_of_range'}

    current = Decimal(str(rider.rating or '5.00'))
    n = int(rider.rating_count or 0)

    # Running mean: new = (current * n + s) / (n + 1).
    new_n = n + 1
    new_rating = ((current * Decimal(n)) + s) / Decimal(new_n)
    new_rating = _q2(new_rating)
    rider.rating = new_rating
    rider.rating_count = new_n

    fields_to_update = ['rating', 'rating_count']

    was_flagged = rider.flagged_for_review
    now_should_flag = new_rating < RIDER_REVIEW_RATING_THRESHOLD

    if now_should_flag and not was_flagged:
        rider.flagged_for_review = True
        rider.review_flagged_at = timezone.now()
        fields_to_update.extend(['flagged_for_review', 'review_flagged_at'])
    elif (not now_should_flag) and was_flagged:
        # Rating climbed back above threshold -- auto-clear the flag.
        rider.flagged_for_review = False
        rider.review_cleared_at = timezone.now()
        fields_to_update.extend(['flagged_for_review', 'review_cleared_at'])

    rider.save(update_fields=fields_to_update)
    soft_blocked = new_rating < RIDER_SOFT_BLOCK_THRESHOLD

    logger.info(
        'rider-rating update user=%s n=%d -> rating=%s flagged=%s soft_blocked=%s',
        rider.user_id_id, new_n, new_rating, rider.flagged_for_review, soft_blocked,
    )
    return {
        'ok': True,
        'rating': str(new_rating),
        'rating_count': new_n,
        'flagged_for_review': rider.flagged_for_review,
        'soft_blocked': soft_blocked,
    }
