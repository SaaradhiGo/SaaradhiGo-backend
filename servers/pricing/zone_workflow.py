"""Service zones through maker-checker (module 9).

NO ZONE CHANGES BY ITSELF
-------------------------
Creating a zone, changing its polygon or settings, deactivating it, reactivating it
and restoring an earlier revision all raise a `zone.change` request (always a
checker, in India and the US). Nothing is written until a DIFFERENT holder of
`approval.check` + `zone.write`, in the zone's scope, approves. The existing zone
endpoints keep their URLs and answer 202 with the pending request. A zone is never
deleted: DELETE is a deactivation request.

EVERY APPLIED CHANGE IS A REVISION
----------------------------------
`ServiceZoneRevision` snapshots the whole zone after each applied change, with the
version, the reason, the maker and the checker. Rollback is "propose restoring
revision N": a normal request, validated against the world as it is THEN.

THE RULES (server side; the console only mirrors them)
------------------------------------------------------
* the polygon: `zone_geometry.validate` (closed ring, no self-intersection, ranges,
  0.01 to 20,000 km2);
* two ACTIVE city-type zones in one country must not overlap (sharing a border is
  fine);
* a child zone (an airport, any non-city type with a parent) must lie inside its
  parent, and a parent change must keep its children inside;
* the country is required and must be the geo node's country; creating a zone needs a
  CITY geo node in the maker's scope;
* currency, timezone and distance unit must match the country's market defaults
  (`base.countries`) unless the maker gives an override reason; only fields that
  CHANGE are checked, so a legacy zone is not blocked from an unrelated edit;
* deactivation is refused while a trip in the zone is requested, accepted, reached
  or in progress, and while it would leave a LIVE city with no active zone.

The executor re-runs every rule when the checker approves (the world may have moved)
and refuses with `ExecutionRefused` rather than applying a stale change.
"""

import hashlib
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from base.countries import COUNTRIES, get_country
from servers.admin_access.bridge import granted
from servers.admin_access.models import GeoNode
from servers.admin_access.scoping import scoped_or_none
from servers.admin_audit.models import actor_snapshot
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import ZONE_CHANGE
from servers.approvals.service import (
    InvalidRequest, NotInScope, NotPermitted, WrongState, require_approval_or_execute,
)

from . import zone_geometry as geo
from .models import ServiceZone
from .services import _polygon_for_zone
from .zone_models import (
    KIND_BACKFILL, KIND_CREATE, KIND_DEACTIVATE, KIND_REACTIVATE, KIND_RESTORE, KIND_UPDATE,
    MIGRATION_ACTOR, SNAPSHOT_FIELDS, ServiceZoneRevision,
)

logger = logging.getLogger(__name__)

CODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9-]{1,63}$')
#: A trip in one of these states is "in progress": a zone cannot be deactivated under it.
IN_PROGRESS_STATUSES = ('requested', 'accepted', 'reached', 'in_progress')
#: Fields a maker may set on create or change. Code and geo node are fixed after create.
EDITABLE = ('name', 'country', 'state_code', 'city', 'zone_type', 'parent', 'priority',
            'polygon_geojson', 'currency', 'timezone_name', 'distance_unit', 'tax_mode',
            'per_trip_fee', 'per_trip_fee_label', 'metadata')
ZONE_TYPES = {k for k, _ in ServiceZone.ZONE_TYPE_CHOICES}
AUDIT_ACTIONS = {KIND_CREATE: 'service_zone_created', KIND_UPDATE: 'service_zone_updated',
                 KIND_DEACTIVATE: 'service_zone_deactivated',
                 KIND_REACTIVATE: 'service_zone_reactivated',
                 KIND_RESTORE: 'service_zone_restored'}


# ---------------------------------------------------------------------------
# Snapshots and versions
# ---------------------------------------------------------------------------

def zone_values(zone):
    """The zone as a JSON-able dict: every snapshot field, plus parent and geo node codes."""
    out = {}
    for f in SNAPSHOT_FIELDS:
        v = getattr(zone, f)
        out[f] = str(v) if isinstance(v, Decimal) else v
    out['parent'] = zone.parent.code if zone.parent_id else None
    out['geo_node'] = zone.geo_node.code if zone.geo_node_id else None
    return out


