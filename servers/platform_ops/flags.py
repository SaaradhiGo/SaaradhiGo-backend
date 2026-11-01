"""Feature flags: resolution, the helper code calls, and the audited change path.

RESOLUTION: city override, then country override, then the default.

    from servers.platform_ops.flags import flag_enabled
    if flag_enabled('some.flag', city=trip_city_node): ...

`flag_enabled` never raises for an unknown key: it answers the `default` argument (False). A flag
that does not exist is off, so code can ship a read before the flag row is created.

NO FLAG IS SEEDED. Nothing in the code base reads a flag yet, and a flag with no reader is a
switch connected to nothing. The mechanism (this module, the console tab, the app-config
endpoint) is complete and tested with a test-only flag; the first real flag is created by the
change that adds its reader.

CHANGING A FLAG
---------------
Capability `platform.settings` over the place changed (a city override needs it at that city, a
country override at that country, the default at Global). A flag marked `money_affecting` or
`safety` changes only through a `platform.flag_change` request (a checker); any other flag takes
effect immediately and the audit row says so. Every change appends a `FeatureFlagVersion`.
"""

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from servers.admin_access.bridge import granted
from servers.admin_access.models import CITY, COUNTRY, GLOBAL, GeoNode
from servers.admin_audit.services import record_admin_action
from servers.approvals import service as approvals
from servers.approvals.actions import PLATFORM_FLAG_CHANGE

from .errors import PlatformError
from .models import (
    OWNER_AREAS, FeatureFlag, FeatureFlagOverride, FeatureFlagVersion, validate_flag_key,
)

AUDIT_CREATED = 'feature_flag_created'
AUDIT_CHANGED = 'feature_flag_changed'
TARGET = 'feature_flag'


# ---------------------------------------------------------------------------
# Reading and resolving
# ---------------------------------------------------------------------------

def _node(value):
    if value is None or isinstance(value, GeoNode):
        return value
    if isinstance(value, int):
        return GeoNode.objects.filter(pk=value).first()
    return GeoNode.objects.filter(code=str(value)).first()


def _chain(node):
    """The city node, then its country node (either may be absent)."""
    city = node if node is not None and node.kind == CITY else None
    country = node.country if node is not None else None
    return city, country


def resolve(flag, node=None):
    """`(enabled, source)` for `flag` at `node` (a city or country node, or None for global)."""
    city, country = _chain(node)
    overrides = {o.node_id: o.enabled for o in flag.overrides.all()}
    if city is not None and city.pk in overrides:
        return overrides[city.pk], 'city'
    if country is not None and country.pk in overrides:
        return overrides[country.pk], 'country'
    return flag.default_enabled, 'default'


def flag_enabled(key, city=None, country=None, default=False):
    """Is flag `key` on for this city/country? Unknown or archived key: `default`."""
    flag = (FeatureFlag.objects.filter(key=key, is_archived=False)
            .prefetch_related('overrides').first())
    if flag is None:
        return default
    node = _node(city)
    if node is None and country is not None:
        c = country.upper() if isinstance(country, str) else country
        node = GeoNode.objects.filter(kind=COUNTRY, code=c).first() if isinstance(c, str) else c
    return resolve(flag, node)[0]


def resolved_for(node):
    """`{key: bool}` for every live flag at `node`: what app-config returns."""
    flags = FeatureFlag.objects.filter(is_archived=False).prefetch_related('overrides')
    return {f.key: resolve(f, node)[0] for f in flags}


def snapshot(flag):
    return {'default': flag.default_enabled,
            'overrides': {o.node.code: o.enabled for o in flag.overrides.select_related('node')}}


def flag_dict(flag, user=None):
    snap = snapshot(flag)
    return {
        'key': flag.key, 'description': flag.description, 'owner_area': flag.owner_area,
        'default_enabled': flag.default_enabled, 'overrides': snap['overrides'],
        'money_affecting': flag.money_affecting, 'safety': flag.safety,
        'needs_checker': flag.needs_checker, 'is_archived': flag.is_archived,
        'version': flag.version, 'updated_by': flag.updated_by_label or None,
        'updated_at': flag.updated_at.isoformat() if flag.updated_at else None,
    }


