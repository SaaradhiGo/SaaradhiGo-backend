"""Money with a currency attached, and aggregates that refuse to lie.

THE DEFECT THIS CLOSES
----------------------
`Trip.estimated_fare`, `WalletTransaction.amount`, `Receipt.total_fare` and a dozen
siblings were bare `DecimalField`s. Currency existed only as a function parameter
defaulting to `'INR'`:

    def create_payout(..., currency: str = "INR", ...)

So the database could not tell a 450-rupee trip from a 450-dollar one. Two consequences,
and the second is worse than the first:

1. **Every cross-market aggregate is a meaningless number.** There are about twenty
   `Sum('amount')` and `Sum('final_fare')` calls in this codebase, several of them in the
   operator console's revenue views. Adding one US trip makes all of them wrong, and
   nothing says so.

2. **A misconfigured rate card is undetectable.** A US zone seeded with Indian magnitudes
   charges $30 base instead of $3. The schema has no opinion, the tests pass, and the
   first person to notice is a rider.

THE APPROACH
------------
The zone is the source of truth — a `ServiceZone` has exactly one currency, because a
geographic market does. Every money-bearing row is stamped with its currency **at write
time** from the trip's zone, and never inferred later.

Denormalised on purpose. Joining Trip → zone → currency for every row of a revenue report
is slower and, far worse, breaks when a zone is re-parented or a historical rate card
changes: the currency a trip was charged in is a fact about that trip, not a lookup
against today's configuration. Same reasoning as `Trip.zone_code` and
`rate_card_version`, which are already stored rather than joined.

`sum_by_currency` exists because the honest answer to "what is total revenue" across two
markets is two numbers. A function that returns one number there is a function that
lies, so this module does not provide one.
"""

import logging
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal

from base.countries import COUNTRIES, DEFAULT_COUNTRY_CODE, get_country

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')
CENTS = Decimal('0.01')

# ISO 4217 codes this platform can price in. Deliberately a short allow-list
# rather than "any three letters": an unknown currency on a money row is a
# configuration error that must fail at the boundary, not become data.
SUPPORTED_CURRENCIES = frozenset({'INR', 'USD'})

DEFAULT_CURRENCY = get_country(DEFAULT_COUNTRY_CODE).currency

# Minor units per currency. Both of ours are 2, but JPY is 0 and KWD is 3, and
# quantizing a yen amount to 2 places invents precision that does not exist.
_MINOR_UNITS = {'INR': 2, 'USD': 2}

_SYMBOLS = {'INR': '₹', 'USD': '$'}


def is_supported(currency):
    return str(currency or '').upper().strip() in SUPPORTED_CURRENCIES


def normalize_currency(currency, fallback=None):
    """Upper-case and validate, falling back rather than raising.

    Used on the write path, where refusing to store a trip because a zone has a
    typo'd currency would lose the trip. The fallback is logged so the
    misconfiguration is visible.
    """
    code = str(currency or '').upper().strip()
    if code in SUPPORTED_CURRENCIES:
        return code
    chosen = fallback or DEFAULT_CURRENCY
    if currency:
        logger.error(
            'unsupported currency %r on a money record; falling back to %s. '
            'This is a zone misconfiguration, not a transient error.',
            currency, chosen,
        )
    return chosen


def minor_units(currency):
    return _MINOR_UNITS.get(normalize_currency(currency), 2)


def quantum(currency):
    """The smallest representable amount, e.g. Decimal('0.01') for USD/INR."""
    places = minor_units(currency)
    return Decimal(1).scaleb(-places) if places else Decimal(1)


def quantize(amount, currency=DEFAULT_CURRENCY):
    """Round to this currency's minor unit, half-up.

    Half-up, not banker's rounding: it is what every fare table in both markets
    assumes, and the existing pricing code already rounds this way. Changing the
    mode here would move prices.
    """
    if amount is None:
        return None
    return Decimal(str(amount)).quantize(quantum(currency), rounding=ROUND_HALF_UP)