def current_version(zone):
    return zone.revisions.aggregate(v=Max('version'))['v'] or 0


def write_revision(zone, kind, *, request_row=None, reason='', restored_from=None):
    """Append the revision for the zone as it is now. Returns it."""
    values = zone_values(zone)
    row = ServiceZoneRevision(
        zone=zone, version=current_version(zone) + 1, kind=kind, snapshot=values,
        parent_code=values['parent'] or '', geo_node_code=values['geo_node'] or '',
        reason=(reason or '')[:10000], restored_from_version=restored_from,
        applied_at=timezone.now())
    if request_row is not None:
        row.maker_id, row.maker_label = request_row.maker_id, request_row.maker_label
        row.maker_email = request_row.maker_email
        row.checker_id, row.checker_label = request_row.checker_id, request_row.checker_label
        row.checker_email = request_row.checker_email
        row.approval_request_id = request_row.id
    row.save()
    return row


def ensure_baseline(zone):
    """A zone drawn by a seed command or a test has no revision yet. Before the first
    approved change, record the zone as it stood so history has a v1 to roll back to."""
    if zone.revisions.exists():
        return None
    values = zone_values(zone)
    return ServiceZoneRevision.objects.create(
        zone=zone, version=1, kind=KIND_BACKFILL, snapshot=values,
        parent_code=values['parent'] or '', geo_node_code=values['geo_node'] or '',
        reason='Revision 1: the zone as it stood before its first versioned change.',
        maker_label=MIGRATION_ACTOR, applied_at=timezone.now())


# ---------------------------------------------------------------------------
# Cleaning and validating values
# ---------------------------------------------------------------------------

def _text(raw, field, *, required=False, maximum=128):
    value = str(raw if raw is not None else '').strip()
    if required and not value:
        raise InvalidRequest(f'{field} is required.', code='FIELD_REQUIRED', field=field)
    if len(value) > maximum:
        raise InvalidRequest(f'{field} is too long (max {maximum}).', code='FIELD_TOO_LONG',
                             field=field)
    return value


