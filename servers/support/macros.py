"""Macro placeholders: a fixed allow-list, filled in on the server only.

A macro body may contain `{first_name}`, `{ticket_id}` and `{trip_id}` and
nothing else. Anything else in braces (an unknown name, an attribute walk
like `{first_name.__class__}`, a format spec, a stray brace) is refused when
the macro is saved, so a stored macro can always be rendered. Rendering is
a plain substitution, never `str.format`, so a body can reach no object.
"""

import re
import string

ALLOWED_PLACEHOLDERS = ('first_name', 'ticket_id', 'trip_id')

_TOKEN = re.compile(r'\{(' + '|'.join(ALLOWED_PLACEHOLDERS) + r')\}')


class PlaceholderError(ValueError):
    def __init__(self, bad):
        self.bad = sorted(set(bad))
        super().__init__(
            'Unknown placeholder(s): ' + ', '.join(self.bad)
            + '. Allowed: ' + ', '.join('{%s}' % p for p in ALLOWED_PLACEHOLDERS))


def validate_body(body):
    """Raise PlaceholderError unless every brace in `body` is an allowed token."""
    bad = []
    try:
        for _literal, field, spec, conversion in string.Formatter().parse(body or ''):
            if field is None:
                continue
            if field not in ALLOWED_PLACEHOLDERS or spec or conversion:
                bad.append('{%s%s%s}' % (field, '!' + conversion if conversion else '',
                                         ':' + spec if spec else ''))
    except ValueError:
        raise PlaceholderError(['unbalanced brace'])
    if bad:
        raise PlaceholderError(bad)


def values_for(ticket):
    """The values a ticket supplies. Missing ones are '' (and reported)."""
    name = (getattr(ticket.user_id, 'full_name', '') or '').strip()
    return {
        'first_name': name.split()[0] if name and not any(c.isdigit() for c in name) else 'there',
        'ticket_id': str(ticket.id),
        'trip_id': str(ticket.trip_id_id) if ticket.trip_id_id else '',
    }


def render(body, ticket):
    """(text, missing): `body` with placeholders filled from `ticket`."""
    values = values_for(ticket)
    missing = sorted({m.group(1) for m in _TOKEN.finditer(body or '') if not values[m.group(1)]})
    return _TOKEN.sub(lambda m: values[m.group(1)], body or ''), missing
