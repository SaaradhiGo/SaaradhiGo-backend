"""An audit entry in words (owner, 22 Nov): who, what changed, the target by name, the place, why.

    "Changed Hyderabad payout minimum from ₹100 to ₹200"

Built from the stored row only: the action, the before/after the writer recorded, the target and
the place. Nothing is looked up that the row does not name, and every value passes the same scrub
as the detail view, so a phone number in a before/after never reaches the sentence.
"""

import re

#: Verbs for actions whose name alone reads badly.
ACTION_WORDS = {
    'ops_api_login': 'Signed in to the console',
    'ops_password_changed': 'Changed their password',
    'ops_mfa_enrolled': 'Set up their authenticator',
    'ops_recovery_codes_issued': 'Was given new recovery codes',
    'ops_recovery_code_used': 'Signed in with a recovery code',
    'operator_alert_cleared': 'Alert closed by the system',
    'operator_alert_acknowledged': 'Took an alert',
    'city_status_changed': 'Changed a city\'s launch status',
    'kyc_approved': 'Approved a driver\'s documents',
    'chain_anchor': 'Audit chain started',
    'chain_forks_sealed': 'Sealed audit-chain forks from before the lock fix',
    'zone_deactivated': 'Turned off a service zone',
    'test_account_flagged': 'Marked an account as a test account',
    'wallet_currency_relabelled': 'Corrected a wallet\'s currency',
}

#: Page views and session events: hidden by default on the audit page (owner, 22 Nov).
NOISE_PREFIXES = ('ops_api_login', 'ops_token', 'ops_logout', 'session_', 'page_view', 'console_page',
                  'live_ops_session', 'live_ops_ticket')
NOISE_SUFFIXES = ('_viewed', '_page_view', '_listed', '_searched', '_exported_view', '_session_opened',
                  '_session_closed', '_ticket_issued')

#: Field names in words.
FIELD_WORDS = {
    'minimum_withdrawal_minor': 'payout minimum', 'fee_percent': 'payout fee', 'percent': 'commission',
    'status': 'status', 'is_active': 'active', 'payout_schedule': 'payout schedule',
    'payout_weekday': 'payout day', 'payout_time_local': 'payout time', 'per_trip_fee_minor': 'fee per trip',
    'daily_cap_minor': 'daily cap', 'base_fare': 'base fare', 'per_km_fare': 'fare per km',
    'per_min_fare': 'fare per minute', 'min_fare': 'minimum fare', 'approved': 'approval',
    'acknowledged': 'taken', 'is_test_account': 'test account', 'currency': 'currency',
    'warn_value': 'warning level', 'critical_value': 'critical level', 'instant_cashout_enabled': 'Withdraw now',
    # 24 Nov (owner): no field names on the page.
    'estimated_fare_minor': 'quoted fare', 'final_fare_minor': 'fare', 'net_whole': 'driver earning',
    'net_shown': 'driver earning', 'commission_whole': 'commission', 'correction_minor': 'balance correction',
    'wallets_moved': 'driver balances corrected', 'fee_minor': 'platform fee', 'trips': 'trips',
    'fee_days': 'fee days',
}
#: Amounts kept in minor units (paise, cents) whose name does not end in _minor.
MONEY_FIELDS = {'net_whole', 'net_shown', 'commission_whole', 'amount', 'fare'}
#: The market's currency when the row does not carry one.
COUNTRY_CURRENCY = {'IN': 'INR', 'US': 'USD'}
SKIP_FIELDS = {'version', 'id', 'updated_at', 'created_at', 'reason', 'idempotency_key', 'country', 'scope',
               'city', 'city_code', 'currency', 'kind', 'cleared_by', 'source_type', 'source_id', 'settlements'}
SYMBOL = {'INR': '₹', 'USD': '$'}


def is_noise(action):
    a = (action or '').lower()
    return a.startswith(NOISE_PREFIXES) or a.endswith(NOISE_SUFFIXES)


def _money(minor, currency):
    try:
        value = int(minor) / 100
    except (TypeError, ValueError):
        return str(minor)
    text = f'{value:,.0f}' if value == int(value) else f'{value:,.2f}'
    return f'{SYMBOL.get(currency, "")}{text}' if currency in SYMBOL else f'{text} {currency or ""}'.strip()


def _value(field, value, currency):
    if value is None or value == '':
        return 'nothing'
    if isinstance(value, bool):
        return 'on' if value else 'off'
    if field.endswith('_minor') or field in MONEY_FIELDS:
        return _money(value, currency)
    if field.endswith('percent') or field == 'percent':
        return f'{value}%'
    if isinstance(value, (dict, list)):
        return 'a new value'
    text = str(value)
    return text if len(text) <= 40 else text[:39] + '…'


def _field(field):
    return FIELD_WORDS.get(field) or field.replace('_minor', '').replace('_', ' ')