def _money(raw, field):
    try:
        value = Decimal(str(raw)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidRequest(f'{field} must be a number.', code='INVALID_NUMBER', field=field)
    if value < 0 or value > Decimal('100000'):
        raise InvalidRequest(f'{field} must be between 0 and 100000.', code='INVALID_NUMBER',
                             field=field)
    return value


def _geometry(value):
    try:
        return geo.validate(value)
    except geo.GeometryError as exc:
        raise InvalidRequest(exc.message, code=exc.code, field='polygon_geojson')


def clean_values(raw, base=None):
    """Validate a maker's input against `base` (the zone's current values, or None for a
    new zone). Returns `(values, geometry, area_km2, market_notes)`.

    `values` is the FULL resulting zone (JSON-able) with market fields resolved to
    their effective value, so the payload and the revision say exactly what applies.
    Raises `InvalidRequest` with a stable code.
    """
    raw = dict(raw or {})
    creating = base is None
    values = dict(base or {})
    unknown = set(raw) - set(EDITABLE) - {'code', 'geo_node', 'is_active', 'reason',
                                            'market_override_reason'}
    if unknown:
        raise InvalidRequest(f'Unknown field(s): {", ".join(sorted(unknown))}.',
                             code='UNKNOWN_FIELD', field=sorted(unknown)[0])
    if not creating:
        if 'code' in raw and str(raw['code']).strip() != values['code']:
            raise InvalidRequest('A zone code cannot be changed.', code='CODE_IMMUTABLE',
                                 field='code')
        if 'geo_node' in raw and raw['geo_node'] not in (None, '', values.get('geo_node')):
            raise InvalidRequest('A zone cannot be moved to another geo node.',
                                 code='GEO_NODE_IMMUTABLE', field='geo_node')
    for k in EDITABLE:
        if k in raw:
            values[k] = raw[k]
    if creating:
        code = _text(raw.get('code'), 'code', required=True, maximum=64).upper()
        if not CODE_RE.match(code):
            raise InvalidRequest('code must be capital letters, digits and hyphens '
                                 '(e.g. IN-TG-HYD-AIRPORT).', code='INVALID_CODE', field='code')
        values['code'] = code
        values['geo_node'] = _text(raw.get('geo_node'), 'geo_node', maximum=32)
        values['is_active'] = raw.get('is_active', True) not in (False, 'false', 'False', 0, '0')

    values['name'] = _text(values.get('name'), 'name', required=True)
    country = _text(values.get('country'), 'country', required=True, maximum=2).upper()
    if country not in COUNTRIES:
        raise InvalidRequest(f'Unknown country {country!r}.', code='UNKNOWN_COUNTRY',
                             field='country')
    values['country'] = country
    values['state_code'] = _text(values.get('state_code'), 'state_code', maximum=8)
    values['city'] = _text(values.get('city'), 'city', maximum=64)
    zone_type = _text(values.get('zone_type'), 'zone_type', required=True, maximum=16)
    if zone_type not in ZONE_TYPES:
        raise InvalidRequest(f'zone_type must be one of {sorted(ZONE_TYPES)}.',
                             code='INVALID_ZONE_TYPE', field='zone_type')
    values['zone_type'] = zone_type
    try:
        priority = int(values.get('priority', 10))
    except (TypeError, ValueError):
        raise InvalidRequest('priority must be a whole number.', code='INVALID_NUMBER',
                             field='priority')
    if not 0 <= priority <= 1000:
        raise InvalidRequest('priority must be between 0 and 1000.', code='INVALID_NUMBER',
                             field='priority')
    values['priority'] = priority
    parent = values.get('parent')
    values['parent'] = _text(parent, 'parent', maximum=64) or None if parent else None
    if values['parent'] is not None and values['parent'] == values.get('code'):
        raise InvalidRequest('A zone cannot be its own parent.', code='PARENT_CYCLE',
                             field='parent')
    fee = _money(values.get('per_trip_fee', '0') or '0', 'per_trip_fee')
    values['per_trip_fee'] = str(fee)
    label = _text(values.get('per_trip_fee_label'), 'per_trip_fee_label', maximum=64)
    if fee > 0 and not label:
        raise InvalidRequest('per_trip_fee_label is required when a per-trip fee is set.',
                             code='FIELD_REQUIRED', field='per_trip_fee_label')
    values['per_trip_fee_label'] = label
    if not isinstance(values.get('metadata') or {}, dict):
        raise InvalidRequest('metadata must be an object.', code='INVALID_METADATA',
                             field='metadata')
    values['metadata'] = values.get('metadata') or {}
    tax_mode = _text(values.get('tax_mode'), 'tax_mode', maximum=10)
    if tax_mode and tax_mode not in ('inclusive', 'additive'):
        raise InvalidRequest("tax_mode must be 'inclusive' or 'additive'.",
                             code='INVALID_TAX_MODE', field='tax_mode')

    # Market fields: blank means "the country's default", resolved here so the
    # payload and the revision carry the value that will actually apply.
    profile = get_country(country)
    defaults = {'currency': profile.currency, 'timezone_name': profile.default_timezone,
                'distance_unit': profile.distance_unit, 'tax_mode': profile.tax_mode}
    notes = []
    override = str(raw.get('market_override_reason') or '').strip()
    for field in ('currency', 'timezone_name', 'distance_unit', 'tax_mode'):
        given = str(values.get(field) or '').strip()
        if field == 'currency':
            given = given.upper()
        effective = given or defaults[field]
        values[field] = effective
        changed = creating or effective != (base or {}).get(field)
        if field == 'tax_mode' or not changed or effective == defaults[field]:
            continue
        if not override:
            raise InvalidRequest(
                f'{field} {effective!r} differs from {country}\'s market default '
                f'{defaults[field]!r}. Give market_override_reason to set it on purpose.',
                code='MARKET_MISMATCH', field=field)
        notes.append(f'{field}={effective} (country default {defaults[field]}): {override}')
    try:
        ZoneInfo(values['timezone_name'])
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise InvalidRequest(f"{values['timezone_name']!r} is not an IANA time zone.",
                             code='INVALID_TIMEZONE', field='timezone_name')
    if values['distance_unit'] not in ('km', 'mi'):
        raise InvalidRequest("distance_unit must be 'km' or 'mi'.", code='INVALID_UNIT',
                             field='distance_unit')

    if creating and not values['geo_node']:
        raise InvalidRequest('geo_node is required: a zone hangs from a city.',
                             code='FIELD_REQUIRED', field='geo_node')
    geojson, geom, area = _geometry(values.get('polygon_geojson'))
    values['polygon_geojson'] = geojson
    values['is_active'] = bool(values.get('is_active', True))
    return values, geom, area, notes


def _parent_zone(values):
    if not values.get('parent'):
        return None
    parent = ServiceZone.objects.filter(code=values['parent']).first()
    if parent is None:
        raise InvalidRequest(f"No parent zone {values['parent']!r}.", code='PARENT_NOT_FOUND',
                             field='parent')
    if parent.country != values['country']:
        raise InvalidRequest('A parent zone must be in the same country.',
                             code='PARENT_COUNTRY_MISMATCH', field='parent')
    return parent


def check_world(values, geom, zone=None):
    """The rules that depend on OTHER zones. Raises `InvalidRequest`.

    `zone` is the existing zone being changed (None for a new one).
    """
    parent = _parent_zone(values)
    if parent is not None and zone is not None:
        cursor, seen = parent, {zone.id}
        while cursor is not None:
            if cursor.id in seen:
                raise InvalidRequest('That parent would make the zone its own ancestor.',
                                     code='PARENT_CYCLE', field='parent')
            seen.add(cursor.id)
            cursor = cursor.parent
    if parent is not None and values['zone_type'] != 'city':
        parent_geom = _polygon_for_zone(parent)
        if parent_geom is None or not geo.contains(parent_geom, geom):
            raise InvalidRequest(
                f'A {values["zone_type"]} zone must lie inside its parent {parent.code}.',
                code='CHILD_OUTSIDE_PARENT', field='polygon_geojson')
    if values['is_active'] and values['zone_type'] == 'city':
        others = ServiceZone.objects.filter(
            is_active=True, zone_type='city', country=values['country'])
        if zone is not None:
            others = others.exclude(pk=zone.pk)
        for other in others:
            other_geom = _polygon_for_zone(other)
            if other_geom is not None and geo.overlaps(geom, other_geom):
                raise InvalidRequest(
                    f'This city zone overlaps the active city zone {other.code}. Two '
                    f'active cities in one country cannot share ground.',
                    code='CITY_ZONES_OVERLAP', field='polygon_geojson')
    if zone is not None:
        outside = []
        for child in zone.children.all():
            child_geom = _polygon_for_zone(child)
            if (child.zone_type != 'city' and child_geom is not None
                    and not geo.contains(geom, child_geom)):
                outside.append(child.code)
        if outside:
            raise InvalidRequest(
                f'The new polygon would leave child zone(s) outside it: {", ".join(outside)}.',
                code='CHILD_OUTSIDE_PARENT', field='polygon_geojson')


def in_progress_trips(zone):
    from servers.ride.models import Trip

    return Trip.objects.filter(zone=zone, status_id__status_code__in=IN_PROGRESS_STATUSES)


def check_can_deactivate(zone):
    n = in_progress_trips(zone).count()
    if n:
        raise WrongState(
            f'{n} trip(s) in {zone.code} are in progress (requested, accepted, reached or '
            f'started). A zone cannot be deactivated under a live trip; try again when they end.',
            code='TRIPS_IN_PROGRESS', field='zone')
    node = zone.geo_node
    if node is not None and node.kind == 'city' and node.status == 'active':
        others = (ServiceZone.objects.filter(geo_node=node, is_active=True)
                  .exclude(pk=zone.pk).exists())
        if not others:
            raise WrongState(
                f'{node.code} is live and {zone.code} is its only active zone. Pause the city '
                f'first, or draw its replacement before deactivating this one.',
                code='CITY_WOULD_HAVE_NO_ZONE', field='zone')


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def city_node_in_scope(user, code):
    """The city GeoNode `code` if the caller may reach it, else an ApprovalError."""
    node = scoped_or_none(user, GeoNode.objects.all(), code=str(code or ''))
    if node is None:
        raise NotInScope('No such city.', field='geo_node')
    if node.kind != 'city':
        raise InvalidRequest('A zone must hang from a city geo node.',
                             code='GEO_NODE_NOT_CITY', field='geo_node')
    if not granted(user, 'zone.write', node)[0]:
        raise NotPermitted('You do not hold zone.write here.', code='SCOPE_NOT_HELD',
                           field='geo_node')
    return node


def zone_write_gate(user, zone):
    """403 (as an ApprovalError) when the zone is in scope but zone.write is not held."""
    if zone.geo_node is None or not granted(user, 'zone.write', zone.geo_node)[0]:
        raise NotPermitted('You do not hold zone.write here.', code='SCOPE_NOT_HELD',
                           field='zone')


# ---------------------------------------------------------------------------
# Proposing
# ---------------------------------------------------------------------------

def _digest(payload):
    raw = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
    # Grouped so a long digit run never reaches the approvals API (PII sweep).
    return '-'.join(raw[i:i + 6] for i in range(0, 24, 6))


def _need_reason(reason):
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    return reason.strip()


def _raise(actor, kind, zone_code, zone, node, payload, reason, http_request, country):
    return require_approval_or_execute(
        ZONE_CHANGE, actor=actor, country=country, scope_node=node,
        target_type='service_zone', target_id=str(zone.id) if zone is not None else f'new:{zone_code}',
        payload=payload,
        idempotency_key=f'zone.change:{kind}:{zone_code}:{_digest(payload)}',
        reason=reason, http_request=http_request)


def propose_create(actor, raw, *, reason='', http_request=None):
    values, geom, area, notes = clean_values(raw, None)
    reason = _need_reason(reason)
    node = city_node_in_scope(actor, values['geo_node'])
    node_country = node.country.code if node.country is not None else None
    if node_country != values['country']:
        raise InvalidRequest(
            f'The zone country {values["country"]} does not match the geo node '
            f'{node.code} ({node_country}).', code='COUNTRY_MISMATCH', field='country')
    if ServiceZone.objects.filter(code=values['code']).exists():
        raise WrongState(f'A zone {values["code"]} already exists.', code='ZONE_EXISTS',
                         field='code')
    check_world(values, geom)
    payload = {'kind': KIND_CREATE, 'zone_code': values['code'], 'values': values,
               'area_km2': round(area, 3), 'market_notes': notes}
    return _raise(actor, KIND_CREATE, values['code'], None, node, payload, reason,
                  http_request, values['country'])


def _existing_values(zone):
    return zone_values(zone)


def propose_update(actor, zone, changes, *, reason='', http_request=None):
    zone_write_gate(actor, zone)
    reason = _need_reason(reason)
    changes = dict(changes or {})
    base = _existing_values(zone)
    values, geom, area, notes = clean_values({**changes, 'market_override_reason':
                                              changes.get('market_override_reason', '')}, base)
    values['is_active'] = zone.is_active  # status moves only by deactivate / reactivate
    changed = sorted(k for k in values if values[k] != base.get(k))
    if not changed:
        raise InvalidRequest('Nothing would change.', code='NO_CHANGE', field='general')
    check_world(values, geom, zone)
    payload = {'kind': KIND_UPDATE, 'zone': zone.id, 'zone_code': zone.code,
               'base_version': current_version(zone), 'values': values, 'changed': changed,
               'area_km2': round(area, 3), 'market_notes': notes}
    return _raise(actor, KIND_UPDATE, zone.code, zone, zone.geo_node, payload, reason,
                  http_request, zone.country)


def propose_deactivate(actor, zone, *, reason='', http_request=None):
    zone_write_gate(actor, zone)
    reason = _need_reason(reason)
    if not zone.is_active:
        raise WrongState('That zone is already inactive.', code='ALREADY_INACTIVE', field='zone')
    check_can_deactivate(zone)
    payload = {'kind': KIND_DEACTIVATE, 'zone': zone.id, 'zone_code': zone.code,
               'base_version': current_version(zone)}
    return _raise(actor, KIND_DEACTIVATE, zone.code, zone, zone.geo_node, payload, reason,
                  http_request, zone.country)


def propose_reactivate(actor, zone, *, reason='', http_request=None):
    zone_write_gate(actor, zone)
    reason = _need_reason(reason)
    if zone.is_active:
        raise WrongState('That zone is already active.', code='ALREADY_ACTIVE', field='zone')
    values = {**_existing_values(zone), 'is_active': True}
    geom = geo.to_geometry(values['polygon_geojson'])
    check_world(values, geom, zone)
    payload = {'kind': KIND_REACTIVATE, 'zone': zone.id, 'zone_code': zone.code,
               'base_version': current_version(zone)}
    return _raise(actor, KIND_REACTIVATE, zone.code, zone, zone.geo_node, payload, reason,
                  http_request, zone.country)


def propose_restore(actor, zone, version, *, reason='', http_request=None):
    """Propose putting revision `version`'s polygon and settings back."""
    zone_write_gate(actor, zone)
    reason = _need_reason(reason)
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise InvalidRequest('version must be a whole number.', code='INVALID_NUMBER',
                             field='version')
    rev = zone.revisions.filter(version=version).first()
    if rev is None:
        raise InvalidRequest(f'Zone {zone.code} has no revision {version}.',
                             code='REVISION_NOT_FOUND', field='version')
    base = _existing_values(zone)
    snap = dict(rev.snapshot)
    snap['parent'] = rev.parent_code or snap.get('parent') or None
    wanted = {k: snap[k] for k in EDITABLE if k in snap}
    # The market fields are restored as they were: give them as an explicit override.
    values, geom, area, notes = clean_values(
        {**wanted, 'market_override_reason': f'restored from revision {version}'}, base)
    values['is_active'] = zone.is_active
    changed = sorted(k for k in values if values[k] != base.get(k))
    if not changed:
        raise InvalidRequest(f'The zone already matches revision {version}.', code='NO_CHANGE',
                             field='version')
    check_world(values, geom, zone)
    payload = {'kind': KIND_RESTORE, 'zone': zone.id, 'zone_code': zone.code,
               'base_version': current_version(zone), 'restore_version': version,
               'values': values, 'changed': changed, 'area_km2': round(area, 3),
               'market_notes': notes}
    return _raise(actor, KIND_RESTORE, zone.code, zone, zone.geo_node, payload, reason,
                  http_request, zone.country)


# ---------------------------------------------------------------------------
# Executing
# ---------------------------------------------------------------------------

def _apply(zone, values):
    parent = _parent_zone(values)
    for f in SNAPSHOT_FIELDS:
        if f == 'per_trip_fee':
            setattr(zone, f, Decimal(str(values[f])))
        else:
            setattr(zone, f, values[f])
    zone.parent = parent
    zone.save()


def _audit(kind, zone, request_row, before, after):
    """Audit the applied change. Guarded here as well as inside `record_admin_action`: an
    audit problem must never refuse a zone change a checker has just approved."""
    try:
        record_admin_action(
            None, action=AUDIT_ACTIONS[kind], target_type='service_zone', target_id=zone.id,
            before=before, after=after,
            reason=request_row.decision_reason or request_row.request_reason)
    except Exception as exc:  # noqa: BLE001
        logger.error('zone audit write failed kind=%s zone=%s detail=%s', kind, zone.id, exc)


def execute(request_row):
    p = request_row.payload or {}
    kind = p.get('kind')
    if kind == KIND_CREATE:
        return _execute_create(request_row, p)
    if kind in (KIND_UPDATE, KIND_RESTORE):
        return _execute_values(request_row, p, kind)
    if kind in (KIND_DEACTIVATE, KIND_REACTIVATE):
        return _execute_status(request_row, p, kind)
    raise registry.ExecutionRefused('UNKNOWN_ZONE_CHANGE', f'Unknown zone change {kind!r}.')


def _refuse(exc):
    code = getattr(exc, 'code', 'ZONE_CHANGE_REFUSED')
    return registry.ExecutionRefused(code, exc.message if hasattr(exc, 'message') else str(exc))


def _execute_create(request_row, p):
    values = p['values']
    with transaction.atomic():
        node = GeoNode.objects.filter(code=values['geo_node'], kind='city').first()
        if node is None:
            raise registry.ExecutionRefused('TARGET_GONE', 'The city geo node is gone.')
        if ServiceZone.objects.filter(code=values['code']).exists():
            raise registry.ExecutionRefused('ZONE_EXISTS',
                                            f'A zone {values["code"]} now exists.')
        try:
            values, geom, _area, _notes = clean_values(values, None)
            check_world(values, geom)
        except (InvalidRequest, WrongState) as exc:
            raise _refuse(exc)
        zone = ServiceZone(geo_node=node)
        _apply(zone, values)
        rev = write_revision(zone, KIND_CREATE, request_row=request_row,
                             reason=request_row.request_reason)
        _audit(KIND_CREATE, zone, request_row, {}, {'version': rev.version, **zone_values(zone)})
    return {'zone_id': zone.id, 'zone_code': zone.code, 'version': rev.version}


def _locked(request_row, p):
    zone = (ServiceZone.objects.select_for_update(of=('self',)).select_related('geo_node', 'parent')
            .filter(pk=p['zone']).first())
    if zone is None:
        raise registry.ExecutionRefused('TARGET_GONE', 'The zone is gone.')
    if current_version(zone) != p.get('base_version'):
        raise registry.ExecutionRefused(
            'ZONE_CHANGED', 'The zone changed after this request was raised; raise it again.')
    return zone


def _execute_values(request_row, p, kind):
    with transaction.atomic():
        zone = _locked(request_row, p)
        ensure_baseline(zone)
        before = {'version': current_version(zone), **zone_values(zone)}
        try:
            values, geom, _area, _notes = clean_values(
                {**p['values'], 'market_override_reason': 'approved request'}, zone_values(zone))
            values['is_active'] = zone.is_active
            check_world(values, geom, zone)
        except (InvalidRequest, WrongState) as exc:
            raise _refuse(exc)
        _apply(zone, values)
        rev = write_revision(
            zone, kind, request_row=request_row, reason=request_row.request_reason,
            restored_from=p.get('restore_version') if kind == KIND_RESTORE else None)
        _audit(kind, zone, request_row, before, {'version': rev.version, **zone_values(zone)})
    return {'zone_id': zone.id, 'zone_code': zone.code, 'version': rev.version,
            'changed': p.get('changed', [])}


def _execute_status(request_row, p, kind):
    with transaction.atomic():
        zone = _locked(request_row, p)
        ensure_baseline(zone)
        before = {'version': current_version(zone), 'is_active': zone.is_active}
        if kind == KIND_DEACTIVATE:
            if not zone.is_active:
                raise registry.ExecutionRefused('ALREADY_INACTIVE', 'The zone is already inactive.')
            try:
                check_can_deactivate(zone)
            except WrongState as exc:
                raise _refuse(exc)
            zone.is_active = False
        else:
            if zone.is_active:
                raise registry.ExecutionRefused('ALREADY_ACTIVE', 'The zone is already active.')
            values = {**zone_values(zone), 'is_active': True}
            try:
                check_world(values, geo.to_geometry(values['polygon_geojson']), zone)
            except (InvalidRequest, WrongState) as exc:
                raise _refuse(exc)
            zone.is_active = True
        zone.save()
        rev = write_revision(zone, kind, request_row=request_row,
                             reason=request_row.request_reason)
        _audit(kind, zone, request_row, before,
               {'version': rev.version, 'is_active': zone.is_active})
    return {'zone_id': zone.id, 'zone_code': zone.code, 'version': rev.version,
            'is_active': zone.is_active}


@registry.register(ZONE_CHANGE)
def execute_zone_change(request_row):
    return execute(request_row)


def maker_snapshot(user):
    return actor_snapshot(user)