def history(flag, limit=50):
    return [{'version': v.version, 'snapshot': v.snapshot, 'changed_by': v.changed_by_label,
             'reason': v.reason, 'approval_request_id': v.approval_request_id,
             'created_at': v.created_at.isoformat()}
            for v in flag.versions.order_by('-version')[:limit]]


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------

def create_flag(actor, *, key, description, default_enabled=False, owner_area='platform',
                money_affecting=False, safety=False, reason='', http_request=None):
    key = str(key or '').strip()
    try:
        validate_flag_key(key)
    except Exception as exc:  # noqa: BLE001 - ValidationError carries the message
        raise PlatformError(' '.join(getattr(exc, 'messages', [str(exc)])), code='INVALID_KEY',
                            field='key')
    description = str(description or '').strip()
    if not description or len(description) > 300:
        raise PlatformError('A description of 1 to 300 characters is required.',
                            code='INVALID_DESCRIPTION', field='description')
    if owner_area not in dict(OWNER_AREAS):
        raise PlatformError('Unknown owner area.', code='INVALID_OWNER', field='owner_area')
    if FeatureFlag.objects.filter(key=key).exists():
        raise PlatformError(f'Flag {key} already exists.', code='ALREADY_EXISTS', field='key',
                            status=409)
    root = GeoNode.objects.filter(kind=GLOBAL).first()
    if not granted(actor, 'platform.settings', root)[0]:
        raise PlatformError('Creating a flag needs platform.settings.', code='SCOPE_NOT_HELD',
                            field='key', status=403)
    with transaction.atomic():
        flag = FeatureFlag.objects.create(
            key=key, description=description, default_enabled=bool(default_enabled),
            owner_area=owner_area, money_affecting=bool(money_affecting), safety=bool(safety),
            updated_by_id=actor.pk, updated_by_label=_label(actor))
        FeatureFlagVersion.objects.create(
            flag=flag, version=1, snapshot=snapshot(flag), changed_by_id=actor.pk,
            changed_by_label=_label(actor), reason=reason or 'created')
        record_admin_action(
            http_request, action=AUDIT_CREATED, target_type=TARGET, target_id=key,
            before={}, after={**flag_dict(flag), 'overrides': {}}, reason=reason or 'created')
    return flag


def _label(user):
    return (getattr(user, 'full_name', '') or getattr(user, 'email', '') or f'staff #{user.pk}')[:255]


# ---------------------------------------------------------------------------
# Changing
# ---------------------------------------------------------------------------

def _check_changes(actor, flag, default_enabled, overrides):
    """Validate a requested change and the caller's scope for each part. Returns normalised
    `(default_enabled|None, {code: bool|None})`. A None value removes an override."""
    root = GeoNode.objects.filter(kind=GLOBAL).first()
    if default_enabled is not None:
        if not isinstance(default_enabled, bool):
            raise PlatformError('default_enabled must be true or false.', code='INVALID_VALUE',
                                field='default_enabled')
        if not granted(actor, 'platform.settings', root)[0]:
            raise PlatformError('Changing the default needs platform.settings at Global.',
                                code='SCOPE_NOT_HELD', field='default_enabled', status=403)
    clean = {}
    for code, value in (overrides or {}).items():
        node = GeoNode.objects.filter(code=str(code)).first()
        if node is None or node.kind not in (COUNTRY, CITY):
            raise PlatformError(f'{code} is not a country or a city.', code='INVALID_SCOPE',
                                field='overrides')
        if value is not None and not isinstance(value, bool):
            raise PlatformError('An override is true, false or null (remove).',
                                code='INVALID_VALUE', field='overrides')
        if not granted(actor, 'platform.settings', node)[0]:
            raise PlatformError(f'Changing {code} needs platform.settings there.',
                                code='SCOPE_NOT_HELD', field='overrides', status=403)
        clean[node.code] = value
    return default_enabled, clean


