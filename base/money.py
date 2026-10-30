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

INTEGER MINOR UNITS (owner decision, 2026-10-26)
------------------------------------------------
Every stored amount is a `BigIntegerField` of minor units -- paise, cents -- beside the
row's ISO 4217 `currency`. It replaced `Decimal(10,2)` before launch, because pre-launch
is the cheapest moment, because the gateways speak minor units, and because an integer
ledger has no rounding ambiguity: 13568 paise is 13568 paise in every language, driver
and database.

Rates and multipliers -- per km, per minute, surge, commission %, tax % -- stay
`Decimal`. Arithmetic runs in Decimal and is rounded ONCE, half-up, by `to_minor`, at
the moment it becomes a stored amount. Nothing else in the codebase rounds money.

The digits per currency come from `CURRENCIES`, never from a literal 100: JPY has none
and KWD has three, and `amount * 100` is how a yen fare becomes a hundred times too big.

A model keeps its old attribute name as a `MinorUnitsAmount` property, so `trip.estimated_fare`
still reads as `Decimal('135.68')`. The column behind it is `estimated_fare_minor`, and
the ORM only knows the column: `filter(estimated_fare=...)`, `Sum('estimated_fare')` and
`update_fields=['estimated_fare']` all raise. That is deliberate -- a query that used
to see rupees and would now see paise must fail, not be silently 100x wrong.
"""

import logging
from collections import OrderedDict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from base.countries import COUNTRIES, DEFAULT_COUNTRY_CODE, get_country

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')

# The one rounding rule for money. Used by `to_minor` and nowhere else.
ROUNDING = ROUND_HALF_UP


@dataclass(frozen=True)
class Currency:
    code: str       # ISO 4217
    exponent: int   # ISO 4217 minor-unit digits: 2 for INR/USD, 0 for JPY, 3 for KWD
    symbol: str


# The currency table. Deliberately a short allow-list rather than "any three
# letters": an unknown currency on a money row is a configuration error that
# must fail at the boundary, not become data. Adding a market means adding a row
# here with its ISO 4217 exponent -- and nothing else, because every conversion
# reads the exponent from this table.
CURRENCIES = {
    'INR': Currency('INR', 2, '₹'),
    'USD': Currency('USD', 2, '$'),
}

SUPPORTED_CURRENCIES = frozenset(CURRENCIES)

DEFAULT_CURRENCY = get_country(DEFAULT_COUNTRY_CODE).currency


class MoneyTypeError(TypeError):
    """A float, a bool or something non-numeric reached a money value."""


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


def exponent(currency):
    """ISO 4217 minor-unit digits for `currency`, from `CURRENCIES`.

    Strict, unlike `normalize_currency`: converting an amount in a currency we
    do not know would guess its scale, and a guessed scale is a 100x error.
    """
    code = str(currency or '').upper().strip()
    try:
        return CURRENCIES[code].exponent
    except KeyError:
        raise ValueError(f'unsupported currency {currency!r}: not in CURRENCIES') from None


# Kept for existing callers; `exponent` is the name the minor-unit code uses.
minor_units = exponent


def quantum(currency):
    """The smallest representable amount, e.g. Decimal('0.01') for USD/INR."""
    return Decimal(1).scaleb(-exponent(currency))


def as_decimal(amount):
    """`amount` as a Decimal, refusing anything that is not already exact.

    Accepts Decimal, int and numeric strings. Refuses float -- `0.1 + 0.2` is
    `0.30000000000000004`, and a float that reaches money has already lost
    the paisa it was carrying -- and bool, which is an int to Python and a bug
    everywhere else. Refuses NaN and infinity, which Decimal would otherwise
    carry silently into a ledger.
    """
    if isinstance(amount, bool) or isinstance(amount, float):
        raise MoneyTypeError(
            f'{type(amount).__name__} {amount!r} reached money; pass a Decimal, '
            f'an int or a numeric string')
    if isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, (int, str)):
        try:
            value = Decimal(amount.strip() if isinstance(amount, str) else amount)
        except ArithmeticError:
            raise MoneyTypeError(f'{amount!r} is not a number') from None
    else:
        raise MoneyTypeError(f'{type(amount).__name__} {amount!r} reached money')
    if not value.is_finite():
        raise MoneyTypeError(f'{amount!r} is not a finite amount')
    return value


def parse_input(value):
    """An amount from a REQUEST, as a Decimal. The only float this module takes.

    A JSON body's `{"amount": 45.5}` reaches Python as the float 45.5, and its
    `str()` is the shortest text that round-trips -- '45.5', exactly what the
    client sent. So a float is accepted here, at the parsing boundary, and
    nowhere else: inside the platform a float in money is a bug, and
    `as_decimal` refuses it.
    """
    if isinstance(value, float):
        value = str(value)
    return as_decimal(value)


def to_minor(amount, currency):
    """Major units (Decimal) -> integer minor units. THE rounding point.

    Half-up, once. This is the only function in the codebase that rounds a
    money amount; everything that produces a stored amount comes through here,
    with its arithmetic still unrounded.

        >>> to_minor(Decimal('135.675'), 'INR')
        13568
    """
    if amount is None:
        return None
    scaled = as_decimal(amount).scaleb(exponent(currency))
    return int(scaled.quantize(Decimal(1), rounding=ROUNDING))


def from_minor(minor, currency):
    """Integer minor units -> Decimal major units. Exact; never rounds.

    The result carries exactly the currency's digits, so `str()` of it is the
    shape the API has always returned: 13568 INR -> Decimal('135.68'), 12300
    -> Decimal('123.00').
    """
    if minor is None:
        return None
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise MoneyTypeError(
            f'minor units must be an int, got {type(minor).__name__} {minor!r}')
    return Decimal(minor).scaleb(-exponent(currency))


def quantize(amount, currency=DEFAULT_CURRENCY):
    """Round a major-unit amount to this currency's minor unit, via `to_minor`.

    Half-up, through the same single rounding point, so a value quantized here
    and a value stored are always the same value.
    """
    if amount is None:
        return None
    return from_minor(to_minor(amount, currency), currency)


def to_major_string(minor, currency):
    """'135.68' -- the API's existing string shape for a money value."""
    value = from_minor(minor, currency)
    return None if value is None else str(value)


