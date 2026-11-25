"""Pickup points, restricted areas and a service area per class (owner ruling, 25 Nov).

Nothing is set by default, so booking is unchanged until an area is configured. Each area belongs to
a zone and is a polygon:

    restricted   no pickup and no drop-off inside it ("AREA_RESTRICTED")
    pickup       inside it a pickup must be at one of its named points, within PICKUP_RADIUS_M
                 ("PICKUP_POINT_REQUIRED", with the points, so the app can offer them)
    class_area   a class is offered at a pickup only inside its area(s), when the zone has any for that
                 class ("CLASS_NOT_OFFERED_HERE"); a class with no area keeps the whole zone

Enforced on the server where every booking passes, so the rider and driver apps respect them with no
change of their own: the shared service-area gate (`base.service_area`, used by the fare quote and the
booking socket) and the class check of the quote. A change is a `pricing.change` request (kind
`zone_area`): a second person approves, with the before/after.
"""

import math

from django.db import transaction
from django.utils import timezone

from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import PRICING_CHANGE
from servers.approvals.service import InvalidRequest, require_approval_or_execute

from .zone_area_models import ZoneArea

KIND = 'zone_area'
RESTRICTED, PICKUP, CLASS_AREA = 'restricted', 'pickup', 'class_area'
KINDS = (RESTRICTED, PICKUP, CLASS_AREA)
KIND_WORDS = {RESTRICTED: 'Restricted area', PICKUP: 'Pickup points', CLASS_AREA: 'Service area for a class'}
PICKUP_RADIUS_M = 60


def _geom(area):
    from .zone_geometry import to_geometry

    return to_geometry(area.polygon_geojson)


def _point(lat, lng):
    from shapely.geometry import Point

    return Point(float(lng), float(lat))


def _metres(a_lat, a_lng, b_lat, b_lng):
    k = 111_320
    dx = (float(a_lng) - float(b_lng)) * k * math.cos(math.radians(float(a_lat)))
    dy = (float(a_lat) - float(b_lat)) * k
    return math.hypot(dx, dy)


def _active(kind, zone=None):
    qs = ZoneArea.objects.filter(kind=kind, is_active=True)
    return qs.filter(zone=zone) if zone is not None else qs


def check_points(pickup_lat, pickup_lng, drop_lat, drop_lng):
    """(ok, code, message, extra). Restricted areas, then pickup points."""
    try:
        pick, drop = _point(pickup_lat, pickup_lng), _point(drop_lat, drop_lng)
    except (TypeError, ValueError):
        return True, '', '', None
    for area in _active(RESTRICTED):
        g = _geom(area)
        if g.contains(pick) or g.contains(drop):
            side = 'Pickups' if g.contains(pick) else 'Drop-offs'
            return False, 'AREA_RESTRICTED', f'{side} are not allowed at {area.name}. Choose a spot nearby.', None
    for area in _active(PICKUP):
        if not _geom(area).contains(pick):
            continue
        near = [p for p in (area.points or [])
                if _metres(pickup_lat, pickup_lng, p['lat'], p['lng']) <= PICKUP_RADIUS_M]
        if not near:
            names = ', '.join(p['name'] for p in area.points or [])
            return False, 'PICKUP_POINT_REQUIRED', f'At {area.name}, pickups are at: {names}. Choose one of them.', \
                {'pickup_points': area.points, 'area': area.name}
    return True, '', '', None


def class_offered_at(zone, vehicle_type, lat, lng):
    """False when the zone has a service area for this class and the pickup is outside all of them."""
    if zone is None or not vehicle_type:
        return True
    areas = list(_active(CLASS_AREA, zone).filter(vehicle_type__iexact=vehicle_type))
    if not areas:
        return True
    try:
        p = _point(lat, lng)
    except (TypeError, ValueError):
        return True
    return any(_geom(a).contains(p) for a in areas)


# ---------------------------------------------------------------------------
# Changing them (the Edit pattern)
# ---------------------------------------------------------------------------

