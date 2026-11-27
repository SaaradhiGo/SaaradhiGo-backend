"""Trip extra charges (owner brief, 26 Nov): what the planned route crosses, priced before booking.

The quote matches the route against the catalogue (`TripChargeItem`) and adds each charge the rider
pays as its own line inside the locked total. Tolls, airport and venue fees and permits are passed
through: no commission or fee is taken on them (settlement removes them from the gross). Waiting is
not: it is the driver's earnings for the time, so the driver fee or commission applies (review 27 Nov). Values are ops-entered; Google's toll price
is only a cross-check (`toll_check`). Off unless the `trip_extras` flag is on for the city.
A catalogue change is a `pricing.change` request (kind `trip_charge_item`): a second person approves.
"""

import math
import re
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from servers.admin_audit.services import record_admin_action
from servers.approvals.actions import PRICING_CHANGE
from servers.approvals.service import InvalidRequest, NotPermitted, require_approval_or_execute

from .trip_charge_models import (ACTIVE, AIRPORT, ARCHIVED, AT_GATE, COLLECTIONS, DROP, KINDS, PERMIT,
                                 PICKUP, PICKUP_OR_DROP, PLATFORM_REMITS, RIDER, TOLL_KINDS, TOLL_PLAZA,
                                 TOLLED_ROAD, VENUE, WAITING, WHO_PAYS, TripChargeItem)

KIND = 'trip_charge_item'
FLAG = 'trip_extras'
LINE_CODE = 'trip_extra'
EARTH_M = 6371008.8
#: Less than this on a tolled road is GPS noise at a junction, not a toll.
MIN_TOLLED_KM = Decimal('0.3')
KIND_WORDS = {TOLL_PLAZA: 'Toll plaza', TOLLED_ROAD: 'Tolled road', AIRPORT: 'Airport', VENUE: 'Venue',
              WAITING: 'Waiting', PERMIT: 'Interstate permit'}
WHO_WORDS = {RIDER: 'Rider pays · passed through', 'driver': 'Driver pays (not on the fare)',
             'platform': 'SaaradhiGo pays (not on the fare)'}
#: Waiting is the driver's earnings for the time, never a pass-through (review 27 Nov).
WAITING_WHO_WORDS = 'Rider pays · goes to the driver'
COLLECTION_WORDS = {AT_GATE: 'Driver pays at the gate (reimbursed)',
                    PLATFORM_REMITS: 'SaaradhiGo pays the authority (permit)'}
#: Step 3 (driver reimbursement) live? Until it is, production never charges trip extras: the
#: flag is held off there and the launch checklist blocks a city that has it on (review 27 Nov).
DRIVER_REIMBURSEMENT_LIVE = False


def who_words(kind, who):
    if who == RIDER and kind == WAITING:
        return WAITING_WHO_WORDS
    return WHO_WORDS.get(who, who)


def collection_of(item):
    """Who pays the authority; items made before the field read as the kind's default."""
    if item.collection:
        return item.collection
    return '' if item.kind == WAITING else PLATFORM_REMITS if item.kind == PERMIT else AT_GATE


def on_behalf_note(line):
    """Whom a rider-paid pass-through line is collected for, in the rider's words (step 2)."""
    kind = line.get('kind')
    if kind == WAITING:
        return ''
    collection = line.get('collection') or (PLATFORM_REMITS if kind == PERMIT else AT_GATE)
    if collection == PLATFORM_REMITS and line.get('remit_to'):
        return f"Collected on behalf of {line['remit_to']}. SaaradhiGo pays it."
    if kind in TOLL_KINDS:
        return 'Collected on behalf of the toll operator. Your driver pays it at the plaza.'
    if kind == VENUE:
        return 'Collected on behalf of the venue. Your driver pays it at the gate.'
    return 'Collected on behalf of the airport. Your driver pays it at the gate.'


