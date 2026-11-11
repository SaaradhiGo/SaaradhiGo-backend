"""What would this zone change do? The impact preview and the point tester.

ONE RESOLUTION FUNCTION
-----------------------
Both the preview and the tester resolve a point with `services.ZoneIndex`, the same
code quoting, SOS anchoring and the service-area check use. A draft is swapped in
through `DraftZone`, so "the zone this point would resolve to under the draft" and
"the zone it resolves to now" can only differ because of the draft.

THE PREVIEW (last 30 days)
--------------------------
For a draft change (a polygon, a priority, a deactivation, a new zone):

* pickups that would move into or out of the zone, counted by zone (old -> new);
* drivers whose recent pickups would be anchored to a DIFFERENT CITY NODE: a count
  only. A driver's `home_city` is set by onboarding and is not changed by drawing a
  polygon; this counts the drivers whose centre of gravity (the majority of their
  last-30-day pickups) would cross a city boundary, which is who an operator should
  talk to before approving;
* the active fare cards that would newly apply to the pickups that move, and the
  vehicle classes that would be left with NO card.

It scans only trips whose pickup falls in the bounding box of the old and the new
shape, capped at `MAX_TRIPS`; when the cap bites the result says `truncated`.
"""

import logging
from collections import Counter, defaultdict
from datetime import timedelta

from django.utils import timezone

from . import zone_geometry as geo
from .models import ServiceZone
from .services import DraftZone, ZoneIndex, _polygon_for_zone, get_active_rate_card

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
MAX_TRIPS = 50_000


# ---------------------------------------------------------------------------
# Building the draft overlay
# ---------------------------------------------------------------------------

def draft_overlay(zone, values, geom):
    """`DraftZone` for `values` (a validated zone dict) over `zone` (None = new)."""
    from .zone_workflow import _parent_zone

    candidate = ServiceZone(
        code=values['code'], name=values['name'], country=values['country'],
        zone_type=values['zone_type'], priority=values['priority'],
        currency=values['currency'], timezone_name=values['timezone_name'],
        distance_unit=values['distance_unit'], tax_mode=values['tax_mode'],
        is_active=values['is_active'])
    candidate.parent = _parent_zone(values)
    if zone is not None:
        candidate.pk = zone.pk
        candidate.id = zone.id
        candidate.geo_node = zone.geo_node
    return DraftZone(candidate, geom, zone_id=zone.id if zone is not None else None,
                     active=bool(values['is_active']))


def _card_zone(zone):
    """The zone a card lookup should start from. An unsaved draft has no cards of its
    own, so it starts at its parent (what `get_active_rate_card` would do next)."""
    return zone if zone is not None and zone.pk is not None else (
        zone.parent if zone is not None else None)


def cards_for_point(index, lat, lon, vehicle_types, at=None):
    """`(zone, {vehicle_type_id: card or None})` for a point, through `index`."""
    zone = index.resolve(lat, lon)
    start = _card_zone(zone)
    cards = {}
    for vt in vehicle_types:
        cards[vt.id] = get_active_rate_card(start, vt, at=at) if start is not None else None
    return zone, cards


def _card_dict(card):
    if card is None:
        return None
    return {'id': card.id, 'version': card.version, 'zone': card.zone.code,
            'vehicle_type': card.vehicle_type_id,
            'vehicle_type_name': card.vehicle_type.type,
            'base_fare': str(card.base_fare), 'per_km_fare': str(card.per_km_fare),
            'per_min_fare': str(card.per_min_fare), 'min_fare': str(card.min_fare),
            'effective_from': card.effective_from.isoformat()}


def _zone_dict(zone):
    if zone is None:
        return None
    return {'id': zone.pk, 'code': zone.code, 'name': zone.name, 'zone_type': zone.zone_type,
            'priority': zone.priority, 'currency': zone.currency,
            'distance_unit': zone.distance_unit, 'tax_mode': zone.tax_mode,
            'parent': zone.parent.code if zone.parent_id else None}


# ---------------------------------------------------------------------------
# The point tester
# ---------------------------------------------------------------------------

