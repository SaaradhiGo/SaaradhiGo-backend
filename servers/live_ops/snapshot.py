"""GET /live-ops/admin/snapshot/: the whole picture once, for first paint and as
the polling fallback when the socket is down.

EVERYTHING IS FILTERED TWICE, AND THE SECOND TIME IS IN SQL OR BEFORE THE DOT
IS BUILT
-----------------------------------------------------------------------------
  1. which CITIES the caller sees at all (`LiveScope.sees`);
  2. which of those they may see INDIVIDUALS in (`LiveScope.reads`).

A driver in a city the caller cannot read is dropped before a dot exists, and
before the per-city counts are computed, so no count of theirs leaks either.
Counts are computed per visible city over ALL positions in that city, not over
the capped dot list, so the status bar is true even when the map says "zoom in".
"""

import logging
from collections import defaultdict
from django.db.models import Count, Min, Q
from django.utils import timezone

from servers.admin_access.models import ACTIVE, CITY

from .config import (
    ACTIVE_TRIP_STATES, CRITICAL_REQUEST_TO_DRIVER_RATIO, HIGH_DEMAND_REQUEST_TO_DRIVER_RATIO,
    POLL_FALLBACK_SECONDS, REQUESTED, SNAPSHOT_DOT_CAP, SNAPSHOT_TRIP_CAP, STATUS_CRITICAL,
    STATUS_HIGH_DEMAND, STATUS_NORMAL, STATUS_OFFLINE,
)
from .dots import ON_TRIP, build_alert, build_dot, build_trip
from .geo import Geography
from .positions import place, read_live_positions

logger = logging.getLogger(__name__)


class CityNotVisible(Exception):
    """The requested city does not exist or is outside the caller's scope.
    The same answer for both, so a code cannot be probed."""


def network_status(online, on_trip, unassigned):
    """OFFLINE, CRITICAL, HIGH DEMAND or NORMAL for one city.

    Ported from the Django `predictive_heatmaps` screen, same ratios:
    requests beyond TWICE the idle drivers is critical, beyond ONE times is
    high demand. Two differences, both deliberate: the supply is the drivers
    live on the map (not the `status` column), and a city is OFFLINE only
    when no driver at all is live, so a city whose drivers are all mid-trip is
    not reported dead.
    """
    if online + on_trip == 0:
        return STATUS_OFFLINE
    if unassigned > online * CRITICAL_REQUEST_TO_DRIVER_RATIO:
        return STATUS_CRITICAL
    if unassigned > online * HIGH_DEMAND_REQUEST_TO_DRIVER_RATIO:
        return STATUS_HIGH_DEMAND
    return STATUS_NORMAL


def visible_cities(scope, geography, zone_node_ids):
    """City nodes the caller sees: in scope, and live (active, or with a zone)."""
    return [
        c for c in geography.cities()
        if scope.sees(c) and (c.status == ACTIVE or c.id in zone_node_ids)
    ]


def zone_outlines(scope, geography, cities):
    """Zone outlines for the picked cities: [lat, lng] rings, no more."""
    from servers.pricing.models import ServiceZone
    from servers.pricing.services import _polygon_for_zone

    city_ids = {c.id for c in cities}
    out = []
    zones = ServiceZone.objects.filter(is_active=True, geo_node_id__in=city_ids).order_by('priority', 'code')
    for zone in zones:
        poly = _polygon_for_zone(zone)
        if poly is None:
            continue
        shapes = list(poly.geoms) if hasattr(poly, 'geoms') else [poly]
        rings = [[[round(lat, 5), round(lng, 5)] for lng, lat in shape.exterior.coords]
                 for shape in shapes]
        node = geography.node(zone.geo_node_id)
        out.append({
            'code': zone.code, 'name': zone.name, 'type': zone.zone_type,
            'city': node.code if node else None, 'rings': rings,
        })
    return out


def _zone_node_ids():
    from servers.pricing.models import ServiceZone

    return set(ServiceZone.objects.filter(
        is_active=True, geo_node__isnull=False).values_list('geo_node_id', flat=True))


