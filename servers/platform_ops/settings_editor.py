"""The key/value Platform Settings editor (`pricing.PlatformSettings`).

WHAT THE KEYS DO TODAY: NOTHING, AND THE PAGE SAYS SO
-----------------------------------------------------
`servers.pricing.services.get_platform_setting` is the one reader, and it has NO caller: the
commission key it once served was retired by module 8 (commission comes from `CommissionRule`).
`READERS` below records what each key controls; it is empty, and `tests/test_platform_settings.py`
fails if a call site of the reader appears without an entry here, so the page can never claim a
control the code does not have. Every row therefore shows "nothing reads this key yet". The editor
is a complete, audited, type-checked mechanism for the first reader that arrives.

RULES
-----
* Type-aware: `integer`, `decimal`, `boolean`, `string` (max 2000 characters) and `json` are
  validated before anything is stored; the stored text is the canonical form.
* Capability `platform.settings` (Super Admin). Every write is audited with before and after.
* A SENSITIVE key (flag on the row, forced on when the name contains SECRET, TOKEN, PASSWORD, KEY
  or CREDENTIAL) changes only through a `platform.setting_change` request (a checker). Its value is
  never in a list response and never in an audit row: a short SHA-256 fingerprint stands in.
* A DEPRECATED key (`DEPRECATED`) is read-only and points at the page that replaced it.
"""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction

from servers.admin_access.bridge import granted
from servers.admin_access.models import GLOBAL, GeoNode
from servers.admin_audit.services import record_admin_action
from servers.approvals import service as approvals
from servers.approvals.actions import PLATFORM_SETTING_CHANGE
from servers.pricing.models import PlatformSettings

from .errors import PlatformError

KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{2,127}$')
SENSITIVE_WORDS = ('SECRET', 'TOKEN', 'PASSWORD', 'KEY', 'CREDENTIAL')
MASK = '********'
STRING_MAX = 2000
AUDIT = 'platform_setting_changed'
TARGET = 'platform_setting'

#: key -> (what replaced it, console path). Read-only.
DEPRECATED = {
    'PLATFORM_COMMISSION_PERCENT': (
        'Commission is now a versioned, maker-checker rule per country, city and vehicle class.',
        '/pricing'),
}

#: key -> what it controls (code paths that read it). Empty on purpose: see the module docstring.
READERS = {}

TYPES = ('decimal', 'integer', 'string', 'boolean', 'json')


def is_sensitive_name(key):
    return any(word in key for word in SENSITIVE_WORDS)


def fingerprint(value):
    return 'sha256:' + hashlib.sha256(str(value).encode()).hexdigest()[:12]


def canonical(setting_type, raw):
    """The stored text for `raw` as `setting_type`, or a PlatformError saying what is wrong."""
    if setting_type not in TYPES:
        raise PlatformError(f'type must be one of {", ".join(TYPES)}.', code='INVALID_TYPE',
                            field='setting_type')
    if isinstance(raw, bool):
        text = 'true' if raw else 'false'
    elif isinstance(raw, (dict, list)):
        text = json.dumps(raw, sort_keys=True)
    else:
        text = str(raw if raw is not None else '').strip()
    try:
        if setting_type == 'integer':
            if isinstance(raw, float) or '.' in text:
                raise ValueError
            return str(int(text))
        if setting_type == 'decimal':
            value = Decimal(text)
            if not value.is_finite():
                raise InvalidOperation
            return str(value)
        if setting_type == 'boolean':
            if text.lower() not in ('true', 'false', '1', '0', 'yes', 'no'):
                raise ValueError
            return 'true' if text.lower() in ('true', '1', 'yes') else 'false'
        if setting_type == 'json':
            return json.dumps(json.loads(text), sort_keys=True)
    except (ValueError, InvalidOperation, json.JSONDecodeError):
        raise PlatformError(f'That is not a valid {setting_type}.', code='INVALID_VALUE',
                            field='value')
    if len(text) > STRING_MAX:
        raise PlatformError(f'A string setting is at most {STRING_MAX} characters.',
                            code='INVALID_VALUE', field='value')
    return text


