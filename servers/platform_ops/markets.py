"""Markets: which countries and cities exist, which are live, and how they change.

A country is a row in `base/countries.py` (the registry: calling code, currency, tax mode) AND a
`GeoNode`. A city is a `GeoNode`. Its LIFECYCLE is `GeoNode.status`:

    draft  -> live -> paused -> live

(`live` is stored as `active`; see `admin_access.models`.) A country is LIVE when at least one of
its cities is live, PAUSED when none is live but one is paused, DRAFT otherwise. A country has no
status of its own to keep in step.

EVERY CHANGE GOES THROUGH A CHECKER
-----------------------------------
Adding a country, state or city, launching a city, pausing a city: each is a
`platform.city_status` approval request (a policy row per country, always a checker). Nothing here
writes `GeoNode.status`; the executor (`workflows`) calls `readiness.set_city_status`, which
re-checks the readiness checklist at approval time, so a launch that was green when asked and red
when approved is refused.
"""

import hashlib
import re

from django.db.models import Q

from base.countries import COUNTRIES, find_country
from servers.admin_access import readiness
from servers.admin_access.access import scope_paths
from servers.admin_access.bridge import granted
from servers.admin_access.models import (
    ACTIVE, CITY, COUNTRY, DRAFT, GLOBAL, PAUSED, STATE, GeoNode,
)
from servers.approvals import service as approvals
from servers.approvals.actions import PLATFORM_CITY_STATUS
from servers.approvals.models import REQUESTED, ApprovalRequest

from .errors import PlatformError

LIVE = 'live'
STATUS_LABEL = {ACTIVE: LIVE, PAUSED: 'paused', DRAFT: 'draft'}
CODE_RE = re.compile(r'^[A-Z]{2}(-[A-Z0-9]{1,8}){0,2}$')
NAME_MAX = 120

CITY_PAUSED = 'CITY_PAUSED'
CITY_NOT_LIVE = 'CITY_NOT_LIVE'
#: Neutral rider-facing text: it never says "paused by an operator" or why.
CITY_PAUSED_MESSAGE = 'SaaradhiGo is not taking new bookings in this area right now. Please try again later.'
CITY_NOT_LIVE_MESSAGE = 'SaaradhiGo is not available in this area yet.'


def label_of(status):
    return STATUS_LABEL.get(status, status)


# ---------------------------------------------------------------------------
# What is live
# ---------------------------------------------------------------------------

def live_cities():
    return GeoNode.objects.filter(kind=CITY, status=ACTIVE)


def country_code_of(node):
    country = node.country if node is not None else None
    return country.code if country is not None else None


def live_country_codes():
    codes = set()
    for node in live_cities().select_related('parent__parent'):
        code = country_code_of(node)
        if code:
            codes.add(code)
    return sorted(codes)


def country_status(code):
    """`live`, `paused` or `draft` for a country code, derived from its cities."""
    statuses = set(GeoNode.objects.filter(kind=CITY, path__startswith=f'GLOBAL/{code}/')
                   .values_list('status', flat=True))
    if not statuses:
        statuses = set(GeoNode.objects.filter(kind=CITY, path__startswith=f'{code}/')
                       .values_list('status', flat=True))
    if ACTIVE in statuses:
        return LIVE
    if PAUSED in statuses:
        return 'paused'
    return 'draft'


def policy_country():
    """The country a PLATFORM-WIDE approval request (no single country) is filed under.

    Approval policies are per country. A change with no country of its own (an app's minimum
    version, a global flag default, a setting) is filed under the first LIVE country, else the
    first registry country; the checker is a Super Admin either way, so the choice only decides
    which country's policy row is read (all are seeded ALWAYS)."""
    codes = live_country_codes()
    return codes[0] if codes else sorted(COUNTRIES)[0]


def default_phone_country():
    """The country a bare national number is read in: the sole LIVE country, else None.

    Never India by default. With two live countries a number typed without a country is
    ambiguous and `normalize_phone` returns None; the caller asks for the country or the plus."""
    codes = live_country_codes()
    return codes[0] if len(codes) == 1 else None


def resolve_login_phone(raw):
    """A phone typed at an operator sign-in, as E.164: the number of the ONE existing account that
    matches it in some supported country, else the sole live country's reading, else as typed.

    A bare national number is ambiguous when two countries are live, so the account decides; a
    number with its country code never needs this."""
    from django.contrib.auth import get_user_model

    from base.countries import normalize_phone

    raw = str(raw or '').strip()
    candidates = []
    for code in sorted(COUNTRIES):
        number = normalize_phone(raw, default_country=code)
        if number and number not in candidates:
            candidates.append(number)
    if len(candidates) > 1:
        found = list(get_user_model().objects.filter(phone_number__in=candidates)
                     .values_list('phone_number', flat=True)[:2])
        if len(found) == 1:
            return found[0]
    return normalize_phone(raw, default_country=default_phone_country()) or raw