def _trip_stats(now):
    """Per-city unassigned and stale trip counts: two grouped queries."""
    from servers.ride.models import Trip

    unassigned = {
        row['zone__geo_node_id']: (row['n'], row['oldest'])
        for row in Trip.objects.filter(
            status_id__status_code=REQUESTED, driver_id__isnull=True,
        ).values('zone__geo_node_id').annotate(n=Count('id'), oldest=Min('requested_at'))
    }
    stale = {
        row['zone__geo_node_id']: row['n']
        for row in Trip.objects.filter(
            status_id__status_code__in=[s for s in ACTIVE_TRIP_STATES if s != REQUESTED],
            stale_flagged_at__isnull=False,
        ).values('zone__geo_node_id').annotate(n=Count('id'))
    }
    return unassigned, stale


def city_status_rows(cities, positions, now=None):
    """One status row per visible city. Counts only; never a position."""
    now = now or timezone.now()
    unassigned, stale = _trip_stats(now)
    online, on_trip = defaultdict(int), defaultdict(int)
    for p in positions or []:
        if p.city is None:
            continue
        (on_trip if p.status == ON_TRIP else online)[p.city.id] += 1
    rows = []
    for c in cities:
        n_unassigned, oldest = unassigned.get(c.id, (0, None))
        rows.append({
            'code': c.code,
            'name': c.name,
            'network_status': network_status(online[c.id], on_trip[c.id], n_unassigned),
            'online_drivers': online[c.id],
            'on_trip_drivers': on_trip[c.id],
            'unassigned_trips': n_unassigned,
            'oldest_unassigned_age_s': (
                max(0, int((now - oldest).total_seconds())) if oldest else None),
            'stale_trips': stale.get(c.id, 0),
        })
    return rows


def _trips(scope, cities, selected, exact, now):
    from servers.ride.models import Trip

    readable = [c for c in cities if scope.reads(c)]
    if selected is not None:
        readable = [c for c in readable if c.id == selected.id]
    ids = [c.id for c in readable]
    qs = Trip.objects.filter(
        status_id__status_code__in=ACTIVE_TRIP_STATES, zone__geo_node_id__in=ids,
    ).order_by('-requested_at')
    rows = list(qs.values_list(
        'id', 'status_id__status_code', 'pickup_lat', 'pickup_long',
        'destination_lat', 'destination_long', 'requested_vehicle_type__type',
        'requested_at', 'stale_flagged_at', 'zone__geo_node_id',
    )[:SNAPSHOT_TRIP_CAP + 1])
    truncated = len(rows) > SNAPSHOT_TRIP_CAP
    out = []
    for tid, state, plat, plng, dlat, dlng, vclass, requested_at, stale_at, node_id in rows[:SNAPSHOT_TRIP_CAP]:
        city = next((c.code for c in readable if c.id == node_id), None)
        out.append(build_trip(
            trip_id=tid, state=state, pickup_lat=plat, pickup_lng=plng, drop_lat=dlat,
            drop_lng=dlng, vehicle_class=vclass, age_s=(now - requested_at).total_seconds(),
            city=city, stale=stale_at is not None, exact=exact))
    return out, truncated


def _alerts(user, scope, cities, selected, exact):
    """Open alerts the caller already sees on the Command Centre, narrowed to
    where they may read the map. SOS only for `sees_live_sos` and finance
    kinds never: both rules live in `_visible_alerts`, which is reused rather
    than restated."""
    from servers.command_centre.models import FINANCE_ALERT_KINDS
    from servers.command_centre.views import SOS_ALERT_KINDS, _visible_alerts, sees_live_sos
    from servers.sos.models import SOSEvent

    readable = [c for c in cities if scope.reads(c)]
    if selected is not None:
        readable = [c for c in readable if c.id == selected.id]
    qs = (_visible_alerts(user).filter(acknowledged_at__isnull=True)
          .exclude(kind__in=FINANCE_ALERT_KINDS))
    if not sees_live_sos(user):
        qs = qs.exclude(kind__in=SOS_ALERT_KINDS)
    where = Q(city_id__in=[c.id for c in readable])
    if selected is None:
        where |= Q(city__isnull=True)
    alerts = list(qs.filter(where).select_related('city')[:200])

    sos_ids = [a.payload.get('sos_id') for a in alerts
               if a.kind in SOS_ALERT_KINDS and isinstance(a.payload, dict) and a.payload.get('sos_id')]
    where_is = {
        e['id']: (e['latitude'], e['longitude'])
        for e in SOSEvent.objects.filter(id__in=sos_ids).values('id', 'latitude', 'longitude')
    } if sos_ids else {}
    out = []
    for a in alerts:
        is_sos = a.kind in SOS_ALERT_KINDS
        lat = lng = None
        if is_sos and isinstance(a.payload, dict):
            lat, lng = where_is.get(a.payload.get('sos_id'), (None, None))
        out.append(build_alert(
            alert_id=a.id, kind=a.kind, severity=a.severity, title=a.title,
            city=a.city.code if a.city_id else None, created_at=a.created_at, is_sos=is_sos,
            lat=lat, lng=lng, exact=exact))
    return out


