"""What runs when a checker approves a platform request. Four executors, one per action.

Each re-reads the world at approval time and refuses (`ExecutionRefused`, the request becomes
`failed` and nothing moved) if it changed since the maker asked:

* `platform.city_status`: adds a country/state/city as a draft, or launches/pauses a city through
  `readiness.set_city_status`, which RE-CHECKS the readiness checklist: a launch that was green
  when asked and is red now is refused with `CITY_NOT_READY`.
* `platform.flag_change`: applies the flag change if the flag is still at the version the maker
  saw (`STALE_REQUEST` otherwise: a checker must not approve a diff against a state that moved).
* `platform.app_version`: writes the policy slot.
* `platform.setting_change`: writes the sensitive setting.
"""

from servers.admin_access import readiness
from servers.admin_access.models import GeoNode
from servers.admin_audit.context import current_context
from servers.approvals import registry
from servers.approvals.actions import (
    PLATFORM_APP_VERSION, PLATFORM_CITY_STATUS, PLATFORM_FLAG_CHANGE, PLATFORM_SETTING_CHANGE,
)

from . import flags, settings_editor, versions
from .errors import PlatformError


def _checker():
    ctx = current_context()
    return ctx.resolve_actor() if ctx is not None else None


def _maker_note(request):
    return f'{request.request_reason} (asked by {request.maker_label}, approved by checker)'


@registry.register(PLATFORM_CITY_STATUS)
def execute_city_status(request):
    p = request.payload or {}
    if p.get('kind') == 'add_node':
        spec = p.get('node') or {}
        parent = GeoNode.objects.filter(code=spec.get('parent_code')).first()
        if parent is None:
            raise registry.ExecutionRefused('PARENT_MISSING',
                                            f"{spec.get('parent_code')} no longer exists.")
        if GeoNode.objects.filter(code=spec.get('code')).exists():
            raise registry.ExecutionRefused('ALREADY_EXISTS', f"{spec.get('code')} exists.")
        from servers.admin_access.models import DRAFT

        node = GeoNode.objects.create(kind=spec['kind'], code=spec['code'], name=spec['name'],
                                      parent=parent, status=DRAFT)
        return {'created': node.code, 'status': node.status}
    node = GeoNode.objects.filter(code=p.get('city')).first()
    if node is None:
        raise registry.ExecutionRefused('NOT_FOUND', f"{p.get('city')} no longer exists.")
    try:
        node, changed = readiness.set_city_status(
            node, p.get('to_status'), _checker(), _maker_note(request))
    except readiness.CityNotReady as exc:
        raise registry.ExecutionRefused(
            'CITY_NOT_READY',
            f'{node.code} is not ready to go live now: '
            f'{", ".join(i["key"] for i in exc.items)}. Fix them and ask again.')
    except (ValueError, readiness.NotACity) as exc:
        raise registry.ExecutionRefused('INVALID_TRANSITION', str(exc))
    return {'city': node.code, 'status': node.status, 'changed': changed}


@registry.register(PLATFORM_FLAG_CHANGE)
def execute_flag_change(request):
    p = request.payload or {}
    flag = flags.FeatureFlag.objects.filter(key=p.get('key')).first()
    if flag is None or flag.is_archived:
        raise registry.ExecutionRefused('NOT_FOUND', f"Flag {p.get('key')} is gone.")
    if flag.version != p.get('base_version'):
        raise registry.ExecutionRefused(
            'STALE_REQUEST',
            f'{flag.key} changed (now version {flag.version}, the request was made against '
            f"{p.get('base_version')}). Ask again against the current state.")
    try:
        flag, changed = flags.apply_change(
            flag.key, p.get('default_enabled'), p.get('overrides') or {}, actor=_checker(),
            reason=_maker_note(request), approval=request)
    except GeoNode.DoesNotExist:
        raise registry.ExecutionRefused('NOT_FOUND', 'A place in the request no longer exists.')
    return {'key': flag.key, 'version': flag.version, 'changed': changed}


@registry.register(PLATFORM_APP_VERSION)
def execute_app_version(request):
    p = request.payload or {}
    try:
        url = versions._validate(p['app'], p['platform'], p['minimum_version'],
                                 p['latest_version'], p.get('store_url'), p.get('message'))
    except PlatformError as exc:
        raise registry.ExecutionRefused(exc.code, exc.message)
    row, changed = versions.apply_policy(
        p['app'], p['platform'], p['minimum_version'], p['latest_version'], url,
        p.get('message') or '', actor=_checker(), reason=_maker_note(request), approval=request)
    return {'app': row.app, 'platform': row.platform, 'minimum_version': row.minimum_version,
            'version': row.version, 'changed': changed}


@registry.register(PLATFORM_SETTING_CHANGE)
def execute_setting_change(request):
    p = request.payload or {}
    key = p.get('key')
    if key in settings_editor.DEPRECATED:
        raise registry.ExecutionRefused('DEPRECATED_KEY', f'{key} is deprecated and read-only.')
    try:
        value = settings_editor.canonical(p.get('setting_type'), p.get('value'))
    except PlatformError as exc:
        raise registry.ExecutionRefused(exc.code, exc.message)
    row, changed = settings_editor.apply_change(
        key, value, p.get('setting_type'), p.get('description') or '',
        bool(p.get('is_sensitive')), actor=_checker(), reason=_maker_note(request),
        approval=request)
    return {'key': row.key, 'changed': changed}