def waiting_charge(terms, reached_at, started_at, currency):
    """`(billable minutes, minor)` the rider owes for keeping the driver waiting (step 2).

    Whole minutes from the driver's arrival to the start, less the free minutes locked with the fare.
    Rider-caused, so it is the one thing that may raise the locked total."""
    from base.money import from_minor, round_fare, to_minor

    if not terms or reached_at is None or started_at is None or started_at <= reached_at:
        return 0, 0
    waited = int((started_at - reached_at).total_seconds() // 60)
    billable = max(0, waited - int(terms.get('free_minutes') or 0))
    per = int(terms.get('per_minute_minor') or 0)
    if not billable or not per:
        return billable, 0
    return billable, to_minor(round_fare(from_minor(per * billable, currency), currency), currency)


def production_hold():
    """True while production must keep `trip_extras` off (driver reimbursement is not live)."""
    from django.conf import settings

    return bool(getattr(settings, 'IS_PRODUCTION', False)) and not DRIVER_REIMBURSEMENT_LIVE
APPLIES_WORDS = {PICKUP: 'Pickups inside', DROP: 'Drops inside', PICKUP_OR_DROP: 'Pickups or drops inside'}
UNIT_WORDS = {TOLLED_ROAD: '/km', WAITING: '/min'}
#: Typo guards per unit, in whole currency units (not a policy).
MAX_UNITS = {TOLLED_ROAD: 50, WAITING: 20}
MAX_UNITS_DEFAULT = 2000


# ---------------------------------------------------------------------------
# Geometry (metres on a local flat projection: city-sized, well inside its error)
# ---------------------------------------------------------------------------
def _xy(lat, lng, lat0):
    return (math.radians(lng) * EARTH_M * math.cos(math.radians(lat0)), math.radians(lat) * EARTH_M)


def _seg_dist_m(p, a, b):
    lat0 = p[0]
    px, py = _xy(p[0], p[1], lat0)
    ax, ay = _xy(a[0], a[1], lat0)
    bx, by = _xy(b[0], b[1], lat0)
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _dist_to_path_m(p, path):
    if len(path) == 1:
        return _seg_dist_m(p, path[0], path[0])
    return min(_seg_dist_m(p, path[i], path[i + 1]) for i in range(len(path) - 1))


def _seg_len_m(a, b):
    ax, ay = _xy(a[0], a[1], a[0])
    bx, by = _xy(b[0], b[1], a[0])
    return math.hypot(bx - ax, by - ay)


def _densify(points, step_m=150.0):
    """Route points no more than `step_m` apart, so a straight highway still meets a toll plaza."""
    out = []
    for a, b in zip(points, points[1:], strict=False):
        n = max(1, int(_seg_len_m(a, b) // step_m))
        out.extend((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n))
    return out + points[-1:]


def _in_ring(p, ring):
    lat, lng = p
    inside = False
    for (alat, alng), (blat, blng) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (alat > lat) != (blat > lat) and lng < alng + (lat - alat) * (blng - alng) / (blat - alat):
            inside = not inside
    return inside


def _latlngs(coords):
    """GeoJSON [lng, lat] pairs as (lat, lng)."""
    return [(float(c[1]), float(c[0])) for c in coords or []]


def _ring(item):
    coords = (item.geometry or {}).get('coordinates') or []
    ring = _latlngs(coords[0] if coords and isinstance(coords[0][0], (list, tuple)) else coords)
    return ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring


def tolled_km(item, points):
    path = _latlngs((item.geometry or {}).get('coordinates'))
    corridor = item.corridor_m or 40
    metres = 0.0
    for a, b in zip(points, points[1:], strict=False):
        if _dist_to_path_m(a, path) <= corridor and _dist_to_path_m(b, path) <= corridor:
            metres += _seg_len_m(a, b)
    return (Decimal(str(metres)) / 1000).quantize(Decimal('0.1'))


def route_points(polyline, pickup, drop):
    from servers.safety.geometry import decode_polyline

    points = decode_polyline(polyline) if polyline else []
    if len(points) < 2:
        points = [tuple(map(float, pickup)), tuple(map(float, drop))]
    return _densify(points)


# ---------------------------------------------------------------------------
# The catalogue in force, and matching a route against it
# ---------------------------------------------------------------------------
def items_in_force(country, day):
    """{code: row} in force on `day` (newest version first wins); retired items are absent."""
    rows = {}
    for r in (TripChargeItem.objects.filter(country=str(country).upper(), status__in=(ACTIVE, ARCHIVED),
                                            effective_from__lte=day)
              .exclude(effective_to__lte=day).order_by('-effective_from', '-version')):
        rows.setdefault(r.code, r)
    return rows


def _amount_for(item, vehicle_type):
    amounts = item.amounts or {}
    for key in (str(vehicle_type).lower(), '*'):
        if amounts.get(key) is not None:
            return int(amounts[key])
    return None


def rider_label(item):
    if item.kind in TOLL_KINDS:
        return f'Toll (estimated): {item.name}'
    return item.name


def match(items, points, pickup, drop, vehicle_type, currency):
    """The charges this route meets, one line per item, every payer (only `rider` moves the fare)."""
    from base.money import from_minor, round_fare, to_minor

    pickup, drop = tuple(map(float, pickup)), tuple(map(float, drop))
    lines = []
    for item in items:
        per = _amount_for(item, vehicle_type)
        if per is None or item.kind == WAITING:
            continue
        km = None
        if item.kind == TOLL_PLAZA:
            plaza = _latlngs([(item.geometry or {}).get('coordinates')])
            hit = bool(plaza) and any(_seg_dist_m(plaza[0], a, b) <= (item.radius_m or 60)
                                      for a, b in zip(points, points[1:], strict=False))
            minor = per if hit else 0
        elif item.kind == TOLLED_ROAD:
            km = tolled_km(item, points)
            hit = km >= MIN_TOLLED_KM
            minor = to_minor(round_fare(from_minor(per, currency) * km, currency), currency) if hit else 0
        elif item.kind in (AIRPORT, VENUE):
            ring = _ring(item)
            at_pickup = item.applies_on in (PICKUP, PICKUP_OR_DROP) and _in_ring(pickup, ring)
            at_drop = item.applies_on in (DROP, PICKUP_OR_DROP) and _in_ring(drop, ring)
            hit, minor = at_pickup or at_drop, per
        elif item.kind == PERMIT:
            ring = _ring(item)
            hit, minor = _in_ring(pickup, ring) and not _in_ring(drop, ring), per
        else:
            continue
        if not hit or minor <= 0:
            continue
        lines.append({'code': LINE_CODE, 'item_code': item.code, 'item_version': item.version,
                      'kind': item.kind, 'label': rider_label(item), 'amount_minor': int(minor),
                      'amount': from_minor(int(minor), currency), 'who_pays': item.who_pays,
                      'estimated': item.kind in TOLL_KINDS, 'sample': item.sample,
                      'km': str(km) if km is not None else None, 'remit_to': item.remit_to or '',
                      'collection': collection_of(item)})
    return lines


def _enabled(zone):
    if production_hold():
        return False
    try:
        from servers.platform_ops.flags import flag_enabled
        from servers.pricing.commission import city_node_of, country_code_of_zone

        return flag_enabled(FLAG, city=city_node_of(getattr(zone, 'geo_node', None)),
                            country=country_code_of_zone(zone))
    except Exception:  # noqa: BLE001
        return False


def waiting_terms(country, scopes, vehicle_type, currency, day, items=None):
    """The waiting rule for this class here (most specific place first), or None."""
    from . import fee_plans

    items = items if items is not None else items_in_force(country, day)
    waits = {i.scope_code: i for i in items.values() if i.kind == WAITING}
    for code in list(scopes) + ['']:
        item = waits.get(code)
        per = _amount_for(item, vehicle_type) if item else None
        if item is None or per is None:
            continue
        words = (f'First {item.free_minutes} min of waiting free, then {fee_plans._money(per, currency)}/min'
                 if per else 'Waiting is free')
        return {'item_code': item.code, 'free_minutes': item.free_minutes, 'per_minute_minor': per,
                'words': words, 'sample': item.sample}
    return None


def estimate(zone, vehicle_type, pickup, drop, polyline='', currency='INR', when=None):
    """`{lines, rider_minor, waiting, crosses_toll}` for a quote; empty when the flag is off here."""
    empty = {'lines': [], 'rider_minor': 0, 'waiting': None, 'crosses_toll': False}
    if zone is None or drop is None or None in tuple(drop) or not _enabled(zone):
        return empty
    from servers.pricing.commission import country_code_of_zone

    from . import fee_plans

    country = country_code_of_zone(zone)
    day = fee_plans.local_day(country, when, zone)
    items = items_in_force(country, day)
    if not items:
        return empty
    points = route_points(polyline, pickup, drop)
    lines = match(items.values(), points, pickup, drop, vehicle_type, currency)
    return {'lines': lines, 'rider_minor': sum(x['amount_minor'] for x in lines if x['who_pays'] == RIDER),
            'waiting': waiting_terms(country, fee_plans.scope_codes_of_zone(zone), vehicle_type, currency, day,
                                     items),
            'crosses_toll': any(x['kind'] in TOLL_KINDS for x in lines)}


def stored_lines(lines):
    """The lines as FarePricing keeps them (JSON: minor units, no Decimals)."""
    return [{k: v for k, v in x.items() if k != 'amount'} for x in lines]


# ---------------------------------------------------------------------------
# The console: the catalogue, the Edit preview, the request, the executor
# ---------------------------------------------------------------------------
def _currency(country):
    from base.countries import get_country

    return get_country(country).currency


def _money(minor, currency):
    from . import fee_plans

    return '—' if minor is None else fee_plans._money(int(minor), currency)


def amount_words(item_kind, minor, currency):
    return f'{_money(minor, currency)}{UNIT_WORDS.get(item_kind, "")}' if minor is not None else 'not charged'


def item_dict(r):
    return {'id': r.id, 'code': r.code, 'version': r.version, 'status': r.status, 'kind': r.kind,
            'kind_words': KIND_WORDS.get(r.kind, r.kind), 'name': r.name, 'scope_code': r.scope_code,
            'geometry': r.geometry, 'radius_m': r.radius_m, 'corridor_m': r.corridor_m,
            'applies_on': r.applies_on, 'applies_words': APPLIES_WORDS.get(r.applies_on, ''),
            'who_pays': r.who_pays, 'who_words': who_words(r.kind, r.who_pays),
            'collection': collection_of(r), 'collection_words': COLLECTION_WORDS.get(collection_of(r), ''),
            'currency': r.currency, 'amounts': r.amounts,
            'amount_words': {vt: amount_words(r.kind, m, r.currency) for vt, m in (r.amounts or {}).items()},
            'free_minutes': r.free_minutes, 'remit_to': r.remit_to, 'sample': r.sample,
            'effective_from': r.effective_from.isoformat(),
            'effective_to': r.effective_to.isoformat() if r.effective_to else None,
            'created_by': r.created_by_label, 'reason': r.reason, 'approval_request_id': r.approval_request_id,
            'created_at': r.created_at.isoformat() if r.created_at else None}


def catalogue(country, day=None):
    """Items in force today and those scheduled to start, per country."""
    from . import fee_plans

    country = str(country).upper()
    day = day or fee_plans.local_day(country)
    now = items_in_force(country, day)
    later = (TripChargeItem.objects.filter(country=country, status=ACTIVE, effective_from__gt=day)
             .order_by('effective_from'))
    return {'country': country, 'currency': _currency(country),
            'items': [item_dict(r) for r in sorted(now.values(), key=lambda r: (r.kind, r.name))],
            'scheduled': [item_dict(r) for r in later]}


def history(country, code):
    return [item_dict(r) for r in TripChargeItem.objects.filter(country=country, code=code).order_by('-version')[:50]]


def _num(value, field, lo, hi, what):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise InvalidRequest(f'{what} is a whole number.', code='INVALID_NUMBER', field=field)
    if not lo <= n <= hi:
        raise InvalidRequest(f'{what} is between {lo} and {hi}.', code='OUT_OF_RANGE', field=field)
    return n


def _clean_geometry(kind, geometry):
    g = geometry if isinstance(geometry, dict) else {}
    coords = g.get('coordinates')

    def pair(c):
        try:
            lng, lat = float(c[0]), float(c[1])
        except (TypeError, ValueError, IndexError):
            raise InvalidRequest('Each point is [longitude, latitude].', code='INVALID_GEOMETRY', field='geometry')
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            raise InvalidRequest('A point is outside the map.', code='INVALID_GEOMETRY', field='geometry')
        return [round(lng, 6), round(lat, 6)]

    if kind == WAITING:
        return {}
    if kind == TOLL_PLAZA:
        return {'type': 'Point', 'coordinates': pair(coords)}
    if kind == TOLLED_ROAD:
        path = [pair(c) for c in (coords or [])]
        if len(path) < 2:
            raise InvalidRequest('Draw the road with at least 2 points.', code='INVALID_GEOMETRY', field='geometry')
        return {'type': 'LineString', 'coordinates': path}
    ring = [pair(c) for c in ((coords or [[]])[0] if coords and isinstance(coords[0][0], (list, tuple))
                              else (coords or []))]
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        raise InvalidRequest('Draw the area with at least 3 points.', code='INVALID_GEOMETRY', field='geometry')
    return {'type': 'Polygon', 'coordinates': [ring + ring[:1]]}


def _slug(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:50] or 'item'


def _clean(country, data, current):
    from base.money import normalize_currency

    from . import edit_pattern

    data = data or {}
    kind = data.get('kind') or (current.kind if current else '')
    if kind not in KINDS:
        raise InvalidRequest('Choose what kind of charge this is.', code='INVALID_KIND', field='kind')
    if current is not None and kind != current.kind:
        raise InvalidRequest('The kind of an item cannot change: add a new item.', code='KIND_FIXED', field='kind')
    name = str(data.get('name', current.name if current else '') or '').strip()
    if not 3 <= len(name) <= 120:
        raise InvalidRequest('Give it a name riders will understand (3 to 120 characters).',
                             code='INVALID_NAME', field='name')
    scope_code = str(data.get('scope_code', current.scope_code if current else '') or '')
    who = data.get('who_pays', current.who_pays if current else RIDER)
    if who not in WHO_PAYS:
        raise InvalidRequest('Say who pays.', code='INVALID_WHO_PAYS', field='who_pays')
    out = {'kind': kind, 'name': name, 'scope_code': scope_code, 'who_pays': who,
           'geometry': _clean_geometry(kind, data.get('geometry', current.geometry if current else None)),
           'radius_m': 0, 'corridor_m': 0, 'applies_on': '', 'free_minutes': 0, 'remit_to': '', 'collection': ''}
    if kind == WAITING and who != RIDER:
        raise InvalidRequest('Waiting is paid by the rider and goes to the driver.', code='INVALID_WHO_PAYS',
                             field='who_pays')
    if kind in TOLL_KINDS:
        out['collection'] = AT_GATE
    if kind == TOLL_PLAZA:
        out['radius_m'] = _num(data.get('radius_m', current.radius_m if current else 60), 'radius_m', 20, 500,
                               'The plaza radius in metres')
    if kind == TOLLED_ROAD:
        out['corridor_m'] = _num(data.get('corridor_m', current.corridor_m if current else 40), 'corridor_m',
                                 10, 200, 'The road corridor in metres')
    if kind in (AIRPORT, VENUE):
        out['applies_on'] = data.get('applies_on', current.applies_on if current else PICKUP)
        if out['applies_on'] not in APPLIES_WORDS:
            raise InvalidRequest('Say whether it is charged on pickups, drops or both.', code='INVALID_APPLIES_ON',
                                 field='applies_on')
        out['collection'] = data.get('collection', (current.collection if current else '') or AT_GATE)
        if out['collection'] not in COLLECTIONS:
            raise InvalidRequest('Say who pays the authority: the driver at the gate, or SaaradhiGo (permit).',
                                 code='INVALID_COLLECTION', field='collection')
        if out['collection'] == PLATFORM_REMITS:
            out['remit_to'] = str(data.get('remit_to', current.remit_to if current else '') or '').strip()[:120]
            if len(out['remit_to']) < 3:
                raise InvalidRequest('Say which authority SaaradhiGo pays.', code='REMIT_TO_REQUIRED',
                                     field='remit_to')
    if kind == WAITING:
        out['free_minutes'] = _num(data.get('free_minutes', current.free_minutes if current else 3),
                                   'free_minutes', 0, 30, 'Free waiting minutes')
    if kind == PERMIT:
        out['collection'] = PLATFORM_REMITS
        out['remit_to'] = str(data.get('remit_to', current.remit_to if current else '') or '').strip()[:120]
        if len(out['remit_to']) < 3:
            raise InvalidRequest('Say who the permit charge is paid to.', code='REMIT_TO_REQUIRED', field='remit_to')
    amounts = data.get('amounts', current.amounts if current else None)
    if not isinstance(amounts, dict) or not amounts:
        raise InvalidRequest('Give an amount for at least one class.', code='AMOUNTS_REQUIRED', field='amounts')
    offered = set(edit_pattern.offered_classes(country, scope_code))
    currency = _currency(country)
    limit = MAX_UNITS.get(kind, MAX_UNITS_DEFAULT) * 100
    clean = {}
    for vt, minor in amounts.items():
        vt = str(vt).lower()
        if minor is None or minor == '':
            continue  # this class is not charged (e.g. autos may not use the ORR)
        if vt not in offered:
            raise InvalidRequest(f'{edit_pattern.class_word(vt)} is not sold here.', code='INVALID_CLASS',
                                 field='amounts')
        minor = _num(minor, 'amounts', 0, limit, f'{edit_pattern.class_word(vt)}: the amount (minor units)')
        # A per-km or per-minute rate may carry paise; every charged line is whole rupees.
        if normalize_currency(currency) == 'INR' and minor % 100 and kind not in UNIT_WORDS:
            raise InvalidRequest('Charges in India are whole rupees.', code='WHOLE_RUPEES', field='amounts')
        clean[vt] = minor
    if not clean:
        raise InvalidRequest('Give an amount for at least one class.', code='AMOUNTS_REQUIRED', field='amounts')
    out['amounts'] = clean
    # A QA sample value is marked as such everywhere; production never takes one.
    from django.conf import settings

    out['sample'] = bool(data.get('sample')) and not getattr(settings, 'IS_PRODUCTION', False)
    return out


def _start(country, requested):
    from . import fee_plans

    earliest = fee_plans.local_day(country) + timedelta(days=1)
    if not requested:
        return earliest
    try:
        chosen = date.fromisoformat(str(requested)[:10])
    except ValueError:
        raise InvalidRequest('The start date is YYYY-MM-DD.', code='INVALID_DATE', field='effective_from')
    if chosen < earliest:
        raise InvalidRequest(f'The earliest start is {earliest.isoformat()}.', code='TOO_EARLY',
                             field='effective_from')
    return chosen


def _current(country, code):
    from . import fee_plans

    return items_in_force(country, fee_plans.local_day(country)).get(code) or (
        TripChargeItem.objects.filter(country=country, code=code, status=ACTIVE).order_by('-version').first())


FIELD_WORDS = (('name', 'Name'), ('who_pays', 'Who pays'), ('applies_on', 'Charged on'),
               ('radius_m', 'Plaza radius (m)'), ('corridor_m', 'Road corridor (m)'),
               ('free_minutes', 'Free waiting minutes'), ('collection', 'Who pays the authority'),
               ('remit_to', 'Paid to'))


def preview(country, data):
    """The Edit preview: what changes, what a rider or driver sees per class, and the first day."""
    from . import edit_pattern

    country = str(country).upper()
    data = data or {}
    op = data.get('op') or 'save'
    code = str(data.get('code') or '').strip()
    current = _current(country, code) if code else None
    if code and current is None:
        raise InvalidRequest('Unknown item.', code='UNKNOWN_ITEM', field='code')
    start = _start(country, data.get('effective_from'))
    currency = _currency(country)
    if op == 'retire':
        if current is None:
            raise InvalidRequest('Choose the item to remove.', code='UNKNOWN_ITEM', field='code')
        changes = [{'label': f'{current.name}', 'from': 'In the catalogue', 'to': 'Removed'}]
        return {'op': op, 'code': code, 'changes': changes, 'item': None,
                'impact': {'title': 'What riders pay', 'columns': ['Class', 'Today', 'After the change'],
                           'rows': [[edit_pattern.class_word(vt), amount_words(current.kind, m, currency),
                                     'not charged'] for vt, m in (current.amounts or {}).items()],
                           'note': 'New quotes stop showing this charge from the start date.'},
                'start': {'effective_from': start.isoformat()}}
    item = _clean(country, data, current)
    changes = []
    if current is None:
        changes.append({'label': 'New item', 'from': '—', 'to': f"{KIND_WORDS[item['kind']]}: {item['name']}"})
    else:
        for key, word in FIELD_WORDS:
            before, after = (collection_of(current) if key == 'collection' else getattr(current, key)), item[key]
            if before != after and (before or after):
                show = (WHO_WORDS if key == 'who_pays' else APPLIES_WORDS if key == 'applies_on'
                        else COLLECTION_WORDS if key == 'collection' else {})
                changes.append({'label': f'{current.name}: {word.lower()}', 'from': str(show.get(before, before)),
                                'to': str(show.get(after, after))})
        if current.geometry != item['geometry']:
            changes.append({'label': f'{current.name}: where it is', 'from': 'Old outline', 'to': 'New outline'})
        if current.sample != item['sample']:
            changes.append({'label': f'{current.name}: sample value',
                            'from': 'QA sample' if current.sample else 'Real figure',
                            'to': 'QA sample' if item['sample'] else 'Real figure'})
    old_amounts = (current.amounts if current else {}) or {}
    rows = []
    for vt in sorted(set(old_amounts) | set(item['amounts'])):
        before, after = old_amounts.get(vt), item['amounts'].get(vt)
        if before != after:
            changes.append({'label': f"{item['name']}, {edit_pattern.class_word(vt)}",
                            'from': amount_words(item['kind'], before, currency),
                            'to': amount_words(item['kind'], after, currency)})
        rows.append([edit_pattern.class_word(vt), amount_words(item['kind'], before, currency),
                     amount_words(item['kind'], after, currency)])
    payer = who_words(item['kind'], item['who_pays'])
    note = ('Passed through on the rider\'s fare as its own line, inside the locked total. '
            'No commission or fee is taken on it.' if item['who_pays'] == RIDER else
            f'{payer}. It is not added to the rider\'s fare.')
    if item['kind'] == WAITING:
        note = ('Waiting is charged only when the rider keeps the driver waiting past the free minutes. It is '
                "the driver's earnings for that time: the driver fee or commission applies, as on the fare.")
    elif item['who_pays'] == RIDER and item['collection'] == PLATFORM_REMITS:
        note += f" SaaradhiGo pays it to {item['remit_to']}."
    elif item['who_pays'] == RIDER:
        note += ' The driver pays it at the gate and is reimbursed in full.'
    return {'op': op, 'code': code, 'changes': changes, 'item': item,
            'impact': {'title': 'What this charges', 'columns': ['Class', 'Today', 'After the change'],
                       'rows': rows, 'note': note},
            'start': {'effective_from': start.isoformat()}}


def display(body):
    return {'changes': body['changes'], 'impact': body['impact'],
            'effective': f"Starts {body['start']['effective_from']} (local day), for new quotes."}


def propose(actor, country, data, *, http_request=None, system_label=''):
    import hashlib
    import json

    from servers.admin_access.bridge import granted
    from servers.approvals.models import ApprovalRequest

    from .fee_plan_workflow import _node

    country = str(country).upper()
    data = data or {}
    reason = str(data.get('reason') or '').strip()
    if len(reason) < 5:
        raise InvalidRequest('Say why, in a few words.', code='REASON_REQUIRED', field='reason')
    body = preview(country, data)
    if not body['changes']:
        raise InvalidRequest('Nothing is changed.', code='NO_CHANGE', field='general')
    scope_code = body['item']['scope_code'] if body['item'] else _current(country, body['code']).scope_code
    node = _node(country, scope_code)
    if node is None:
        raise InvalidRequest('Unknown place.', code='UNKNOWN_PLACE', field='scope_code')
    if actor is not None and not granted(actor, 'pricing.write', node)[0]:
        raise NotPermitted('You do not hold pricing.write here.', code='SCOPE_NOT_HELD', field='scope_code')
    code = body['code']
    if not code and TripChargeItem.objects.filter(
            country=country, status=ACTIVE, kind=body['item']['kind'], name__iexact=body['item']['name']).exists():
        raise InvalidRequest('An item with this name and kind is already in the catalogue: edit that one.',
                             code='DUPLICATE_ITEM', field='name')
    if not code:
        base = _slug(body['item']['name'])
        taken = set(TripChargeItem.objects.filter(country=country, code__startswith=base)
                    .values_list('code', flat=True))
        code = base if base not in taken else next(f'{base}-{n}' for n in range(2, 999) if f'{base}-{n}' not in taken)
    if ApprovalRequest.objects.filter(action_key=PRICING_CHANGE, target_type=KIND, status='requested',
                                      target_id=f'{country}:{code}').exists():
        raise InvalidRequest('A change to this item is already waiting: withdraw it or wait.',
                             code='CHANGE_WAITING', field='general')
    payload = {'kind': KIND, 'country': country, 'code': code, 'op': body['op'], 'item': body['item'],
               'requested_from': body['start']['effective_from'], 'display': display(body)}
    last = TripChargeItem.objects.filter(country=country, code=code).order_by('-version').first()
    digest = hashlib.sha256(json.dumps([body['op'], body['item']], sort_keys=True).encode()).hexdigest()[:12]
    return require_approval_or_execute(
        PRICING_CHANGE, actor=actor, country=country, scope_node=node, target_type=KIND,
        target_id=f'{country}:{code}', payload=payload,
        idempotency_key=f"{KIND}:{country}:{code}:v{last.version if last else 0}:{digest}",
        reason=reason, http_request=http_request, system_label=system_label)


def execute(request_row):
    from . import fee_plans

    p = request_row.payload or {}
    country, code = p['country'], p['code']
    start = max(fee_plans.local_day(country) + timedelta(days=1), date.fromisoformat(p['requested_from']))
    with transaction.atomic():
        current = list(TripChargeItem.objects.select_for_update()
                       .filter(country=country, code=code, status=ACTIVE))
        before = None
        for old in sorted(current, key=lambda r: r.effective_from):
            if old.effective_from < start:
                before = item_dict(old)
            old.status, old.effective_to = ARCHIVED, start
            old.save(update_fields=['status', 'effective_to'])
        if p.get('op') == 'retire':
            record_admin_action(None, action='trip_charge_item_removed', target_type=KIND, target_id=code,
                                before=before, after={'effective_to': start.isoformat()},
                                reason=request_row.decision_reason or request_row.request_reason)
            return {'code': code, 'removed_from': start.isoformat()}
        last = TripChargeItem.objects.filter(country=country, code=code).order_by('-version').first()
        row = TripChargeItem.objects.create(
            country=country, code=code, version=(last.version + 1) if last else 1, status=ACTIVE,
            currency=_currency(country), effective_from=start,
            approval_request_id=request_row.id, created_by_label=request_row.maker_label or '',
            reason=request_row.request_reason or '', **p['item'])
        record_admin_action(None, action='trip_charge_item_changed', target_type=KIND, target_id=str(row.id),
                            before=before, after=item_dict(row),
                            reason=request_row.decision_reason or request_row.request_reason)
    return {'trip_charge_item_id': row.id, 'effective_from': start.isoformat()}


__all__ = ['DRIVER_REIMBURSEMENT_LIVE', 'FLAG', 'on_behalf_note', 'production_hold', 'waiting_charge',
           'who_words', 'KIND', 'LINE_CODE', 'catalogue', 'estimate', 'execute', 'history', 'match', 'preview',
           'propose', 'stored_lines', 'waiting_terms']
