"""Which riders an operator may see: ONE rule, used by every rider surface.

THE RULE (owner ruling, AUDIT 27.32)
------------------------------------
A rider belongs to EVERY city where they took a trip in the last
`RIDER_SCOPE_TRIP_MONTHS` months (default 12), plus their HOME city
(`Rider.home_city`) when one is known. Those are the rider's *anchors*.

A rider with no anchor at all is anchored at their COUNTRY, read from the
phone's calling code (+91 India, +1 United States; any other prefix has no
country and is visible to unrestricted callers only). "A new rider who
can't book is a core support case": a country-anchored rider is visible to
every holder of the capability scoped ANYWHERE within that country (a city
assignment in India sees a +91 newcomer), and to anyone scoped at the
country or above.

A caller sees a rider when ANY anchor is inside the caller's scope. The
caller's scope is their assignments, narrowed by `?scope=` when one is sent
(narrowing is applied on top, never instead).

WHERE IT IS USED
----------------
`riders_visible_to(user)` is the only queryset that decides this. The rider
list, the rider page and its writes, rider search (`/search/admin/riders/`)
and the reveal endpoint for rider / user targets all call it, so a rider
that search returns always opens (never a 404 behind a result).

WHAT A CAPABILITY MEANS FOR A MULTI-ANCHOR RIDER
------------------------------------------------
`rider_may(user, permission, rider)` is true when the caller holds the
permission at ANY of the rider's anchors (for a country anchor: anywhere
within that country).

COST
----
The trip anchor is an `EXISTS` over `ride_trip` using the existing
`(user_id, -requested_at)` index, bounded by the window; the city test is a
prefix match on the indexed `GeoNode.path`. The phone test is a prefix
match on a column already used for rider search.
"""

from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Length
from django.utils import timezone

DEFAULT_TRIP_MONTHS = 12


def trip_window_start(now=None):
    months = int(getattr(settings, 'RIDER_SCOPE_TRIP_MONTHS', DEFAULT_TRIP_MONTHS))
    return (now or timezone.now()) - timedelta(days=round(months * 30.4375))


def unrestricted(user):
    """True when the caller's scope covers everything, rows with no place
    included: a Global assignment or superuser, or (until strict mode) an
    unmigrated account, exactly as `scoping.scoped` treats them."""
    from servers.admin_access.access import scope_paths
    from servers.admin_access.bridge import is_migrated, strict_mode

    if not strict_mode() and not is_migrated(user):
        return True
    return scope_paths(user) is None


def _related(a, b):
    """One path contains the other."""
    return a.startswith(b) or b.startswith(a)


def _country_phone_q(paths):
    """Phone numbers of the supported countries that relate to `paths`."""
    from base.countries import COUNTRIES
    from servers.admin_access.models import COUNTRY, GeoNode

    nodes = GeoNode.objects.filter(kind=COUNTRY, code__in=list(COUNTRIES))
    q = Q(pk__in=[])
    for node in nodes:
        if not any(_related(node.path, p) for p in paths):
            continue
        country = COUNTRIES[node.code]
        lengths = [1 + n for n in country.e164_length]
        q |= Q(user_id__phone_number__startswith='+' + country.calling_code,
               phone_len__in=lengths)
    return q


def riders_visible_to(user, node=None, queryset=None, paths=None):
    """Riders in the caller's scope (see the module docstring).

    `node` is the already-validated `?scope=` node (`requested_node`), or
    None. `queryset` defaults to every rider.

    `paths` narrows the caller's scope to those path prefixes (the places a
    particular capability is held, `payments.scope.paths_for`); None means
    every assignment counts, as everywhere else. An empty list sees nobody.
    """
    from servers.admin_access.access import scope_paths
    from servers.ride.models import Trip

    from .models import Rider

    riders = queryset if queryset is not None else Rider.objects.select_related('user_id')
    if node is not None:
        paths = [node.path]
    elif paths is not None:
        pass  # already narrowed to where one capability is held
    elif unrestricted(user):
        return riders
    else:
        paths = scope_paths(user)
    if not paths:
        return riders.none()

    since = trip_window_start()
    recent = Trip.objects.filter(user_id=OuterRef('user_id'), requested_at__gte=since)
    anchored_trip = Q()
    anchored_home = Q()
    for p in paths:
        anchored_trip |= Q(zone__geo_node__path__startswith=p)
        anchored_home |= Q(home_city__path__startswith=p)
    country_q = _country_phone_q(paths)
    return (
        riders.annotate(phone_len=Length('user_id__phone_number'))
        .filter(
            Exists(recent.filter(anchored_trip))
            | anchored_home
            | (~Exists(recent.filter(zone__geo_node__isnull=False))
               & Q(home_city__isnull=True) & country_q)
        )
    )


def rider_in_scope(user, rider, node=None):
    return riders_visible_to(user, node).filter(pk=rider.pk).exists()


def anchor_nodes(rider):
    """The rider's anchor GeoNodes: recent trip cities, the home city, or
    the country from the phone. Empty for an unplaceable rider."""
    from base.countries import country_of_phone
    from servers.admin_access.models import COUNTRY, GeoNode
    from servers.ride.models import Trip

    ids = set(Trip.objects.filter(user_id=rider.user_id_id,
                                  requested_at__gte=trip_window_start(),
                                  zone__geo_node__isnull=False)
              .order_by().values_list('zone__geo_node_id', flat=True).distinct())
    if rider.home_city_id:
        ids.add(rider.home_city_id)
    if ids:
        return list(GeoNode.objects.filter(id__in=ids))
    country = country_of_phone(getattr(rider.user_id, 'phone_number', None))
    if country is None:
        return []
    node = GeoNode.objects.filter(kind=COUNTRY, code=country.code).first()
    return [node] if node else []


def rider_may(user, permission, rider, anchors=None):
    """Does the caller hold `permission` at any of the rider's anchors?"""
    from servers.admin_access.access import active_assignments
    from servers.admin_access.bridge import granted, is_migrated, strict_mode
    from servers.admin_access.models import COUNTRY

    nodes = anchor_nodes(rider) if anchors is None else anchors
    if not nodes:
        # Unplaceable: only the unrestricted can see it; decide anywhere.
        return granted(user, permission)[0] if unrestricted(user) else False
    for node in nodes:
        if node.kind != COUNTRY:
            if granted(user, permission, node)[0]:
                return True
            continue
        # A country anchor is "anywhere within the country".
        if getattr(user, 'is_superuser', False):
            return granted(user, permission, node)[0]
        if not strict_mode() and not is_migrated(user):
            if granted(user, permission)[0]:
                return True
            continue
        for a in active_assignments(user):
            if permission in (a.role.permissions or []) and _related(node.path, a.scope.path):
                return True
    return False