def _flat(blob):
    """One level of nesting lifted ({'fare': {'final_fare_minor': 1}} -> {'final_fare_minor': 1})."""
    if not isinstance(blob, dict):
        return {}
    out = {}
    for key, value in blob.items():
        if isinstance(value, dict) and key not in SKIP_FIELDS and all(isinstance(k, str) for k in value):
            for inner, v in value.items():
                out.setdefault(inner, v)
        else:
            out.setdefault(key, value)
    return out


def changes(before, after, place=None):
    """[(field words, from, to)] for the fields the entry changed, at most four."""
    before, after = _flat(before), _flat(after)
    currency = (after.get('currency') or before.get('currency')
                or COUNTRY_CURRENCY.get(str(place or '').split('-')[0]))
    out = []
    for key, new in after.items():
        if key in SKIP_FIELDS or key.startswith('_'):
            continue
        old = before.get(key)
        if key in before and old == new:
            continue
        if key not in before and not before:
            continue  # a creation: the action already says it
        out.append((_field(key), _value(key, old, currency), _value(key, new, currency)))
        if len(out) == 4:
            break
    return out


#: A bulk action's target ("all") in words.
ALL_WORDS = {'user_preference': 'Every account', 'trip': 'All trips', 'user': 'Every account'}


def target_words(entry, names):
    t, tid = entry.target_type or '', entry.target_id or ''
    if t == 'geonode' and tid:
        return names.get(tid, tid)
    ref = names.get(('ref', t, str(tid)))
    if ref:
        return f"{t.replace('_', ' ').capitalize()} {ref}" if t not in ('trip', 'driver', 'rider') else {
            'trip': 'Trip', 'driver': 'Driver', 'rider': 'Rider'}[t] + f' {ref}'
    prefix = {'driver': 'Driver D-', 'rider': 'Rider R-', 'trip': 'Trip T-', 'user': 'Account #',
              'withdrawal': 'Payout W-', 'operator_alert': 'Alert #', 'service_zone': 'Zone #',
              'wallet': 'Wallet #', 'operator': 'Operator #'}.get(t)
    if tid == 'all':
        return ALL_WORDS.get(t, f"All {t.replace('_', ' ')}s")
    if prefix and tid:
        return f'{prefix}{tid}'
    if not t:
        return ''
    return f"{t.replace('_', ' ')} {tid}".strip()


def summary(entry, names):
    """One sentence for the list."""
    action = entry.action or ''
    code = _place(entry, names)
    place = names.get(code or '', '')
    lines = changes(entry.before, entry.after, code)
    if action in ROUNDING_ACTIONS:
        return _rounding(entry, lines, names)
    if action in TEMPLATES:
        before = entry.before if isinstance(entry.before, dict) else {}
        after = entry.after if isinstance(entry.after, dict) else {}
        return TEMPLATES[action](before, after)
    if lines:
        where = f'{place} ' if place else ''
        field, old, new = lines[0]
        text = f'Set {where}{field} to {new}' if old == 'nothing' else f'Changed {where}{field} from {old} to {new}'
        if len(lines) > 1:
            text += f' (and {len(lines) - 1} more)'
        return text
    words = ACTION_WORDS.get(action) or re.sub(r'[._]+', ' ', action).strip().capitalize()
    target = target_words(entry, names)
    return f'{words}{" · " + target if target else ""}{" · " + place if place and place not in target else ""}'


def _n(count, word):
    count = int(count or 0)
    return f'{count} {word}{"s" * (count != 1)}'


#: Bulk actions whose before/after hold counts, said as a sentence.
TEMPLATES = {
    'themes_reset_to_light': lambda b, a: f"Reset {_n(a.get('reset'), 'account')} to the light theme",
}

ROUNDING_ACTIONS = {'inr_trip_money_rounded', 'inr_platform_fee_rounded', 'inr_money_rounded_to_rupees'}


def _rounding(entry, lines, names):
    """The 23 Nov whole-rupee correction in words: what was rounded and what the driver now sees."""
    after = entry.after if isinstance(entry.after, dict) else {}
    before = entry.before if isinstance(entry.before, dict) else {}
    if entry.action == 'inr_money_rounded_to_rupees':
        trips, moved = before.get('trips') or 0, after.get('wallets_moved') or 0
        return (f'Rounded India money to whole rupees: {trips} trip{"s" * (trips != 1)}, '
                f'{moved} driver balance{"s" * (moved != 1)} corrected')
    head = ('Rounded the fare to whole rupees' if entry.action == 'inr_trip_money_rounded'
            else 'Rounded the platform fee to whole rupees')
    parts = [f'{field} {old} to {new}' if old != 'nothing' else f'{field} {new}'
             for field, old, new in lines if field != 'balance correction']
    corr = after.get('correction_minor')
    if corr:
        parts.append(f'balance corrected by {"+" if corr > 0 else "−"}{_money(abs(corr), "INR")}')
    return f'{head}: {", ".join(parts[:3])}' if parts else head


def _place(entry, names=None):
    from .viewer import display_place, place_of

    return display_place(entry, names) if names is not None else place_of(entry)