def _clean(zone, data):
    from .zone_geometry import GeometryError, area_km2, normalise, to_geometry

    kind = str(data.get('kind') or '')
    if kind not in KINDS:
        raise InvalidRequest('kind is restricted, pickup or class_area.', code='INVALID_KIND', field='kind')
    name = ' '.join(str(data.get('name') or '').split())[:120]
    if len(name) < 2:
        raise InvalidRequest('Give the area a name.', code='NAME_REQUIRED', field='name')
    try:
        poly = normalise(data.get('polygon_geojson'))
    except GeometryError as exc:
        raise InvalidRequest(str(exc), code='POLYGON_INVALID', field='polygon_geojson')
    km2 = area_km2(to_geometry(poly))
    out = {'kind': kind, 'name': name, 'polygon_geojson': poly, 'points': [], 'vehicle_type': '', 'km2': km2}
    if kind == PICKUP:
        pts = []
        for p in data.get('points') or []:
            try:
                pts.append({'name': ' '.join(str(p['name']).split())[:60], 'lat': float(p['lat']), 'lng': float(p['lng'])})
            except (KeyError, TypeError, ValueError):
                raise InvalidRequest('Each pickup point has a name, lat and lng.', code='INVALID_POINTS', field='points')
        if not pts or any(not to_geometry(poly).contains(_point(p['lat'], p['lng'])) for p in pts):
            raise InvalidRequest('Give at least one pickup point, inside the area.', code='INVALID_POINTS', field='points')
        out['points'] = pts
    if kind == CLASS_AREA:
        vt = str(data.get('vehicle_type') or '').strip().lower()
        from .regulation import rules_for_zone

        offered = rules_for_zone(zone).get('vehicle_classes') or []
        if not vt or (offered and vt not in offered):
            raise InvalidRequest("Choose one of this market's classes.", code='INVALID_CLASS', field='vehicle_type')
        out['vehicle_type'] = vt
    return out


def _describe(values):
    from servers.approvals.describe import CLASS_WORDS

    if values is None:
        return 'none'
    bits = f"{KIND_WORDS[values['kind']]}: {values['name']}"
    if values.get('vehicle_type'):
        bits += f" ({CLASS_WORDS.get(values['vehicle_type'], values['vehicle_type'])})"
    if values.get('points'):
        bits += f" · points {', '.join(p['name'] for p in values['points'])}"
    if values.get('km2') is not None:
        bits += f" · {values['km2']:.2f} km²"
    return bits


def display(zone, before, after):
    effect = {RESTRICTED: 'No pickup or drop-off inside it.', PICKUP: 'Pickups inside it only at the named points.',
              CLASS_AREA: 'That class is offered only for pickups inside it.'}
    kind = (after or before)['kind']
    return {'changes': [{'label': f'{zone.name}: {KIND_WORDS[kind].lower()}', 'from': _describe(before),
                         'to': _describe(after) if after else 'removed'}],
            'impact': {'title': 'What riders see', 'columns': ['Effect'],
                       'rows': [[effect[kind] if after else 'Booking works there as it did before this area.']]},
            'effective': 'From approval, for new bookings.'}


def preview(zone, data):
    return display(zone, None, _clean(zone, data))


def propose(actor, zone, data, reason, *, remove=None, http_request=None):
    from .fare_card_workflow import country_of

    if len((reason or '').strip()) < 5:
        raise InvalidRequest('Say why, in a few words.', code='REASON_REQUIRED', field='reason')
    if remove is not None:
        before = {'kind': remove.kind, 'name': remove.name, 'points': remove.points,
                  'vehicle_type': remove.vehicle_type, 'km2': None}
        payload = {'kind': KIND, 'zone': zone.id, 'remove': remove.id, 'display': display(zone, before, None)}
        key = f'{KIND}:remove:{remove.id}'
    else:
        values = _clean(zone, data)
        payload = {'kind': KIND, 'zone': zone.id, 'values': {k: v for k, v in values.items() if k != 'km2'},
                   'display': display(zone, None, values)}
        import hashlib
        import json

        key = f"{KIND}:{zone.id}:{hashlib.sha256(json.dumps(payload['values'], sort_keys=True).encode()).hexdigest()[:16]}"
    return require_approval_or_execute(
        PRICING_CHANGE, actor=actor, country=country_of(zone), scope_node=zone.geo_node,
        target_type=KIND, target_id=str(zone.id), payload=payload, idempotency_key=key, reason=reason,
        http_request=http_request)


def execute(request_row):
    p = request_row.payload or {}
    with transaction.atomic():
        if p.get('remove'):
            area = ZoneArea.objects.select_for_update().filter(pk=p['remove'], is_active=True).first()
            if area is None:
                raise registry.ExecutionRefused('AREA_GONE', 'That area is no longer active.')
            area.is_active, area.ended_at = False, timezone.now()
            area.save(update_fields=['is_active', 'ended_at'])
            record_admin_action(None, action='zone_area_removed', target_type=KIND, target_id=area.id,
                                before={'active': True}, after={'active': False}, reason=request_row.request_reason)
            return {'zone_area_id': area.id, 'removed': True}
        area = ZoneArea.objects.create(zone_id=p['zone'], approval_request_id=request_row.id,
                                       created_by_label=request_row.maker_label or '',
                                       reason=request_row.request_reason or '', **p['values'])
        record_admin_action(None, action='zone_area_added', target_type=KIND, target_id=area.id,
                            after={'zone': p['zone'], 'kind': area.kind, 'name': area.name},
                            reason=request_row.decision_reason or request_row.request_reason)
    return {'zone_area_id': area.id}
