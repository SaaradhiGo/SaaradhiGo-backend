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

NOT IN v1: the clock does not pause while the ticket is WAITING_USER.
That is an open product question (see the module report), so a ticket
waiting on a rider can breach.
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
    ticket.resolution_due_at = base + timedelta(minutes=resolution)
    if save and ticket.pk:
        type(ticket).objects.filter(pk=ticket.pk).update(
            first_response_due_at=ticket.first_response_due_at,
            resolution_due_at=ticket.resolution_due_at)
    return ticket


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

    `state`: 'closed' once the ticket is closed, else 'breached' when any
    open clock is past due, 'due_soon' when the next deadline is within
    `DUE_SOON_MINUTES`, else 'ok'. `next` names the deadline that matters.
    """
    now = now or timezone.now()
    closed = ticket.status == 'CLOSED'
    first = _clock(ticket.first_response_due_at, ticket.first_responded_at, now)
    resolution = _clock(ticket.resolution_due_at, ticket.resolved_at if closed else None, now)
    open_clocks = [(name, clock) for name, clock in
                   (('first_response', first), ('resolution', resolution))
                   if clock['due_at'] is not None and clock['done_at'] is None]
    if closed:
        state, nxt, remaining = 'closed', None, None
    else:
        breached = [c for _, c in open_clocks if c['breached']]
        if breached:
            state = 'breached'
        else:
            state = 'ok'
        if open_clocks:
            nxt, clock = min(open_clocks, key=lambda item: item[1]['due_at'])
            remaining = clock['minutes_remaining']
            if state == 'ok' and remaining is not None and remaining <= DUE_SOON_MINUTES:
                state = 'due_soon'
        else:
            nxt, remaining = None, None
    return {
        'state': state,
        'next': nxt,
        'minutes_remaining': remaining,
        'breached': state == 'breached',
        'first_response': _iso_clock(first),
        'resolution': _iso_clock(resolution),
    }


def _iso_clock(clock):
    return {
        'due_at': clock['due_at'].isoformat() if clock['due_at'] else None,
        'done_at': clock['done_at'].isoformat() if clock['done_at'] else None,
        'breached': clock['breached'],
        'minutes_remaining': clock['minutes_remaining'],
    }