def phone_country_for_operator(user):
    """The country an operator's own scope sits in, else the sole live country, else None."""
    paths = scope_paths(user)
    if paths:
        codes = set()
        for path in paths:
            parts = [p for p in path.split('/') if p]
            for part in parts:
                if part in COUNTRIES:
                    codes.add(part)
        if len(codes) == 1:
            return next(iter(codes))
    return default_phone_country()


# ---------------------------------------------------------------------------
# The booking gate
# ---------------------------------------------------------------------------

def city_of_zone(zone):
    """The city `GeoNode` a zone belongs to: its own link, else its parent zone's, upward."""
    seen = 0
    while zone is not None and seen < 6:
        node = zone.geo_node
        if node is not None:
            return node if node.kind == CITY else None
        zone = zone.parent
        seen += 1
    return None


def booking_gate_for_zone(zone):
    """`(ok, code, message)`: may a NEW booking start in this zone?

    Live city: yes. Paused: `CITY_PAUSED`. Draft: `CITY_NOT_LIVE`. A zone linked to no city is not
    gated (legacy rows; the zone itself is still required to be active). Trips already requested
    or in progress never come through here, so a pause never touches them."""
    city = city_of_zone(zone)
    if city is None or city.status == ACTIVE:
        return True, '', ''
    if city.status == PAUSED:
        return False, CITY_PAUSED, CITY_PAUSED_MESSAGE
    return False, CITY_NOT_LIVE, CITY_NOT_LIVE_MESSAGE


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _covered(user, queryset):
    """Nodes the operator's scope covers: each node under a path they hold, plus the ancestors
    needed to draw the tree (countries and states above a city they hold)."""
    paths = scope_paths(user)
    if paths is None:
        return queryset
    if not paths:
        return queryset.none()
    q = Q()
    for path in paths:
        q |= Q(path__startswith=path)
    return queryset.filter(q)


def pending_requests(node_ids=None):
    """Open `platform.city_status` requests keyed by target code (the latest per code)."""
    out = {}
    for row in ApprovalRequest.objects.filter(
            action_key=PLATFORM_CITY_STATUS, status=REQUESTED).order_by('id'):
        out[row.target_id] = row
    return out


def request_dict(row):
    p = row.payload or {}
    return {
        'id': row.id, 'kind': p.get('kind'), 'target': row.target_id,
        'to_status': label_of(p.get('to_status')) if p.get('to_status') else None,
        'node': p.get('node'), 'reason': row.request_reason,
        'maker_label': row.maker_label, 'maker_id': row.maker_id,
        'requested_at': row.requested_at.isoformat() if row.requested_at else None,
        'expires_at': row.expires_at.isoformat() if row.expires_at else None,
        'country': row.country,
    }


def city_row(node, user, pending=None, with_items=False):
    items = readiness.city_readiness(node) if (with_items or True) else []
    blocking = [i for i in items if i['blocking']]
    warnings = [i for i in items if i['status'] == 'warning']
    country = node.country
    state = node.parent if node.parent_id and node.parent.kind == STATE else None
    row = pending.get(node.code) if pending else None
    pause_ok = granted(user, 'platform.settings', node)[0] or granted(user, 'platform.city_pause', node)[0]
    return {
        'code': node.code, 'name': node.name, 'status': label_of(node.status),
        'country': country.code if country else None,
        'state': {'code': state.code, 'name': state.name} if state else None,
        'blocking_count': len(blocking), 'warning_count': len(warnings),
        'ready': not blocking,
        'readiness': items if with_items else None,
        'pending_request': request_dict(row) if row else None,
        'may_launch': granted(user, 'platform.settings', node)[0] and node.status != ACTIVE,
        'may_pause': pause_ok and node.status == ACTIVE,
    }


def overview(user):
    """Countries, each with its cities, scoped to what the operator may read."""
    nodes = list(_covered(user, GeoNode.objects.filter(kind__in=[COUNTRY, STATE, CITY])
                          .select_related('parent__parent')).order_by('path'))
    pending = pending_requests()
    countries = {}
    for node in nodes:
        if node.kind == COUNTRY:
            countries.setdefault(node.code, {'node': node, 'cities': []})
    for node in nodes:
        if node.kind != CITY:
            continue
        code = country_code_of(node)
        if code is None:
            continue
        slot = countries.setdefault(code, {'node': node.country, 'cities': []})
        slot['cities'].append(city_row(node, user, pending))
    out = []
    for code, slot in sorted(countries.items()):
        registry = find_country(code)
        cn = slot['node']
        out.append({
            'code': code, 'name': registry.name if registry else (cn.name if cn else code),
            'currency': registry.currency if registry else None,
            'status': country_status(code),
            'in_registry': registry is not None,
            'cities': slot['cities'],
            'may_add_city': granted(user, 'platform.settings', cn)[0] if cn else False,
        })
    return out


# ---------------------------------------------------------------------------
# Proposing a change
# ---------------------------------------------------------------------------

def _anchor_country(node):
    return country_code_of(node)


def _digest(*parts):
    return hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:24]