def point_test(lat, lon, *, overlay=None, at=None):
    """Which zone and which rate cards does this point resolve to, now and under a draft."""
    from servers.driver.models import VehicleType

    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError('lat and lng must be numbers')
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        raise ValueError('lat/lng out of range')
    vts = list(VehicleType.objects.order_by('type'))
    live_index = ZoneIndex.load()
    zone, cards = cards_for_point(live_index, lat_f, lon_f, vts, at)
    out = {
        'point': {'lat': lat_f, 'lng': lon_f},
        'in_service_area': zone is not None,
        'zone': _zone_dict(zone),
        'rate_cards': [{'vehicle_type': vt.id, 'vehicle_type_name': vt.type,
                        'card': _card_dict(cards[vt.id])} for vt in vts],
        'matching_zones': [z.code for z in live_index.containing(lat_f, lon_f)],
    }
    if overlay is not None:
        draft_index = ZoneIndex.load(overlay)
        dzone, dcards = cards_for_point(draft_index, lat_f, lon_f, vts, at)
        out['draft'] = {
            'in_service_area': dzone is not None,
            'zone': _zone_dict(dzone),
            'rate_cards': [{'vehicle_type': vt.id, 'vehicle_type_name': vt.type,
                            'card': _card_dict(dcards[vt.id])} for vt in vts],
            'matching_zones': [z.code for z in draft_index.containing(lat_f, lon_f)],
            'changes_resolution': (dzone.code if dzone else None) != (zone.code if zone else None),
        }
    return out


# ---------------------------------------------------------------------------
# The impact preview
# ---------------------------------------------------------------------------

def _city_code(zone):
    """The city geo node a zone belongs to (walking up from a sub-zone node)."""
    node = zone.geo_node if zone is not None else None
    while node is not None and node.kind != 'city':
        node = node.parent
    return node.code if node is not None else None


def impact(zone, values, geom, *, now=None):
    """The 30-day impact of replacing `zone` (None = a new zone) with `values`."""
    from servers.driver.models import VehicleType
    from servers.ride.models import Trip

    now = now or timezone.now()
    overlay = draft_overlay(zone, values, geom)
    old_index = ZoneIndex.load()
    new_index = ZoneIndex.load(overlay)

    old_geom = _polygon_for_zone(zone) if zone is not None and zone.is_active else None
    new_geom = geom if values['is_active'] else None
    box = geo.bounds_of(old_geom, new_geom)
    vts = {vt.id: vt for vt in VehicleType.objects.all()}

    transitions = Counter()
    scanned = unchanged = 0
    truncated = False
    card_pairs = Counter()          # (new_card_id, vt_id) -> trips
    card_rows = {}
    no_card = Counter()             # (zone_code, vt_id) -> trips
    card_cache = {}
    driver_old, driver_new = defaultdict(Counter), defaultdict(Counter)

    def card(z, vt_id):
        start = _card_zone(z)
        if start is None or vt_id is None or vt_id not in vts:
            return None
        key = (id(start), vt_id)
        if key not in card_cache:
            card_cache[key] = get_active_rate_card(start, vts[vt_id], at=now)
        return card_cache[key]

    if box is not None:
        min_lon, min_lat, max_lon, max_lat = box
        qs = (Trip.objects.filter(
            requested_at__gte=now - timedelta(days=WINDOW_DAYS),
            pickup_lat__gte=min_lat, pickup_lat__lte=max_lat,
            pickup_long__gte=min_lon, pickup_long__lte=max_lon)
            .values_list('pickup_lat', 'pickup_long', 'requested_vehicle_type_id',
                         'driver_id')
            .order_by('-requested_at'))
        for lat, lon, vt_id, driver_id in qs[:MAX_TRIPS + 1].iterator(chunk_size=2000):
            if scanned >= MAX_TRIPS:
                truncated = True
                break
            scanned += 1
            old = old_index.resolve(lat, lon)
            new = new_index.resolve(lat, lon)
            if driver_id is not None:
                driver_old[driver_id][_city_code(old)] += 1
                driver_new[driver_id][_city_code(new)] += 1
            if (old.code if old else None) == (new.code if new else None):
                unchanged += 1
                continue
            transitions[(old.code if old else None, new.code if new else None)] += 1
            old_card, new_card = card(old, vt_id), card(new, vt_id)
            if new_card is None:
                if old_card is not None:
                    no_card[((new.code if new else None), vt_id)] += 1
            elif old_card is None or new_card.id != old_card.id:
                card_pairs[(new_card.id, vt_id)] += 1
                card_rows[new_card.id] = new_card

    moved = sum(transitions.values())
    into = sum(n for (a, b), n in transitions.items() if b == values['code'] and a != b)
    out_of = sum(n for (a, b), n in transitions.items() if a == values['code'] and a != b)

    drivers_changed = 0
    for driver_id, new_counts in driver_new.items():
        old_top = driver_old[driver_id].most_common(1)[0][0]
        new_top = new_counts.most_common(1)[0][0]
        if old_top != new_top:
            drivers_changed += 1

    cards = [{**_card_dict(card_rows[cid]), 'trips': n}
             for (cid, vt_id), n in sorted(card_pairs.items(), key=lambda kv: -kv[1])]
    missing = [{'zone': code, 'vehicle_type': vt_id,
                'vehicle_type_name': vts[vt_id].type if vt_id in vts else None, 'trips': n}
               for (code, vt_id), n in sorted(no_card.items(), key=lambda kv: -kv[1])]

    area = geo.area_km2(geom)
    old_area = geo.area_km2(old_geom) if old_geom is not None else None
    result = {
        'window_days': WINDOW_DAYS,
        'zone': values['code'], 'is_new': zone is None,
        'area_km2': round(area, 2), 'old_area_km2': round(old_area, 2) if old_area else None,
        'vertex_count': _vertices(values['polygon_geojson']),
        'trips_scanned': scanned, 'truncated': truncated,
        'trips_unchanged': unchanged, 'trips_moved': moved,
        'moved_into_zone': into, 'moved_out_of_zone': out_of,
        'transitions': [{'from': a, 'to': b, 'trips': n}
                        for (a, b), n in sorted(transitions.items(), key=lambda kv: -kv[1])],
        'drivers_whose_city_would_change': drivers_changed,
        'cards_newly_applying': cards,
        'pickups_without_a_card': missing,
        'trips_in_progress_now': (_in_progress(zone) if zone is not None else 0),
    }
    result['summary'] = summarise(result, values, zone)
    return result