def row_dict(row):
    deprecated = DEPRECATED.get(row.key)
    sensitive = bool(row.is_sensitive)
    return {
        'key': row.key, 'setting_type': row.setting_type,
        'value': MASK if sensitive else row.value, 'masked': sensitive,
        'description': row.description, 'is_sensitive': sensitive,
        'deprecated': bool(deprecated),
        'deprecated_note': deprecated[0] if deprecated else None,
        'deprecated_href': deprecated[1] if deprecated else None,
        'editable': not deprecated,
        'controls': list(READERS.get(row.key, [])),
        'updated_by': (row.updated_by.full_name or row.updated_by.email) if row.updated_by_id else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


def list_rows():
    rows = PlatformSettings.objects.select_related('updated_by').order_by('key')
    return [row_dict(r) for r in rows]


def _audit_view(row_or_none, *, key, value=None, setting_type=None, sensitive=False):
    if row_or_none is None and value is None:
        return {}
    shown = fingerprint(value if value is not None else row_or_none.value) if sensitive else (
        value if value is not None else row_or_none.value)
    return {'key': key, 'value': shown, 'setting_type': setting_type or row_or_none.setting_type,
            'is_sensitive': sensitive}


def apply_change(key, value, setting_type, description, is_sensitive, *, actor, reason,
                 request=None, approval=None):
    with transaction.atomic():
        row = PlatformSettings.objects.select_for_update().filter(key=key).first()
        was_sensitive = bool(row and row.is_sensitive)
        before = _audit_view(row, key=key, sensitive=was_sensitive) if row else {}
        created = row is None
        if row is None:
            row = PlatformSettings(key=key)
        if (not created and row.value == value and row.setting_type == setting_type
                and row.description == description and row.is_sensitive == is_sensitive):
            return row, False
        row.value, row.setting_type, row.description = value, setting_type, description
        row.is_sensitive = is_sensitive
        row.updated_by = actor if actor is not None and getattr(actor, 'pk', None) else None
        row.save()
        record_admin_action(
            request, action=AUDIT, target_type=TARGET, target_id=key, before=before,
            after={**_audit_view(row, key=key, sensitive=is_sensitive),
                   'created': created, 'approval_request_id': getattr(approval, 'pk', None)},
            reason=reason)
    return row, True


def propose_change(actor, *, key, value, setting_type=None, description=None, is_sensitive=None,
                   reason='', http_request=None):
    """Change (or create) a setting now, or raise a `platform.setting_change` request.

    Returns `(row_or_None, outcome_or_None)`."""
    key = str(key or '').strip()
    if not KEY_RE.match(key):
        raise PlatformError('A key is UPPER_CASE letters, digits and "_" (3 to 128 characters).',
                            code='INVALID_KEY', field='key')
    reason = (reason or '').strip()
    if len(reason) < 5:
        raise PlatformError('A reason of at least 5 characters is required.',
                            code='REASON_REQUIRED', field='reason')
    if key in DEPRECATED:
        note, href = DEPRECATED[key]
        raise PlatformError(f'{key} is deprecated and read-only. {note} See {href}.',
                            code='DEPRECATED_KEY', field='key', status=409)
    root = GeoNode.objects.filter(kind=GLOBAL).first()
    if not granted(actor, 'platform.settings', root)[0]:
        raise PlatformError('Changing a setting needs platform.settings.',
                            code='SCOPE_NOT_HELD', field='key', status=403)
    row = PlatformSettings.objects.filter(key=key).first()
    s_type = setting_type or (row.setting_type if row else 'string')
    text = canonical(s_type, value)
    desc = (description if description is not None else (row.description if row else '')) or ''
    if len(desc) > 500:
        raise PlatformError('A description is at most 500 characters.', code='INVALID_DESCRIPTION',
                            field='description')
    if row is None and not desc.strip():
        raise PlatformError('A description is required for a new setting.',
                            code='INVALID_DESCRIPTION', field='description')
    sensitive = bool(is_sensitive) if is_sensitive is not None else bool(row and row.is_sensitive)
    sensitive = sensitive or is_sensitive_name(key)
    if row is not None and row.is_sensitive and not sensitive:
        sensitive = True  # a sensitive key can never be quietly turned into a visible one
    needs_checker = sensitive or bool(row and row.is_sensitive)
    if not needs_checker:
        row, _changed = apply_change(key, text, s_type, desc, False, actor=actor, reason=reason,
                                     request=http_request)
        return row, None
    from . import markets

    payload = {'key': key, 'value': text, 'setting_type': s_type, 'description': desc,
               'is_sensitive': True, 'value_fingerprint': fingerprint(text)}
    outcome = approvals.require_approval_or_execute(
        PLATFORM_SETTING_CHANGE, actor=actor, country=markets.policy_country(), scope_node=root,
        target_type=TARGET, target_id=key, payload=payload,
        idempotency_key=f'platform_setting:{key}:{fingerprint(text)}', reason=reason,
        http_request=http_request)
    return row, outcome