def _effective(flag, default_enabled, overrides):
    """What the flag's snapshot WOULD be: used to skip a no-op change."""
    snap = snapshot(flag)
    if default_enabled is not None:
        snap['default'] = default_enabled
    for code, value in overrides.items():
        if value is None:
            snap['overrides'].pop(code, None)
        else:
            snap['overrides'][code] = value
    return snap


def apply_change(flag_key, default_enabled, overrides, *, actor, reason, request=None,
                 approval=None):
    """Write the change under a lock, append a version, audit it. Returns the flag."""
    with transaction.atomic():
        flag = FeatureFlag.objects.select_for_update().get(key=flag_key)
        before = snapshot(flag)
        after = _effective(flag, default_enabled, overrides)
        if before == after:
            return flag, False
        if default_enabled is not None:
            flag.default_enabled = default_enabled
        for code, value in overrides.items():
            node = GeoNode.objects.get(code=code)
            if value is None:
                FeatureFlagOverride.objects.filter(flag=flag, node=node).delete()
            else:
                FeatureFlagOverride.objects.update_or_create(
                    flag=flag, node=node, defaults={'enabled': value})
        flag.version += 1
        flag.updated_by_id = getattr(actor, 'pk', None)
        flag.updated_by_label = _label(actor) if actor is not None else ''
        flag.save()
        FeatureFlagVersion.objects.create(
            flag=flag, version=flag.version, snapshot=after,
            changed_by_id=getattr(actor, 'pk', None), changed_by_label=flag.updated_by_label,
            reason=reason, approval_request_id=getattr(approval, 'pk', None),
            created_at=timezone.now())
        record_admin_action(
            request, action=AUDIT_CHANGED, target_type=TARGET, target_id=flag.key,
            before=before, after={**after, 'version': flag.version,
                                  'approval_request_id': getattr(approval, 'pk', None)},
            reason=reason)
    return flag, True


def propose_change(actor, key, *, default_enabled=None, overrides=None, reason='',
                   http_request=None):
    """Change a flag now, or raise a `platform.flag_change` request. Returns `(flag, outcome)`:
    `outcome` is None when the change was applied directly (or was a no-op)."""
    flag = FeatureFlag.objects.filter(key=key).first()
    if flag is None:
        raise PlatformError(f'No flag {key!r}.', code='NOT_FOUND', field='key', status=404)
    if flag.is_archived:
        raise PlatformError('This flag is archived.', code='ARCHIVED', field='key', status=409)
    reason = (reason or '').strip()
    if len(reason) < 5:
        raise PlatformError('A reason of at least 5 characters is required.',
                            code='REASON_REQUIRED', field='reason')
    default_enabled, overrides = _check_changes(actor, flag, default_enabled, overrides)
    if default_enabled is None and not overrides:
        raise PlatformError('Nothing to change.', code='NOTHING_TO_CHANGE', field='general')
    if _effective(flag, default_enabled, overrides) == snapshot(flag):
        return flag, None
    if not flag.needs_checker:
        flag, _changed = apply_change(key, default_enabled, overrides, actor=actor, reason=reason,
                                      request=http_request)
        return flag, None
    country, scope_node = approvals_anchor(overrides)
    payload = {'key': key, 'default_enabled': default_enabled, 'overrides': overrides,
               'base_version': flag.version}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    outcome = approvals.require_approval_or_execute(
        PLATFORM_FLAG_CHANGE, actor=actor, country=country, scope_node=scope_node,
        target_type=TARGET, target_id=key, payload=payload,
        idempotency_key=f'platform_flag:{key}:{digest}', reason=reason, http_request=http_request)
    return flag, outcome


def approvals_anchor(overrides):
    """`(country_code, node)` a request is raised under: the one country the change touches, else
    Global with the first country that has a policy (policy rows are per country)."""
    from . import markets

    nodes = [GeoNode.objects.get(code=c) for c in overrides]
    codes = {n.country.code for n in nodes if n.country is not None}
    if len(codes) == 1 and len(nodes) == len(overrides) and overrides:
        code = next(iter(codes))
        return code, (nodes[0] if len(nodes) == 1 else nodes[0].country)
    return markets.policy_country(), GeoNode.objects.filter(kind=GLOBAL).first()