def _in_progress(zone):
    from .zone_workflow import in_progress_trips

    return in_progress_trips(zone).count()


def _vertices(polygon):
    from .zone_models import _vertex_count

    return _vertex_count(polygon)


def summarise(r, values, zone):
    """The 'What this change does' lines, plain English, most important first."""
    code = values['code']
    lines = []
    if r['is_new']:
        lines.append(f"Creates zone {code} covering {r['area_km2']:,} km2 "
                     f"({r['vertex_count']} vertices).")
    elif not values['is_active'] and zone is not None and zone.is_active:
        lines.append(f'Takes {code} out of service: pickups inside it resolve to the next '
                     f'zone that contains them, or to no zone.')
    else:
        old = r['old_area_km2']
        if old:
            delta = (r['area_km2'] - old) / old * 100
            lines.append(f"Reshapes {code}: {old:,} km2 to {r['area_km2']:,} km2 "
                         f"({delta:+.1f}%).")
        else:
            lines.append(f"Puts {code} into service covering {r['area_km2']:,} km2.")
    if r['trips_moved'] == 0:
        lines.append(f"No pickup from the last {r['window_days']} days would change zone.")
    else:
        lines.append(f"{r['trips_moved']:,} of the last {r['window_days']} days' pickups "
                     f"would change zone ({r['moved_into_zone']:,} into {code}, "
                     f"{r['moved_out_of_zone']:,} out of it).")
        for t in r['transitions'][:5]:
            lines.append(f"  {t['trips']:,} pickup(s): {t['from'] or 'no zone'} -> "
                         f"{t['to'] or 'no zone'}.")
    if r['drivers_whose_city_would_change']:
        lines.append(f"{r['drivers_whose_city_would_change']:,} driver(s) would have most of "
                     f"their recent pickups anchored to a different city.")
    if r['cards_newly_applying']:
        lines.append(f"{len(r['cards_newly_applying'])} fare card(s) would newly apply to "
                     f"moved pickups.")
    if r['pickups_without_a_card']:
        total = sum(m['trips'] for m in r['pickups_without_a_card'])
        lines.append(f'WARNING: {total:,} moved pickup(s) would land where their vehicle class '
                     f'has no fare card, so they could not be quoted.')
    if r['trips_in_progress_now']:
        lines.append(f"{r['trips_in_progress_now']:,} trip(s) in this zone are in progress now; "
                     f"a deactivation is refused until they end.")
    if r['truncated']:
        lines.append(f'Counts cover the most recent {r["trips_scanned"]:,} pickups in the area '
                     f'(the scan is capped).')
    return lines
