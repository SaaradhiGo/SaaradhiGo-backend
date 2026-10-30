"""Support SLA: the clock on a ticket.

WHAT IS COUNTED
---------------
Two clocks, both from the ticket's `created_at`:

* first response: minutes until the first SUPPORT reply (an internal note
  and a system message do not stop it; `first_responded_at` is stamped by
  the reply write only);
* resolution: minutes until the ticket is closed.

The minutes come from `SupportSlaPolicy` rows (country + priority). With no
row for a pair the platform default below is used, so a ticket is never
without a clock. Due times are stored on the ticket at creation and
recomputed from `created_at` whenever the priority changes.

WAITING ON THE USER (owner ruling, AUDIT 27.36)
------------------------------------------------
WAITING_USER pauses the RESOLUTION clock only. `waiting_since` is stamped on
entry; on leaving WAITING_USER (an operator's status change, or the user
replying through the app) the elapsed time is added to
`resolution_paused_seconds` and `resolution_due_at` moves out by it. First
response never pauses. A paused ticket reads `paused`, not `breached`.
A safety ticket (`SupportTicket.is_safety`) never pauses: WAITING_USER is
allowed as a status but the clock keeps running.
"""

from datetime import timedelta

from django.utils import timezone

PRIORITIES = ('urgent', 'high', 'normal', 'low')
PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}

#: (first_response_minutes, resolution_minutes). Same for India and the US.
DEFAULT_MINUTES = {
    'urgent': (15, 240),
    'high': (60, 1440),
    'normal': (240, 2880),
    'low': (1440, 4320),
}

#: A ticket is "due soon" when its next deadline is this close.
DUE_SOON_MINUTES = 60


def policy_minutes(country, priority):
    """(first_response, resolution) minutes for a country and priority."""
    from .models import SupportSlaPolicy

    row = SupportSlaPolicy.objects.filter(country=country or '', priority=priority).first()
    if row is not None:
        return row.first_response_minutes, row.resolution_minutes
    return DEFAULT_MINUTES.get(priority, DEFAULT_MINUTES['normal'])


def apply_due_times(ticket, *, save=True):
    """Set both due times from `created_at` and the current priority."""
    first, resolution = policy_minutes(ticket.country, ticket.priority)
    base = ticket.created_at or timezone.now()
    ticket.first_response_due_at = base + timedelta(minutes=first)
    ticket.resolution_due_at = base + timedelta(
        minutes=resolution, seconds=ticket.resolution_paused_seconds or 0)
    if save and ticket.pk:
        type(ticket).objects.filter(pk=ticket.pk).update(
            first_response_due_at=ticket.first_response_due_at,
            resolution_due_at=ticket.resolution_due_at)
    return ticket


def enter_status(ticket, new_status, now):
    """Move `ticket` to `new_status` (in memory), keeping the pause honest.

    Returns the extra model fields the caller must save. Leaving
    WAITING_USER folds the waiting time into the resolution due time;
    entering it starts a pause, except for a safety ticket.
    """
    old = ticket.status
    changed = []
    if old == 'WAITING_USER' and new_status != 'WAITING_USER' and ticket.waiting_since:
        delta = max(0, int((now - ticket.waiting_since).total_seconds()))
        ticket.resolution_paused_seconds = (ticket.resolution_paused_seconds or 0) + delta
        if ticket.resolution_due_at is not None:
            ticket.resolution_due_at = ticket.resolution_due_at + timedelta(seconds=delta)
        ticket.waiting_since = None
        changed += ['resolution_paused_seconds', 'resolution_due_at', 'waiting_since']
    if new_status == 'WAITING_USER' and old != 'WAITING_USER' and not ticket.is_safety:
        ticket.waiting_since = now
        changed.append('waiting_since')
    ticket.status = new_status
    return changed


def is_paused(ticket):
    return (ticket.status == 'WAITING_USER' and ticket.waiting_since is not None
            and not ticket.is_safety)


def country_config(country):
    """The country's `SupportCountryConfig`, or None. No fallback, ever."""
    from .models import SupportCountryConfig

    if not country:
        return None
    return SupportCountryConfig.objects.filter(country=country).first()


