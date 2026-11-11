"""Stuck trips: an active trip that needs a person to look at it.

THE RULE (owner, 2026-11-03), read per country from the active
`SafetyCountryConfig` so each country can tune it:

  * overrun        -- in progress for more than `stuck_overrun_percent` of its
                      estimated duration (200 = twice as long);
  * no_location    -- accepted, arrived or in progress, and no driver location
                      for `stuck_no_location_minutes`;
  * not_started    -- accepted (or arrived) and not started after
                      `stuck_not_started_minutes` from acceptance.

A country with no config, or a NULL field, has that check off: nothing falls
back to another country's numbers.

This is not the liveness detector's "stale" (`ride.liveness`), which decides
whether a trip is still alive and drives the release flow. "Stuck" is the
operator's view: what to look at now. Nothing here changes a trip.

Active trips are a small set however large the history is (indexed by status),
so the rule runs in Python over that set; the candidates query is the only one.
"""

from datetime import timedelta

from django.utils import timezone

OVERRUN, NO_LOCATION, NOT_STARTED = 'overrun', 'no_location', 'not_started'

#: What the console says for each reason.
WORDS = {
    OVERRUN: 'Running over twice its estimated time',
    NO_LOCATION: 'No driver location',
    NOT_STARTED: 'Accepted but not started',
}

_RUNNING = ('accepted', 'reached', 'in_progress')


def _country_of(trip):
    zone = getattr(trip, 'zone', None)
    node = getattr(zone, 'geo_node', None) if zone is not None else None
    if node is not None:
        parts = [p for p in str(node.path).split('/') if p]
        if len(parts) > 1:
            return parts[1]
    return (getattr(zone, 'country', None) or '').upper() or None


def stuck_reason(trip, config, now=None):
    """(reason, minutes) for a stuck trip, else (None, None). `config` is the
    trip's country's active SafetyCountryConfig, or None (every check off)."""
    if config is None:
        return None, None
    now = now or timezone.now()
    code = trip.status_id.status_code if trip.status_id else ''
    if code not in _RUNNING:
        return None, None

    if code == 'in_progress' and trip.started_at and config.stuck_overrun_percent:
        estimate = float(trip.estimated_duration_min or 0)
        if estimate > 0:
            limit = timedelta(minutes=estimate * config.stuck_overrun_percent / 100)
            running = now - trip.started_at
            if running > limit:
                return OVERRUN, int(running.total_seconds() // 60)

    if config.stuck_no_location_minutes:
        last = trip.last_driver_activity_at or trip.started_at or trip.accepted_at
        if last and now - last > timedelta(minutes=config.stuck_no_location_minutes):
            return NO_LOCATION, int((now - last).total_seconds() // 60)

    if (code in ('accepted', 'reached') and trip.accepted_at and trip.started_at is None
            and config.stuck_not_started_minutes):
        waited = now - trip.accepted_at
        if waited > timedelta(minutes=config.stuck_not_started_minutes):
            return NOT_STARTED, int(waited.total_seconds() // 60)

    return None, None


def stuck_trips(queryset, now=None, limit=500):
    """[(trip, reason, minutes)] for the stuck trips in an already SCOPED
    queryset, longest-stuck first. Reads only active trips."""
    from servers.safety.config import active_configs

    now = now or timezone.now()
    configs = active_configs()
    candidates = (queryset.filter(status_id__status_code__in=_RUNNING)
                  .select_related('status_id', 'zone', 'zone__geo_node', 'driver_id__user_id')
                  .order_by('accepted_at')[:limit * 4])
    out = []
    for trip in candidates:
        reason, minutes = stuck_reason(trip, configs.get(_country_of(trip)), now)
        if reason:
            out.append((trip, reason, minutes))
    out.sort(key=lambda row: -row[2])
    return out[:limit]
