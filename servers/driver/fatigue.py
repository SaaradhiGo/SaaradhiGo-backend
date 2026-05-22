"""Driver-side compliance services: MVA 2020 fatigue cap + cancel penalty.

Two responsibilities right now:

1. **Fatigue cap.** MVA Aggregator Guidelines 2020 paragraph 9 caps a
   driver's active duration at 12 hours in any rolling 24-hour window.
   We model this with a DriverSession ledger -- one row per online ->
   offline interval -- and compute the rolling total at trip-accept
   time. A driver who breaches the cap is forced offline; the lockout
   sits on `Driver.fatigue_lockout_until` so checks are O(1) until it
   expires.

2. **Cancellation penalties.** Drivers who cancel a trip AFTER
   accepting it are tracked in DriverCancellation. Three cancels in
   24 hours triggers a 1-hour online lockout and a 0.1 rating
   deduction. The same `fatigue_lockout_until` column carries the
   cancel lockout, so a single check covers both.

This module owns ALL writes to `Driver.fatigue_lockout_until`. Callers
ask high-level questions (`is_locked_out`, `record_session_start`,
`apply_cancellation_penalty`) and never touch the column directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (kept module-local for readability; could move to settings)
# ---------------------------------------------------------------------------

# MVA 2020 cap: 12h active in any rolling 24h window.
FATIGUE_CAP_SECONDS = 12 * 3600
FATIGUE_WINDOW_SECONDS = 24 * 3600

# Mandatory rest after hitting the cap before going online again.
FATIGUE_LOCKOUT_SECONDS = 8 * 3600

# Cancellation penalty rule.
CANCEL_WINDOW_SECONDS = 24 * 3600
CANCEL_PENALTY_THRESHOLD = 3       # Nth cancel within window triggers lockout
CANCEL_LOCKOUT_SECONDS = 60 * 60   # 1h online lockout
CANCEL_RATING_PENALTY = Decimal('0.10')


# ---------------------------------------------------------------------------
# Fatigue
# ---------------------------------------------------------------------------

@dataclass
class FatigueStatus:
    locked: bool
    reason: str = ''
    locked_until: Optional[object] = None  # datetime
    active_seconds_24h: int = 0
    minutes_until_cap: int = 0

    def to_dict(self):
        return {
            'locked': self.locked,
            'reason': self.reason,
            'locked_until': self.locked_until.isoformat() if self.locked_until else None,
            'active_seconds_24h': self.active_seconds_24h,
            'minutes_until_cap': self.minutes_until_cap,
        }


def compute_active_seconds_24h(driver, now=None) -> int:
    """Sum of completed + currently-running session seconds within the
    last 24 hours. A session that started >24h ago AND is still open
    counts only the portion inside the window."""
    from servers.driver.models import DriverSession

    now = now or timezone.now()
    window_start = now - timedelta(seconds=FATIGUE_WINDOW_SECONDS)
    total = 0

    sessions = DriverSession.objects.filter(
        driver=driver,
        started_at__lt=now,
    ).filter(
        Q(ended_at__isnull=True) | Q(ended_at__gt=window_start),
    )
    for s in sessions:
        seg_start = max(s.started_at, window_start)
        seg_end = s.ended_at if s.ended_at else now
        if seg_end <= seg_start:
            continue
        total += int((seg_end - seg_start).total_seconds())
    return total


def get_fatigue_status(driver, now=None) -> FatigueStatus:
    """Whether the driver is allowed to be online / accept a trip."""
    now = now or timezone.now()
    locked_until = driver.fatigue_lockout_until

    if locked_until and locked_until > now:
        return FatigueStatus(
            locked=True,
            reason='lockout',
            locked_until=locked_until,
            active_seconds_24h=compute_active_seconds_24h(driver, now=now),
            minutes_until_cap=0,
        )

    active = compute_active_seconds_24h(driver, now=now)
    if active >= FATIGUE_CAP_SECONDS:
        # Stamp the lockout so subsequent checks are O(1).
        until = now + timedelta(seconds=FATIGUE_LOCKOUT_SECONDS)
        type(driver).objects.filter(pk=driver.pk).update(fatigue_lockout_until=until)
        driver.fatigue_lockout_until = until
        return FatigueStatus(
            locked=True,
            reason='cap_reached',
            locked_until=until,
            active_seconds_24h=active,
            minutes_until_cap=0,
        )

    return FatigueStatus(
        locked=False,
        reason='',
        locked_until=None,
        active_seconds_24h=active,
        minutes_until_cap=max(0, (FATIGUE_CAP_SECONDS - active) // 60),
    )


def is_locked_out(driver, now=None) -> bool:
    return get_fatigue_status(driver, now=now).locked


def record_session_start(driver, now=None):
    """Open a DriverSession (or return the existing open one).

    Called from the driver-online WS handler. Idempotent: if the
    driver already has an open session, returns it instead of
    creating a duplicate.
    """
    from servers.driver.models import DriverSession

    now = now or timezone.now()
    existing = DriverSession.objects.filter(
        driver=driver, ended_at__isnull=True,
    ).order_by('-started_at').first()
    if existing:
        return existing
    return DriverSession.objects.create(driver=driver, started_at=now)


def record_session_end(driver, now=None, reason='offline'):
    """Close the driver's open session (no-op if no open session)."""
    from servers.driver.models import DriverSession

    now = now or timezone.now()
    session = DriverSession.objects.filter(
        driver=driver, ended_at__isnull=True,
    ).order_by('-started_at').first()
    if not session:
        return None

    if now <= session.started_at:
        session.ended_at = session.started_at  # zero-duration safety
    else:
        session.ended_at = now
    session.duration_seconds = int(
        (session.ended_at - session.started_at).total_seconds()
    )
    session.end_reason = reason
    session.save(update_fields=['ended_at', 'duration_seconds', 'end_reason'])
    return session


