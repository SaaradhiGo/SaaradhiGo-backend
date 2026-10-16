"""Template filters so the operator console shows the right currency.

WHY A FILTER RATHER THAN FIXING THE STRINGS
-------------------------------------------
There were about forty hardcoded `₹` across eleven console templates. Replacing each
with `$` where a US page needs it would be the same mistake twice: the symbol belongs
to the ROW, not to the page. A trip list can contain an Indian trip and a US trip on
the same screen.

So the filter takes the currency from whatever it is rendering:

    {{ trip.estimated_fare|money:trip.currency }}     -> ₹190.00  or  $30.83
    {{ total|money:"USD" }}                           -> $30.83
    {{ totals|money_totals }}                         -> ₹190.00 + $30.83

`money_totals` renders a `sum_by_currency` map as separate amounts. It deliberately
cannot produce a single number: adding rupees to dollars and presenting the result as
money is the defect the currency column exists to prevent, and a template filter that
did it would reintroduce it at the last possible moment.
"""

from django import template

from base.money import DEFAULT_CURRENCY
from base.money import format_money as _format_money
from base.money import format_totals as _format_totals

register = template.Library()


@register.filter(name='money')
def money(value, currency=None):
    """Format an amount in its own currency.

    Falls back to the deployment default when a template has no currency to
    pass -- which is every template today that renders an aggregate. That is
    still better than a hardcoded symbol, because the fallback follows the
    deployment while a literal `₹` follows nothing.
    """
    if value in (None, ''):
        return '—'
    try:
        return _format_money(value, currency or DEFAULT_CURRENCY)
    except Exception:  # noqa: BLE001
        # A template filter must never raise: it would blank the whole page an
        # operator is trying to read.
        return str(value)


@register.filter(name='money_bare')
def money_bare(value, currency=None):
    """The number without a symbol, for a column that labels its own currency."""
    if value in (None, ''):
        return '—'
    try:
        return _format_money(value, currency or DEFAULT_CURRENCY,
                             with_symbol=False)
    except Exception:  # noqa: BLE001
        return str(value)


@register.filter(name='money_totals')
def money_totals(totals):
    """Render a `sum_by_currency` map as separate amounts, never a sum."""
    if not totals:
        return _format_money(0, DEFAULT_CURRENCY)
    try:
        return _format_totals(totals)
    except Exception:  # noqa: BLE001
        return str(totals)