def dots_for(scope, placed, shown, selected, exact):
    """`(dots, total_candidates)`: the placed positions the caller may read,
    in the cities being shown, capped at `SNAPSHOT_DOT_CAP`."""
    readable_ids = {c.id for c in shown if scope.reads(c)}
    candidates = sorted(
        (p for p in placed if p.city is not None and p.city.id in readable_ids),
        key=lambda p: p.driver_id)
    # An unplaceable driver (no zone, no home city) is the unrestricted
    # caller's alone, and only on the all-cities view.
    if scope.read is None and selected is None:
        candidates += sorted((p for p in placed if p.city is None), key=lambda p: p.driver_id)
    dots = [
        build_dot(driver_id=p.driver_id, status=p.status, lat=p.lat, lng=p.lng,
                  vehicle_class=p.vehicle_class, heading=p.heading, updated_at=p.ts,
                  trip_id=p.trip_id, city=p.city.code if p.city else None, exact=exact)
        for p in candidates[:SNAPSHOT_DOT_CAP]
    ]
    return dots, len(candidates)


def build_snapshot(user, scope, *, city_code='', exact=True, geography=None, positions=None):
    """The snapshot for `user`. Raises `CityNotVisible` for a bad `city_code`."""
    now = timezone.now()
    geography = geography or Geography.get()
    zone_nodes = _zone_node_ids()
    cities = visible_cities(scope, geography, zone_nodes)

    selected = None
    if city_code:
        selected = next((c for c in cities if c.code == city_code), None)
        if selected is None:
            raise CityNotVisible(city_code)

    if positions is None:
        positions = read_live_positions()
    presence_ok = positions is not None
    placed = place(positions, geography) if presence_ok else []

    # Counts are about visible cities only: a driver in a city the caller
    # cannot see contributes to nothing.
    shown = [selected] if selected is not None else cities
    snapshot = {
        'generated_at': now.isoformat().replace('+00:00', 'Z'),
        'mode': scope.mode,
        'city': selected.code if selected else None,
        'poll_seconds': POLL_FALLBACK_SECONDS,
        'presence_available': presence_ok,
        'cities': city_status_rows(cities, placed, now),
        'zones': zone_outlines(scope, geography, shown),
    }
    if not scope.can_read:
        return snapshot

    dots, candidates = dots_for(scope, placed, shown, selected, exact)
    trips, trips_truncated = _trips(scope, cities, selected, exact, now)
    dots_truncated = candidates > SNAPSHOT_DOT_CAP
    snapshot.update({
        'drivers': dots,
        'trips': trips,
        'alerts': _alerts(user, scope, cities, selected, exact),
        'truncated': {
            'drivers': dots_truncated, 'trips': trips_truncated,
            'limit': SNAPSHOT_DOT_CAP,
            'message': (
                f'Showing {SNAPSHOT_DOT_CAP:,} of {candidates:,} drivers. Zoom in: pick a city.'
                if dots_truncated and selected is None else
                f'Showing {SNAPSHOT_DOT_CAP:,} of {candidates:,} drivers.'
                if dots_truncated else
                'Showing the newest trips only.' if trips_truncated else None),
        },
    })
    return snapshot