def format_money(amount, currency=DEFAULT_CURRENCY, with_symbol=True):
    """Render for a human. Never used for arithmetic.

    Replaces f-strings like `f"Insufficient balance. Available: ₹{available:.2f}"`,
    which would show a dollar balance with a rupee sign.
    """
    code = normalize_currency(currency)
    if amount is None:
        amount = ZERO
    value = quantize(amount, code)
    places = minor_units(code)
    text = f'{value:,.{places}f}'
    if not with_symbol:
        return text
    symbol = _SYMBOLS.get(code, code + ' ')
    country = next((c for c in COUNTRIES.values() if c.currency == code), None)
    if country is not None and country.symbol_position == 'suffix':
        return f'{text} {symbol}'
    return f'{symbol}{text}'


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

def charged_fare():
    """The amount a completed trip actually charged, as a DB expression.

    WHY THIS EXISTS
    ---------------
    `Trip.final_fare` is written in exactly one place -- `ride/promos.py`, when
    a promo code is applied. Every ordinary trip completes with it NULL,
    because the rider is quoted up front and pays the quote.

    Every per-trip consumer in this codebase already knows that and reads
    `trip.final_fare or trip.estimated_fare`. The AGGREGATES did not: they
    summed `Sum('final_fare')`, so the operator console's GMV and revenue
    counted promo trips and nothing else. Trip #81 settled ₹135.68 in cash and
    the dashboard reported ₹0.00.

    This is the same rule as an expression, so the aggregates and the
    per-trip views can no longer disagree about what a trip charged.

    It does NOT invent money: a trip with neither fare contributes zero, and
    a promo trip contributes its discounted `final_fare`, which is what was
    charged.
    """
    from django.db.models import DecimalField, Value
    from django.db.models.functions import Coalesce

    return Coalesce(
        'final_fare',
        'estimated_fare',
        Value(0, output_field=DecimalField(max_digits=12, decimal_places=2)),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def sum_by_currency(queryset, field, currency_field='currency'):
    """Total `field`, grouped by currency. Returns an OrderedDict.

    The only honest shape for a cross-market total. There is deliberately no
    `total_revenue(queryset)` in this module: with two currencies in one table,
    a single number is either wrong or silently restricted to one market, and
    both are worse than two numbers.

        >>> sum_by_currency(Trip.objects.completed(), 'final_fare')
        OrderedDict([('INR', Decimal('45200.00')), ('USD', Decimal('1310.50'))])
    """
    from django.db.models import Sum

    # `field` may be a column name OR an expression -- `charged_fare()` is
    # passed here by the revenue aggregates, because the column they used to
    # sum is NULL on every trip without a promo.
    rows = (queryset
            .values(currency_field)
            .annotate(_total=Sum(field))
            .order_by(currency_field))
    out = OrderedDict()
    for row in rows:
        code = normalize_currency(row.get(currency_field))
        total = row.get('_total') or ZERO
        out[code] = quantize(out.get(code, ZERO) + total, code)
    return out


def single_currency_or_none(queryset, currency_field='currency'):
    """The one currency in this queryset, or None if it spans several.

    For a view that legitimately shows one number -- a single driver's earnings,
    one trip's settlement -- this is how it confirms it is entitled to.
    """
    codes = set(queryset.values_list(currency_field, flat=True).distinct())
    codes.discard(None)
    codes.discard('')
    if len(codes) == 1:
        return normalize_currency(next(iter(codes)))
    return None


def format_totals(totals):
    """Render a `sum_by_currency` result for an operator, e.g. '₹45,200.00 + $1,310.50'.

    Two currencies shown as two amounts rather than added. An operator reading a
    revenue figure needs to see that it spans markets.
    """
    if not totals:
        return format_money(ZERO)
    return ' + '.join(format_money(v, k) for k, v in totals.items())


# ---------------------------------------------------------------------------
# Resolution: where a record's currency comes from
# ---------------------------------------------------------------------------

def currency_for_zone(zone):
    """A zone's currency, falling back to its country's, then to the default."""
    if zone is None:
        return DEFAULT_CURRENCY
    explicit = getattr(zone, 'currency', None)
    if explicit:
        return normalize_currency(explicit)
    return normalize_currency(get_country(getattr(zone, 'country', None)).currency)


def currency_for_trip(trip):
    """A trip's currency.

    Prefers the value stamped on the trip, because that is the fact about what
    the rider was charged. Falls back to the zone only for rows written before
    the column existed.
    """
    if trip is None:
        return DEFAULT_CURRENCY
    stamped = getattr(trip, 'currency', None)
    if stamped:
        return normalize_currency(stamped)
    return currency_for_zone(getattr(trip, 'zone', None))
