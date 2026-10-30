"""Reading and versioning `SafetyCountryConfig`.

NO FALLBACK, EVER
-----------------
`config_for(country)` returns the country's active row or None. A caller that
gets None has NO timer and NO detection for that country and must say so
("not configured"); it must never reach for another country's row or a
constant. There is deliberately no `DEFAULT_*` in this module.

VERSIONED EDITS
---------------
`change_config` closes the active row and opens the next version in one
transaction, writes one audit row with before and after, and is idempotent: a
request that changes nothing writes nothing.
"""

import re

from django.db import transaction
from django.utils import timezone

from base.countries import COUNTRIES
from servers.admin_audit.services import record_admin_action

from .models import SafetyCountryConfig

TARGET = 'safety_config'
AUDIT_ACTION = 'safety_config_changed'

#: Upper bounds. Generous; they exist to catch a typo (a day of minutes typed
#: into the metres field), not to set policy.
LIMITS = {
    'sos_ack_minutes': (1, 120),
    'incident_assign_minutes': (1, 1440),
    'long_stop_minutes': (1, 720),
    'long_stop_radius_m': (5, 5000),
    'stop_exempt_radius_m': (0, 5000),
    'long_stop_incident_minutes': (1, 1440),
    'eta_overrun_percent': (1, 1000),
    'eta_overrun_minutes': (1, 1440),
    'eta_overrun_incident_percent': (1, 2000),
    'eta_overrun_incident_minutes': (1, 2880),
    'route_deviation_meters': (10, 50000),
    'route_deviation_minutes': (1, 720),
}

_NUMBER = re.compile(r'^[0-9+*#() -]{2,20}$')


class ConfigError(Exception):
    def __init__(self, code, message, field):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


def config_for(country):
    """The active config of `country`, or None. Never a fallback."""
    code = (country or '').upper().strip()
    if not code:
        return None
    return SafetyCountryConfig.objects.filter(country=code, is_active=True).first()


def active_configs():
    return {c.country: c for c in SafetyCountryConfig.objects.filter(is_active=True)}


def registry_numbers(country):
    """The emergency numbers the country registry holds, as config rows."""
    entry = COUNTRIES.get((country or '').upper())
    if entry is None:
        return []
    return [{'label': label, 'number': number} for label, number in entry.emergency_numbers]


def clean_numbers(raw):
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > 8:
        raise ConfigError('INVALID_NUMBERS', 'emergency_numbers must be a list of at most 8.',
                          'emergency_numbers')
    out = []
    for row in raw:
        if not isinstance(row, dict):
            raise ConfigError('INVALID_NUMBERS', 'Each number is {label, number}.',
                              'emergency_numbers')
        label = str(row.get('label') or '').strip()[:40]
        number = str(row.get('number') or '').strip()
        if not label or not _NUMBER.match(number):
            raise ConfigError('INVALID_NUMBERS',
                              'Each number needs a label and a dialable number.',
                              'emergency_numbers')
        out.append({'label': label, 'number': number})
    return out


def clean_values(data, current):
    """The new value for every numeric field: provided ones validated, the
    rest carried over. An explicit null clears (turns that rule off)."""
    values = current.values() if current is not None else {n: None for n in LIMITS}
    for name, (low, high) in LIMITS.items():
        if name not in data:
            continue
        raw = data[name]
        if raw is None or raw == '':
            values[name] = None
            continue
        if isinstance(raw, bool):
            raise ConfigError('INVALID_VALUE', f'{name} must be a whole number.', name)
        try:
            number = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigError('INVALID_VALUE', f'{name} must be a whole number.', name)
        if not low <= number <= high:
            raise ConfigError('INVALID_VALUE', f'{name} must be between {low} and {high}.', name)
        values[name] = number
    return values


def _incident_lines_make_sense(values):
    stop, stop_inc = values['long_stop_minutes'], values['long_stop_incident_minutes']
    if stop and stop_inc and stop_inc < stop:
        raise ConfigError('INVALID_VALUE',
                          'long_stop_incident_minutes cannot be below long_stop_minutes.',
                          'long_stop_incident_minutes')
    pct, pct_inc = values['eta_overrun_percent'], values['eta_overrun_incident_percent']
    if pct and pct_inc and pct_inc < pct:
        raise ConfigError('INVALID_VALUE',
                          'eta_overrun_incident_percent cannot be below eta_overrun_percent.',
                          'eta_overrun_incident_percent')
    mins, mins_inc = values['eta_overrun_minutes'], values['eta_overrun_incident_minutes']
    if mins and mins_inc and mins_inc < mins:
        raise ConfigError('INVALID_VALUE',
                          'eta_overrun_incident_minutes cannot be below eta_overrun_minutes.',
                          'eta_overrun_incident_minutes')


def change_config(user, country, data, *, reason='', request=None):
    """Write the next version of `country`'s config. Returns `(row, changed)`.

    `data` may carry any of `LIMITS` and `emergency_numbers`; fields it omits
    keep their current value.
    """
    code = (country or '').upper().strip()
    if code not in COUNTRIES:
        raise ConfigError('UNKNOWN_COUNTRY', f'{country!r} is not a supported country.', 'country')
    reason = (reason or '').strip()
    if not reason:
        raise ConfigError('REASON_REQUIRED', 'reason is required.', 'reason')

    with transaction.atomic():
        current = (SafetyCountryConfig.objects.select_for_update()
                   .filter(country=code, is_active=True).first())
        values = clean_values(data, current)
        _incident_lines_make_sense(values)
        numbers = clean_numbers(data.get('emergency_numbers')) if 'emergency_numbers' in data else None
        if numbers is None:
            numbers = list(current.emergency_numbers) if current is not None else []

        if current is not None and values == current.values() and numbers == list(current.emergency_numbers):
            return current, False

        before = current.as_dict() if current is not None else {}
        now = timezone.now()
        version = 1
        if current is not None:
            current.is_active, current.valid_to = False, now
            current.save(update_fields=['is_active', 'valid_to'])
            version = current.version + 1
        row = SafetyCountryConfig(country=code, version=version, is_active=True,
                                  valid_from=now, reason=reason[:2000],
                                  emergency_numbers=numbers, **values)
        row.set_created_by(user)
        row.save()
        record_admin_action(
            request, action=AUDIT_ACTION, target_type=TARGET, target_id=code,
            before=before, after=row.as_dict(), reason=reason)
    return row, True


def history(country, limit=20):
    return list(SafetyCountryConfig.objects.filter(country=country.upper())
                .order_by('-version')[:limit])