def propose_status(actor, node, to_status, reason, http_request=None):
    """Raise a `platform.city_status` request to launch or pause a city. Returns the Outcome."""
    reason = (reason or '').strip()
    if len(reason) < 5:
        raise PlatformError('A reason of at least 5 characters is required.',
                            code='REASON_REQUIRED', field='reason')
    if node.kind != CITY:
        raise PlatformError(f'{node.code} is a {node.kind}, not a city.', code='NOT_A_CITY',
                            field='city', status=400)
    to_status = {LIVE: ACTIVE, 'active': ACTIVE, 'paused': PAUSED}.get(to_status)
    if to_status is None:
        raise PlatformError('status must be "live" or "paused".', code='INVALID_STATUS',
                            field='status')
    if to_status == ACTIVE:
        if not granted(actor, 'platform.settings', node)[0]:
            raise PlatformError('Launching a city needs platform.settings here.',
                                code='SCOPE_NOT_HELD', field='city', status=403)
        if node.status == ACTIVE:
            raise PlatformError(f'{node.code} is already live.', code='ALREADY_LIVE',
                                field='status', status=409)
        blocking = [i for i in readiness.city_readiness(node) if i['blocking']]
        if blocking:
            raise PlatformError(
                f'{node.code} is not ready to go live: '
                f'{", ".join(i["key"] for i in blocking)}.',
                code='CITY_NOT_READY', field='city', status=409)
    else:
        if not (granted(actor, 'platform.settings', node)[0]
                or granted(actor, 'platform.city_pause', node)[0]):
            raise PlatformError('Pausing a city needs platform.city_pause here.',
                                code='SCOPE_NOT_HELD', field='city', status=403)
        if node.status != ACTIVE:
            raise PlatformError(f'{node.code} is {label_of(node.status)}, not live.',
                                code='NOT_LIVE', field='status', status=409)
    country = _anchor_country(node)
    payload = {'kind': 'status', 'city': node.code, 'to_status': to_status,
               'from_status': node.status}
    return approvals.require_approval_or_execute(
        PLATFORM_CITY_STATUS, actor=actor, country=country, scope_node=node,
        target_type='geonode', target_id=node.code, payload=payload,
        idempotency_key=f'platform_city:{node.code}:{to_status}:{_digest(node.status, reason)}',
        reason=reason, http_request=http_request)


def propose_add_node(actor, *, kind, code, name, parent_code, reason, http_request=None):
    """Raise a `platform.city_status` request to ADD a country, state or city as a draft."""
    reason = (reason or '').strip()
    if len(reason) < 5:
        raise PlatformError('A reason of at least 5 characters is required.',
                            code='REASON_REQUIRED', field='reason')
    code = str(code or '').strip().upper()
    name = str(name or '').strip()
    if kind not in (COUNTRY, STATE, CITY):
        raise PlatformError('kind must be country, state or city.', code='INVALID_KIND',
                            field='kind')
    if not CODE_RE.match(code):
        raise PlatformError('A code is the ISO country code, then "-" and up to 8 letters or '
                            'digits per level (for example TX or TX-AUS under US).',
                            code='INVALID_CODE', field='code')
    if not name or len(name) > NAME_MAX:
        raise PlatformError(f'A name of 1 to {NAME_MAX} characters is required.',
                            code='INVALID_NAME', field='name')
    if GeoNode.objects.filter(code=code).exists():
        raise PlatformError(f'{code} already exists.', code='ALREADY_EXISTS', field='code',
                            status=409)
    if kind == COUNTRY:
        if find_country(code) is None:
            raise PlatformError(
                f'{code} is not in the country registry (base/countries.py): its calling code, '
                f'currency and tax mode are code, so a new country needs a registry entry first.',
                code='UNKNOWN_COUNTRY', field='code', status=400)
        parent = GeoNode.objects.filter(kind=GLOBAL).first()
        if parent is None:
            raise PlatformError('There is no Global node.', code='NO_GLOBAL', field='code',
                                status=409)
    else:
        parent = GeoNode.objects.filter(code=str(parent_code or '').upper()).first()
        expected = COUNTRY if kind == STATE else STATE
        if parent is None or parent.kind != expected:
            raise PlatformError(f'A {kind} sits under a {expected}; "{parent_code}" is not one.',
                                code='INVALID_PARENT', field='parent_code')
        if not code.startswith(parent.code + '-'):
            raise PlatformError(f'The code of a {kind} under {parent.code} starts with '
                                f'"{parent.code}-".', code='INVALID_CODE', field='code')
    if not granted(actor, 'platform.settings', parent)[0]:
        raise PlatformError('Adding a place needs platform.settings over its parent.',
                            code='SCOPE_NOT_HELD', field='parent_code', status=403)
    country = code.split('-')[0] if kind != COUNTRY else code
    if find_country(country) is None:
        raise PlatformError(f'{country} is not in the country registry.', code='UNKNOWN_COUNTRY',
                            field='code')
    payload = {'kind': 'add_node', 'node': {'kind': kind, 'code': code, 'name': name,
                                            'parent_code': parent.code}}
    return approvals.require_approval_or_execute(
        PLATFORM_CITY_STATUS, actor=actor, country=country, scope_node=parent,
        target_type='geonode', target_id=code, payload=payload,
        idempotency_key=f'platform_city_add:{code}', reason=reason, http_request=http_request)
