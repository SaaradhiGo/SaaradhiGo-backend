"""Which "today" a number belongs to, in a platform that spans timezones.

THE DEFECT THIS CLOSES
----------------------
`TIME_ZONE = 'Asia/Kolkata'` is a single global, and every "today" in the product was
computed with `timezone.localtime(timezone.now())` against it.

That is right for India and silently wrong for anywhere else. A driver in New York asking
what they earned today gets a window that opened at **14:30 the previous afternoon**,
Eastern — so their morning's rides are filed under yesterday, and the figure they
reconcile their cash against does not match the shift they just worked. In California it
is 11:30 the previous morning.

Nothing crashes. The number is just quietly about a different day than the one the person
asking is living in, and no test catches it because the test and the code share the same
global.

THE RULE
--------
A money total belongs to the market that produced it. So "today" is resolved from the
**zone**, not from the deployment:

    zone -> ServiceZone.timezone_name -> that market's local midnight

and only falls back to the global setting when there is no zone to ask.

WHY THE DRIVER'S TIMEZONE COMES FROM THEIR TRIPS
------------------------------------------------
There is no `home_market` on `Driver`, and adding one would need a migration plus a
backfill that guesses. A driver's most recent trip's zone is the same answer without the
guesswork: a driver operates in one market, and if they move markets their "today"
follows them, which is the behaviour you want anyway.

The lookup is one indexed query (`trip(driver_id, -requested_at)` already exists) and it
is cached per request by the caller where it matters.
"""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

from base.countries import get_country

logger = logging.getLogger(__name__)


def _zoneinfo(name):
    """ZoneInfo for a name, falling back to the deployment default.

    Never raises: a typo'd IANA name on a zone must not 500 an earnings screen.
    It is logged at ERROR because the number that comes back will be about the
    wrong day.
    """
    if name:
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            logger.error(
                'invalid timezone_name %r; falling back to settings.TIME_ZONE. '
                'Every "today" computed for this zone is now about the wrong day.',
                name,
            )
    try:
        return ZoneInfo(settings.TIME_ZONE)
    except Exception:
        return timezone.get_current_timezone()


def zone_timezone(zone):
    """The IANA timezone a `ServiceZone` operates in."""
    if zone is None:
        return _zoneinfo(None)
    name = getattr(zone, 'timezone_name', None)
    if not name:
        name = get_country(getattr(zone, 'country', None)).default_timezone
    return _zoneinfo(name)


def trip_timezone(trip):
    """The timezone a trip happened in."""
    return zone_timezone(getattr(trip, 'zone', None))


def driver_timezone(driver):
    """The timezone a driver's 'today' should be measured in.

    Resolved from their most recent trip's zone. A driver with no trips yet gets
    the deployment default, which is correct: there is no market to infer.
    """
    if driver is None:
        return _zoneinfo(None)
    try:
        from servers.ride.models import Trip

        name = (Trip.objects
                .filter(driver_id=driver)
                .exclude(zone=None)
                .order_by('-requested_at')
                .values_list('zone__timezone_name', flat=True)
                .first())
    except Exception as exc:
        logger.warning('could not resolve driver timezone: %s', exc)
        name = None
    return _zoneinfo(name)


def local_day_start(tzinfo, now=None):
    """Midnight today, in `tzinfo`, as an aware datetime.

    Built by combining the local DATE with `time.min` rather than by calling
    `.replace(hour=0, ...)` on a converted datetime. The replace approach is
    wrong across a DST transition: on a spring-forward day in a US zone local
    midnight may not exist as that wall time, and `.replace` produces a
    non-existent instant that compares strangely. `datetime.combine` plus a
    tz-aware construction lets zoneinfo resolve the fold.
    """
    now = now or timezone.now()
    local = now.astimezone(tzinfo)
    return datetime.combine(local.date(), time.min, tzinfo=tzinfo)


def local_day_bounds(tzinfo, now=None):
    """(start, end) of the local day, end-exclusive."""
    start = local_day_start(tzinfo, now=now)
    # Add a day to the DATE, not 24 hours to the instant: a DST day is 23 or 25
    # hours long, and adding a fixed timedelta would clip or overlap it.
    next_date = (start.date() + timedelta(days=1))
    return start, datetime.combine(next_date, time.min, tzinfo=tzinfo)


def local_month_start(tzinfo, now=None):
    start = local_day_start(tzinfo, now=now)
    return datetime.combine(start.date().replace(day=1), time.min, tzinfo=tzinfo)