def refund_review_sentence(country):
    """The rider-facing refund line. With no config there is no timeframe."""
    config = country_config(country)
    base = "We're reviewing your refund request."
    if config is None or not config.refund_review_hours:
        return base
    hours = config.refund_review_hours
    if hours >= 48:
        days = -(-hours // 24)
        span = f'{days} days'
    else:
        span = f'{hours} hour' + ('' if hours == 1 else 's')
    return f"{base} You'll hear from us within {span}."


def country_of_node(node):
    """The ISO country a GeoNode sits in (`/IN/IN-TG/IN-TG-HYD/` -> IN)."""
    from base.countries import COUNTRIES

    if node is None:
        return ''
    for part in str(node.path).split('/'):
        if part in COUNTRIES:
            return part
    return ''


def derive_country(ticket):
    """Where a new ticket belongs, from its anchor (trip, driver, rider)."""
    from base.countries import country_of_phone
    from servers.driver.models import Driver
    from servers.rider.models import Rider

    trip = ticket.trip_id
    if trip is not None:
        zone = trip.zone
        if zone is not None:
            code = country_of_node(zone.geo_node) or (zone.country or '').upper()
            if code:
                return code
    user = ticket.user_id
    driver = Driver.objects.filter(user_id=user).select_related('home_city').first()
    if driver is not None:
        return country_of_node(driver.home_city)
    rider = Rider.objects.filter(user_id=user).select_related('home_city').first()
    if rider is not None and rider.home_city_id:
        code = country_of_node(rider.home_city)
        if code:
            return code
    country = country_of_phone(getattr(user, 'phone_number', None))
    return country.code if country else ''


def _minutes_between(later, earlier):
    return int((later - earlier).total_seconds() // 60)


def _clock(due_at, done_at, now):
    """One clock: due, done, breached, minutes remaining (negative = late)."""
    if due_at is None:
        return {'due_at': None, 'done_at': done_at, 'breached': False,
                'minutes_remaining': None, 'met': done_at is not None}
    if done_at is not None:
        return {'due_at': due_at, 'done_at': done_at, 'breached': done_at > due_at,
                'minutes_remaining': None, 'met': done_at <= due_at}
    remaining = _minutes_between(due_at, now)
    return {'due_at': due_at, 'done_at': None, 'breached': due_at < now,
            'minutes_remaining': remaining, 'met': False}


def sla_state(ticket, now=None):
    """The ticket's SLA as the console shows it.

    `state`: 'closed' once the ticket is closed, else 'breached' when an
    open clock is past due, 'paused' while the resolution clock is paused
    (and nothing else is running late), 'due_soon' when the next deadline is
    within `DUE_SOON_MINUTES`, else 'ok'. `next` names the deadline that
    matters. A paused ticket's resolution clock is frozen: it neither
    breaches nor counts down.
    """
    now = now or timezone.now()
    closed = ticket.status == 'CLOSED'
    paused = (not closed) and is_paused(ticket)
    first = _clock(ticket.first_response_due_at, ticket.first_responded_at, now)
    resolution = _clock(ticket.resolution_due_at, ticket.resolved_at if closed else None, now)
    if paused and resolution['due_at'] is not None:
        resolution = {'due_at': resolution['due_at'], 'done_at': None, 'breached': False,
                      'minutes_remaining': _minutes_between(
                          resolution['due_at'], ticket.waiting_since),
                      'met': False}
    open_clocks = [(name, clock) for name, clock in
                   (('first_response', first), ('resolution', resolution))
                   if clock['due_at'] is not None and clock['done_at'] is None
                   and not (paused and name == 'resolution')]
    if closed:
        state, nxt, remaining = 'closed', None, None
    else:
        breached = [c for _, c in open_clocks if c['breached']]
        state = 'breached' if breached else 'ok'
        if open_clocks:
            nxt, clock = min(open_clocks, key=lambda item: item[1]['due_at'])
            remaining = clock['minutes_remaining']
            if state == 'ok' and remaining is not None and remaining <= DUE_SOON_MINUTES:
                state = 'due_soon'
        else:
            nxt, remaining = None, None
        if paused and state == 'ok':
            state = 'paused'
            if not open_clocks:
                nxt, remaining = 'resolution', resolution['minutes_remaining']
    return {
        'state': state,
        'next': nxt,
        'minutes_remaining': remaining,
        'breached': state == 'breached',
        'paused': paused,
        'paused_since': ticket.waiting_since.isoformat() if paused else None,
        'paused_seconds': ticket.resolution_paused_seconds or 0,
        'first_response': _iso_clock(first),
        'resolution': {**_iso_clock(resolution), 'paused': paused},
    }


def _iso_clock(clock):
    return {
        'due_at': clock['due_at'].isoformat() if clock['due_at'] else None,
        'done_at': clock['done_at'].isoformat() if clock['done_at'] else None,
        'breached': clock['breached'],
        'minutes_remaining': clock['minutes_remaining'],
    }
