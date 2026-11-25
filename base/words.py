"""Counts that agree with their noun (owner 25 Nov: "1 Approvals waiting" read wrong)."""

import re


def plural(n, one, many=None):
    """'1 day', '0 days', '1,500 trips'; `many` for irregular nouns ('entry', 'entries')."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = None
    shown = f'{v:,}' if v is not None and v == n else str(n)
    return f"{shown} {one if v == 1 else (many or one + 's')}"


_ONE = re.compile(r'(?<![\d.,])\b1 (day|hour|minute|second|week|month|trip|ride|driver|rider|payout|'
                  r'document|request|ticket|step)s\b')


def agree(text):
    """English text from a template: '1 days' becomes '1 day' once the values are in."""
    return _ONE.sub(r'1 \1', text or '')