def format_money(amount, currency=DEFAULT_CURRENCY, with_symbol=True):
    """Render for a human. Never used for arithmetic.

    Replaces f-strings like `f"Insufficient balance. Available: ₹{available:.2f}"`,
    which would show a dollar balance with a rupee sign.
    """
    code = normalize_currency(currency)
    if amount is None:
        amount = ZERO
    value = quantize(amount, code)
    places = exponent(code)
    text = f'{value:,.{places}f}'
    if not with_symbol:
        return text
    symbol = CURRENCIES[code].symbol
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

    In MINOR units, like every money column: sum it with `sum_by_currency`,
    which converts per currency.
    """
    from django.db.models import BigIntegerField, Value
    from django.db.models.functions import Coalesce

    return Coalesce(
        'final_fare_minor',
        'estimated_fare_minor',
        Value(0, output_field=BigIntegerField()),
        output_field=BigIntegerField(),
    )


def sum_by_currency(queryset, field, currency_field='currency'):
    """Total a minor-unit `field`, grouped by currency. Returns an OrderedDict
    of Decimal major units.

    The only honest shape for a cross-market total. There is deliberately no
    `total_revenue(queryset)` in this module: with two currencies in one table,
    a single number is either wrong or silently restricted to one market, and
    both are worse than two numbers.

        >>> sum_by_currency(Trip.objects.completed(), charged_fare())
        OrderedDict([('INR', Decimal('45200.00')), ('USD', Decimal('1310.50'))])

    The sum is taken in integers, so it is exact; the conversion back to major
    units happens once per currency, here.
    """
    from django.db.models import Sum

    # `field` may be a column name OR an expression -- `charged_fare()` is
    # passed here by the revenue aggregates, because the column they used to
    # sum is NULL on every trip without a promo.
    if isinstance(field, str) and not field.endswith('_minor'):
        raise ValueError(
            f'sum_by_currency({field!r}): money is stored in minor units; '
            f'sum the {field}_minor column')
    rows = (queryset
            .values(currency_field)
            .annotate(_total=Sum(field))
            .order_by(currency_field))
    totals = OrderedDict()
    for row in rows:
        code = normalize_currency(row.get(currency_field))
        totals[code] = totals.get(code, 0) + int(row.get('_total') or 0)
    return OrderedDict((code, from_minor(minor, code)) for code, minor in totals.items())


def sum_minor(queryset, field):
    """Integer total of a minor-unit column, for a queryset already known to be
    in ONE currency. 0 for an empty queryset."""
    from django.db.models import Sum

    return int(queryset.aggregate(_t=Sum(field))['_t'] or 0)


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


# ---------------------------------------------------------------------------
# Models: an integer column, read and written as a Decimal
# ---------------------------------------------------------------------------

def currency_of(instance, currency_field):
    """The currency an amount on `instance` is in, resolving a blank one.

    A money column no longer carries a `default='INR'`: the currency comes from
    the row's own trip, zone or wallet (`base.currency_stamp`), which the model
    constructor has already been given by the time a property keyword such as
    `amount=` is applied (Django sets fields first, properties after). So a
    blank currency is derived HERE, once, and written back onto the instance;
    the stamp at save then finds nothing to change. Only when nothing at all
    can be derived does the platform default apply, and `CurrencyStampedModel`
    logs that at ERROR when the row is saved.
    """
    currency = getattr(instance, currency_field)
    if currency:
        return currency
    derive = getattr(instance, '_derive_currency', None)
    found = derive() if callable(derive) else None
    chosen = normalize_currency(found) if found else DEFAULT_CURRENCY
    if found:
        setattr(instance, currency_field, chosen)
    return chosen


class MinorUnitsAmount(property):
    """`trip.estimated_fare` as a Decimal, stored as `trip.estimated_fare_minor`.

    Declared on a model beside the integer column it fronts:

        estimated_fare_minor = models.BigIntegerField(null=True, blank=True)
        estimated_fare = MinorUnitsAmount('estimated_fare_minor')

    Reading converts exactly with the row's own currency. Writing goes through
    `to_minor`, so an unrounded Decimal assigned here is rounded once, half-up,
    exactly where it becomes stored -- and a float is refused.

    A `property` subclass on purpose: Django lets a model constructor set any
    `property` by keyword, so `Trip.objects.create(estimated_fare=q['total_fare'])`
    keeps working. Query APIs do not see it, which is the point -- see the
    module docstring.
    """

    def __init__(self, minor_field, currency_field='currency'):
        self.minor_field = minor_field
        self.currency_field = currency_field

        def fget(instance):
            return from_minor(getattr(instance, minor_field),
                              currency_of(instance, currency_field))

        def fset(instance, value):
            setattr(instance, minor_field,
                    to_minor(value, currency_of(instance, currency_field)))

        super().__init__(fget, fset, doc=f'Decimal view of `{minor_field}`.')

    def __set_name__(self, owner, name):
        # Recorded so the currency stamp can check that a change of currency
        # does not change the scale of an amount already written.
        owner._money_minor_fields = (
            tuple(getattr(owner, '_money_minor_fields', ())) + (self.minor_field,))


def assert_currency_change_keeps_scale(instance, before, after):
    """Refuse a currency change that would reinterpret stored minor units.

    An amount is converted to minor units with the row's currency at the moment
    it is assigned. The currency stamp can change that currency afterwards, at
    save. While both are 2-digit currencies that changes nothing; between INR
    and JPY it would turn 13568 paise into 13568 yen. That is refused here
    rather than silently rescaled, because a rescale would be a second rounding.
    """
    if not before or not after or before == after:
        return
    try:
        same_scale = exponent(before) == exponent(after)
    except ValueError:
        return
    if same_scale:
        return
    held = [f for f in getattr(type(instance), '_money_minor_fields', ())
            if getattr(instance, f, None) not in (None, 0)]
    if held:
        raise ValueError(
            f'{type(instance).__name__}: currency changing {before} -> {after} '
            f'after amounts were set in {before} ({", ".join(held)}). Set the '
            f'currency before assigning amounts.')


# ---------------------------------------------------------------------------
# Gateways
# ---------------------------------------------------------------------------

def to_gateway_major(minor, currency):
    """Integer minor units -> the JSON number a MAJOR-unit gateway API wants.

    Razorpay and Stripe take minor units and get the integer unchanged.
    Cashfree's `order_amount`, `refund_amount` and payout `amount` are major
    units (rupees), as JSON numbers. This is the one place that produces one:
    exact Decimal first, and only then the float that JSON serialisation
    needs. An amount below 2**53 minor units round-trips through a double
    exactly to the digits shown, which is every amount this platform carries.
    """
    value = from_minor(minor, currency)
    return float(value)
