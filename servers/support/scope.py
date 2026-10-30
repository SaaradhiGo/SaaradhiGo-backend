"""Which support tickets an operator may see: ONE rule, used by every surface.

THE RULE (owner ruling: "a new rider who can't book is a core support case")
---------------------------------------------------------------------------
A ticket is placed by the FIRST of these that applies:

1. it has a trip whose zone has a geo node: that trip's city;
2. else the requester is a driver: the driver's home city;
3. else the requester is a rider: wherever that rider is visible
   (`servers.rider.scope.riders_visible_to`: trip cities in the window, the
   home city, else the phone's country);
4. else (nobody to place it by): unrestricted callers only.

A ticket with a trip is NOT also visible through the rider's other cities:
rule 1 wins, so an Austin ticket from a rider who also rides in Hyderabad
stays an Austin ticket.

A driver with no home city is placed by rule 2 and therefore by nobody but
the unrestricted, as before.

WHERE IT IS USED
----------------
`tickets_visible_to(user, node=None, queryset=None)` is the only queryset
that decides this. The list, the detail, every write, the assignee list,
macro filtering by ticket, the reveal endpoint and any search call it, so a
ticket that one of them returns always opens (never a 404 behind a row).

CAPABILITY AT THE TICKET
------------------------
`ticket_may(user, permission, ticket)`: for a rider-anchored ticket the
permission must be held at ANY of the rider's anchors (`rider_may`); for a
trip or driver ticket, at that city. Out of scope answers 404 and in scope
without the capability 403, like every object in the console.
"""

from django.db.models import Exists, OuterRef, Q


def _paths(user, node):
    """None for unrestricted, else the list of scope path prefixes."""
    from servers.admin_access.access import scope_paths
    from servers.rider.scope import unrestricted

    if node is not None:
        return [node.path]
    if unrestricted(user):
        return None
    return scope_paths(user) or []


def tickets_visible_to(user, node=None, queryset=None):
    """Tickets in the caller's scope (see the module docstring).

    `node` is the already-validated `?scope=` node (`requested_node`), or
    None. `queryset` defaults to every ticket.
    """
    from servers.driver.models import Driver
    from servers.rider.models import Rider
    from servers.rider.scope import riders_visible_to

    from .models import SupportTicket

    tickets = queryset if queryset is not None else SupportTicket.objects.all()
    paths = _paths(user, node)
    if paths is None:
        return tickets
    if not paths:
        return tickets.none()

    has_trip_city = Q(trip_id__zone__geo_node__isnull=False)
    by_trip = Q()
    by_driver_home = Q()
    for p in paths:
        by_trip |= Q(trip_id__zone__geo_node__path__startswith=p)
        by_driver_home |= Q(user_id__driver__home_city__path__startswith=p)

    is_driver = Exists(Driver.objects.filter(user_id=OuterRef('user_id')))
    rider_rows = riders_visible_to(
        user, node, Rider.objects.filter(user_id=OuterRef('user_id')))
    return tickets.filter(
        (has_trip_city & by_trip)
        | (~has_trip_city & is_driver & by_driver_home)
        | (~has_trip_city & ~is_driver & Exists(rider_rows))
    )


def profile(user, name):
    """`user.driver` / `user.rider`, or None. Uses the select_related cache."""
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return getattr(user, name)
    except ObjectDoesNotExist:
        return None


def _trip_node(ticket):
    trip = ticket.trip_id
    if trip is not None and trip.zone is not None and trip.zone.geo_node_id:
        return trip.zone.geo_node
    return None


def ticket_rider(ticket):
    """The Rider a ticket is anchored by (rule 3), else None."""
    if _trip_node(ticket) is not None or profile(ticket.user_id, 'driver') is not None:
        return None
    return profile(ticket.user_id, 'rider')


def ticket_node(ticket):
    """The single GeoNode that places a trip or driver ticket, else None."""
    node = _trip_node(ticket)
    if node is not None:
        return node
    driver = profile(ticket.user_id, 'driver')
    return driver.home_city if driver is not None else None


def ticket_may(user, permission, ticket):
    """Does the caller hold `permission` where this ticket sits?"""
    from servers.admin_access.bridge import granted
    from servers.rider.scope import rider_may, unrestricted

    rider = ticket_rider(ticket)
    if rider is not None:
        return rider_may(user, permission, rider)
    node = ticket_node(ticket)
    if node is None:
        # Nothing places it: only the unrestricted can see it, so decide
        # on "anywhere" for them and refuse everyone else.
        return granted(user, permission)[0] if unrestricted(user) else False
    return granted(user, permission, node)[0]


def ticket_place(ticket):
    """Display only: {code, name, country} of where the ticket sits. A trip
    or driver ticket shows its city; a rider's, their home city, else the
    country their phone belongs to; None when nothing places it."""
    from base.countries import COUNTRIES, country_of_phone

    from .sla import country_of_node

    node = ticket_node(ticket)
    if node is None:
        rider = ticket_rider(ticket)
        if rider is not None and rider.home_city_id:
            node = rider.home_city
    if node is not None:
        return {'code': node.code, 'name': node.name, 'country': country_of_node(node)}
    if ticket_rider(ticket) is not None:
        country = country_of_phone(getattr(ticket.user_id, 'phone_number', None))
        if country is not None:
            return {'code': country.code, 'name': COUNTRIES[country.code].name,
                    'country': country.code}
    return None
