"""Promotional quiet hours: no promotional send between 21:00 and 10:00 recipient-local.

Per-country config (`settings.NOTIFY_QUIET_HOURS`). Applies to PROMOTIONAL messages only;
transactional messages (a commission notice, a payout problem) go at any hour. A promotional
message that falls in the window is HELD (`not_before` set to the window's end), not dropped.

GO-LIVE LEGAL ITEM: the exact TRAI (India) and TCPA (US) windows need legal confirmation
before any live promotional send. 21:00-10:00 is a conservative engineering default, not a
legal determination.
"""

from datetime import datetime, time, timedelta, timezone as dt_timezone

from . import config


def in_quiet_hours(local_hour, window):
    start, end = window
    if start == end:
        return False
    if start > end:                       # wraps midnight: 21..10
        return local_hour >= start or local_hour < end
    return start <= local_hour < end


def release_time(now, tz, country):
    """When a promotional message created `now` may go: None if it may go immediately."""
    window = config.quiet_hours(country)
    local = now.astimezone(tz)
    if not in_quiet_hours(local.hour, window):
        return None
    start, end = window
    day = local.date()
    if start > end and local.hour >= start:
        day = day + timedelta(days=1)     # evening: the window ends tomorrow morning
    release_local = datetime.combine(day, time(hour=end), tzinfo=tz)
    return release_local.astimezone(dt_timezone.utc)
