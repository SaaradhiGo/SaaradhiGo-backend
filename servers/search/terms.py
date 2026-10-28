"""What did the operator type? Decided once, before any query runs.

An operator arrives with a trip number from a complaint, a phone number
from a call, a plate from a report, or a name. The box is one box, so the
term has to be classified, and the classification decides which indexed
lookups run. Nothing here touches the database.

THE RULES, AND WHY THEY ARE NARROW
----------------------------------
Every match below is either an exact match, a left-anchored prefix on an
indexed expression, or `icontains` on `full_name` alone. There is no
free-text search over addresses, notes or documents: those fields hold
personal data and the console has no business grepping them.

  digits only        trip id (exact), phone (national prefix, or the whole
                     number if ten or more digits), plate prefix
  phone punctuation  phone only: `+91 98765 43210`, `(512) 555-0101`
  letters + digits   a plate: `TS09AB1234`, `ts-09-ab-1234`, `QAUS0001`
  letters            a name: `icontains` on `full_name`, capped by `limit`
  anything else      unsupported; the response says so and finds nothing

A phone number is normalised with `base.countries.normalize_phone` so the
E.164 stored on the account is matched exactly through the unique index,
and ALSO by its last ten digits so a number stored under a country code the
operator did not type is still found (`RIGHT(phone_number, 10)` is indexed
for exactly this). Fewer than four digits is not a phone partial: it would
match a tenth of every account and is never what anybody meant.
"""

import re
from dataclasses import dataclass, field

from base.countries import normalize_phone
from base.db_indexes import PHONE_TAIL_DIGITS
from servers.driver.plates import normalise_plate

MIN_TERM = 2
MAX_TERM = 64

#: A trip id is a BigAutoField, but nobody has twelve-digit trip ids and a
#: longer digit string is a phone number somebody typed without a plus.
MAX_TRIP_ID_DIGITS = 12
MIN_PHONE_PARTIAL = 4
MAX_PLATE = 12
MAX_PLATE_PREFIX_DIGITS = 8

_DIGITS_ONLY = re.compile(r'^\d+$')
_PHONE_SHAPE = re.compile(r'^\+?[\d\s().\-]+$')
_PLATE_SHAPE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\s\-]*$')
# Letters in any script, plus the punctuation that appears inside names.
_NAME_SHAPE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[\s.'\-])*$", re.UNICODE)


class TermError(ValueError):
    """The term cannot be searched at all (too short, too long)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Term:
    """The parsed term. Every field is None/empty when it does not apply."""

    raw: str
    kind: str                       # number | phone | plate | name | unsupported
    trip_id: int | None = None
    phone_e164: str | None = None   # exact match on the stored E.164
    phone_tail: str | None = None   # exact match on RIGHT(phone, 10)
    phone_prefix: str | None = None  # prefix match on RIGHT(phone, 10)
    plate: str | None = None        # exact and prefix on the normalised plate
    name: str | None = None         # icontains on full_name
    digits: str = field(default='')

    @property
    def searches_phones(self):
        return bool(self.phone_e164 or self.phone_tail or self.phone_prefix)

    @property
    def length(self):
        return len(self.raw)


def parse_term(raw):
    """Classify `raw`. Raises `TermError` only when nothing could be searched."""
    text = (raw or '').strip()
    # A single digit is a trip id (the first trips in any database have
    # one); a single letter is nothing anybody can be looking for.
    if not text or (len(text) < MIN_TERM and not _DIGITS_ONLY.match(text)):
        raise TermError('SEARCH_TERM_TOO_SHORT', f'Type at least {MIN_TERM} characters.')
    if len(text) > MAX_TERM:
        raise TermError('SEARCH_TERM_TOO_LONG', f'Search terms are at most {MAX_TERM} characters.')

    if _DIGITS_ONLY.match(text):
        return _number(text)

    if _PHONE_SHAPE.match(text):
        return _phone(text)

    if _PLATE_SHAPE.match(text):
        plate = normalise_plate(text)
        has_letter = any(ch.isalpha() for ch in plate)
        has_digit = any(ch.isdigit() for ch in plate)
        if has_letter and has_digit and MIN_TERM <= len(plate) <= MAX_PLATE:
            return Term(raw=text, kind='plate', plate=plate)

    if _NAME_SHAPE.match(text):
        return Term(raw=text, kind='name', name=text)

    return Term(raw=text, kind='unsupported')


def _phone_parts(digits, had_plus, raw):
    """`(e164, tail)` for a number with at least ten digits."""
    e164 = normalize_phone(raw if had_plus else digits)
    tail = digits[-PHONE_TAIL_DIGITS:] if len(digits) >= PHONE_TAIL_DIGITS else None
    if e164 is not None:
        # The tail of the NORMALISED number, so `+1 (512) 555-0101` and
        # `5125550101` agree on what the national part is.
        tail = e164[-PHONE_TAIL_DIGITS:]
    return e164, tail


def _number(text):
    digits = text
    trip_id = int(digits) if len(digits) <= MAX_TRIP_ID_DIGITS else None
    e164 = tail = prefix = None
    if len(digits) >= PHONE_TAIL_DIGITS:
        e164, tail = _phone_parts(digits, had_plus=False, raw=text)
    elif len(digits) >= MIN_PHONE_PARTIAL:
        prefix = digits
    # US plates may begin with digits (`7ABC123`), so a short digit string
    # is also tried as a plate prefix. Indexed, and capped by `limit`.
    plate = digits if len(digits) <= MAX_PLATE_PREFIX_DIGITS else None
    return Term(
        raw=text, kind='number', trip_id=trip_id, digits=digits,
        phone_e164=e164, phone_tail=tail, phone_prefix=prefix, plate=plate,
    )


def _phone(text):
    digits = re.sub(r'\D', '', text)
    if not digits:
        return Term(raw=text, kind='unsupported')
    had_plus = text.lstrip().startswith('+')
    if len(digits) >= PHONE_TAIL_DIGITS:
        e164, tail = _phone_parts(digits, had_plus=had_plus, raw=text)
        return Term(raw=text, kind='phone', digits=digits, phone_e164=e164, phone_tail=tail)
    if len(digits) >= MIN_PHONE_PARTIAL:
        return Term(raw=text, kind='phone', digits=digits, phone_prefix=digits)
    return Term(raw=text, kind='unsupported', digits=digits)
