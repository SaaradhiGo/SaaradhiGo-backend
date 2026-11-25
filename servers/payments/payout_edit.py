"""Payout settings, the Edit pattern (owner brief 25 Nov): what a change does before it is sent.

`display(current, values)` is the before/after in words, the next payout date and what it would
pay (drivers whose balance qualifies today, their total, and the payout fee), today against the
change. It rides on the approval request so the checker sees what the editor saw.
"""

import copy
from decimal import Decimal

from base.money import format_money, from_minor

WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
LABELS = {
    'fee_percent': 'Payout fee %', 'minimum_withdrawal_minor': 'Minimum payout',
    'stuck_payout_hours': 'Flag a stuck payout after (hours)', 'payout_schedule': 'Schedule',
    'payout_weekday': 'Payout day', 'payout_interval_days': 'Every (days)', 'payout_anchor_date': 'Counted from',
    'payout_time_local': 'Payout time (local)', 'reward_hold_days': 'Reward hold (days)',
    'recovery_max_percent': 'Most recovered from one payout %', 'payout_hold_max_days': 'Longest payout hold (days)',
    'instant_cashout_enabled': 'Instant cash-out',
}
SCHEDULE_WORDS = {'on_request': 'On request', 'weekly': 'Weekly', 'every_n_days': 'Every N days'}


def _words(key, value, currency):
    if value is None or value == '':
        return 'not set'
    if key == 'minimum_withdrawal_minor':
        return format_money(from_minor(int(value), currency), currency)
    if key == 'payout_schedule':
        return SCHEDULE_WORDS.get(value, str(value))
    if key == 'payout_weekday':
        return WEEKDAYS[int(value)] if 0 <= int(value) <= 6 else str(value)
    if isinstance(value, bool):
        return 'On' if value else 'Off'
    if isinstance(value, Decimal):
        return f'{value.normalize():f}'
    return str(value)


def changes(before, after, currency):
    return [{'label': LABELS.get(k, k.replace('_', ' ').capitalize()), 'from': _words(k, before.get(k), currency),
             'to': _words(k, after.get(k), currency)}
            for k in after if k in LABELS and before.get(k) != after.get(k)]


def _would_pay(config):
    """Drivers whose balance qualifies today for this config, their total and the payout fee."""
    from django.db.models import Count, Sum

    from servers.rider.models import Wallet

    floor = max(1, int(config.minimum_withdrawal_minor or 0))
    agg = Wallet.objects.filter(scope=Wallet.SCOPE_DRIVER, currency=config.currency,
                                balance_minor__gte=floor).aggregate(n=Count('id'), total=Sum('balance_minor'))
    total = int(agg['total'] or 0)
    fee = int((Decimal(total) * Decimal(str(config.fee_percent or 0)) / 100).quantize(Decimal('1')))
    return int(agg['n'] or 0), total, fee


def display(current, values):
    from .payout_config_write import values_of
    from .payout_scope import next_payout_at

    cur = current.currency
    proposed = copy.copy(current)
    for k, v in values.items():
        setattr(proposed, k, v)
    rows = []
    for label, cfg in (('Today', current), ('After the change', proposed)):
        when = next_payout_at(cfg)
        n, total, fee = _would_pay(cfg)
        m = lambda v: format_money(from_minor(v, cur), cur)  # noqa: E731
        rows.append([label, when.date().isoformat() if when else 'On request', str(n), m(total), m(fee)])
    return {'changes': changes(values_of(current), values, cur),
            'impact': {'title': 'The next payout', 'columns': ['', 'Next payout date', 'Drivers who qualify today',
                                                                'They would be paid', 'Payout fee'],
                       'rows': rows, 'note': 'From balances as they stand now; a run pays what is due on the day.'},
            'effective': 'Applies from the next payout run after approval.'}