# ---------------------------------------------------------------------------
# Cancellation penalty
# ---------------------------------------------------------------------------

@dataclass
class CancelPenalty:
    recent_count: int
    locked: bool
    locked_until: Optional[object] = None
    rating_delta: Decimal = Decimal('0.00')

    def to_dict(self):
        return {
            'recent_count': self.recent_count,
            'locked': self.locked,
            'locked_until': self.locked_until.isoformat() if self.locked_until else None,
            'rating_delta': str(self.rating_delta),
        }


def apply_cancellation_penalty(driver, trip, reason: str, note: str = '', now=None) -> CancelPenalty:
    """Record a driver-side cancellation + apply the rolling penalty."""
    from servers.driver.models import DriverCancellation

    now = now or timezone.now()
    window_start = now - timedelta(seconds=CANCEL_WINDOW_SECONDS)

    with transaction.atomic():
        DriverCancellation.objects.create(
            driver=driver, trip=trip, reason=reason, note=note,
        )
        recent_count = DriverCancellation.objects.filter(
            driver=driver, created_at__gte=window_start,
        ).count()

        locked = recent_count >= CANCEL_PENALTY_THRESHOLD
        locked_until = None
        rating_delta = Decimal('0.00')

        if locked:
            locked_until = now + timedelta(seconds=CANCEL_LOCKOUT_SECONDS)
            cur = driver.fatigue_lockout_until
            if cur is None or cur < locked_until:
                driver.fatigue_lockout_until = locked_until
            rating_delta = CANCEL_RATING_PENALTY
            try:
                new_rating = max(
                    Decimal('0.00'), Decimal(str(driver.ratings)) - rating_delta,
                )
                driver.ratings = new_rating
            except Exception:  # noqa: BLE001
                logger.exception('Failed to apply cancel rating penalty driver=%s', driver.pk)
            driver.save(update_fields=['fatigue_lockout_until', 'ratings'])

    logger.info(
        'cancel-penalty driver=%s trip=%s reason=%s recent=%d locked=%s',
        driver.pk, trip.pk, reason, recent_count, locked,
    )
    return CancelPenalty(
        recent_count=recent_count,
        locked=locked,
        locked_until=locked_until,
        rating_delta=rating_delta,
    )
