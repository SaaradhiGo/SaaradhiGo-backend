"""The fare lines a rider is shown, built so they add up to the total to the paisa (12 Nov).

Trip 207's invoice read base 60.00 + distance and time 122.59 + GST 8.71 = 191.30 beside a total of
183.00: the lines were the fare's gross components (GST already inside them, India) with the GST
listed again as if it were added on top. One builder now serves the PDF, the email, the receipt
screen and the choose screen's "How this fare is worked out", so they cannot drift apart.

Inclusive tax (India): every line is stated BEFORE tax. The gross components (base, distance and
time, demand, minimum fare, rounding) are scaled by taxable / total, rounded to the minor unit, and
the last paisa or two of rounding goes on the largest line, so

    lines = taxable value,  taxable value + tax (included) = total.

Additive tax (the US): the components are already before tax; the tax and any flat fee are added.

Pure: amounts in, lines out. Nothing here changes what anyone is charged.
"""
from decimal import ROUND_HALF_UP, Decimal

from base.money import quantize

ZERO = Decimal('0.00')
UNTAXED_CODES = ('rider_platform_fee', 'per_trip_fee')


def _d(value):
    return Decimal(str(value if value not in (None, '') else '0'))


def _minor(currency):
    """The currency's smallest unit (0.01 for INR and USD, 1 for JPY)."""
    exponent = quantize(Decimal('1.5'), currency).as_tuple().exponent
    return Decimal(1).scaleb(exponent)


def _scale(lines, target, currency):
    """Scale gross `lines` so they sum exactly to `target`, each rounded to the minor unit."""
    gross = sum((amount for _, _, amount in lines), ZERO)
    if not lines:
        return []
    if gross == 0:
        out = [(code, label, ZERO) for code, label, _ in lines]
    else:
        unit = _minor(currency)
        out = [(code, label, (amount * target / gross).quantize(unit, rounding=ROUND_HALF_UP))
               for code, label, amount in lines]
    gap = target - sum((amount for _, _, amount in out), ZERO)
    if gap:
        i = max(range(len(out)), key=lambda k: abs(out[k][2]))
        code, label, amount = out[i]
        out[i] = (code, label, amount + gap)
    return out


def build(*, components, total, tax_amount, tax_mode, tax_percent, tax_label, currency, extras=()):
    """The lines for one fare.

    `components`  [(code, label, gross amount)] before any untaxed extras: base, distance and time,
                  demand, minimum fare, rounding. In an inclusive market they contain the tax.
    `extras`      [(code, label, amount)] outside the tax: a flat regulatory fee (US) or the rider
                  platform fee. Listed after the tax.
    Returns {'lines', 'taxable', 'tax': {...}, 'extras', 'total'} with string amounts, and
    sum(lines) == taxable, taxable + (tax if additive) + extras == total.
    """
    total = _d(total)
    tax = _d(tax_amount)
    extras = [(c, l, _d(a)) for c, l, a in extras if _d(a)]
    extras_sum = sum((a for _, _, a in extras), ZERO)
    components = [(c, l, _d(a)) for c, l, a in components]
    inclusive = (tax_mode or 'inclusive') != 'additive'
    taxable = total - extras_sum - tax
    # Inclusive: the gross components contain the tax, so this scales them down to before tax.
    # Additive: they are already before tax and sum to `taxable`, so this changes nothing.
    lines = _scale(components, taxable, currency)
    lines = [(c, l, a) for c, l, a in lines if a or c in ('base', 'distance_time')]
    return {
        'lines': [{'code': c, 'label': l, 'amount': str(a)} for c, l, a in lines],
        'taxable': str(taxable),
        'tax': {'label': tax_label or 'Tax', 'percent': _percent(tax_percent),
                'amount': str(tax), 'included': inclusive},
        'extras': [{'code': c, 'label': l, 'amount': str(a)} for c, l, a in extras],
        'total': str(total),
        'currency': currency,
    }


def _percent(value):
    p = _d(value).normalize()
    return format(p, 'f')


def from_quote(fare):
    """The lines for a fare quote (`quote_fare`'s dict): what the choose screen explains."""
    adjustments = fare.get('fare_adjustments') or []
    components = [
        ('base', 'Base fare', fare.get('base_fare')),
        ('distance_time', 'Distance and time', _d(fare.get('distance_fare')) + _d(fare.get('time_fare'))),
    ]
    extras = []
    for a in adjustments:
        code = a.get('code')
        if code == 'tax':
            continue
        if code in UNTAXED_CODES:
            extras.append((code, a.get('label') or code, a.get('amount')))
        else:
            components.append((code, a.get('label') or code, a.get('amount')))
    return build(components=components, total=fare.get('total_fare'), tax_amount=fare.get('tax_amount'),
                 tax_mode=fare.get('tax_mode'), tax_percent=fare.get('tax_percent'),
                 tax_label=fare.get('tax_label'), currency=fare.get('currency') or 'INR', extras=extras)


def from_receipt(trip, receipt, fare_pricing, tax_percent, tax_label):
    """The lines for an issued receipt: its own total and tax (never recomputed), decomposed from
    the trip's fare snapshot. Whatever the snapshot does not explain is named: under one minor unit
    of the rounding rule it is "Rounding", otherwise a minimum fare or an adjustment."""
    currency = getattr(receipt, 'currency', None) or 'INR'
    total = _d(receipt.total_fare)
    zone = getattr(trip, 'zone', None)
    tax_mode = getattr(zone, 'tax_mode', '') or 'inclusive'
    components = []
    extras = []
    if fare_pricing is not None and getattr(trip, 'end_rule', '') != 'platform_funded' and total:
        base = _d(fare_pricing.base_fare)
        metered = _d(fare_pricing.distance_fare) + _d(fare_pricing.time_fare)
        surge = _d(fare_pricing.surge_multiplier or 1)
        components = [('base', 'Base fare', base), ('distance_time', 'Distance and time', metered)]
        if surge != Decimal('1'):
            from servers.ride.receipts import _demand_line
            components.append(('surge', _demand_line(surge), quantize((base + metered) * (surge - 1), currency)))
        fee_minor = getattr(fare_pricing, 'rider_platform_fee_minor', 0) or 0
        if fee_minor:
            from base.money import from_minor
            extras.append(('rider_platform_fee', 'Platform fee', from_minor(fee_minor, currency)))
        gross = total - sum((_d(a) for _, _, a in extras), ZERO)
        if tax_mode == 'additive':
            gross -= _d(receipt.gst_amount)
        residual = gross - sum((a for _, _, a in components), ZERO)
        if residual:
            label = ('Rounding' if abs(residual) < 1
                     else 'Minimum fare adjustment' if residual > 0 else 'Fare adjustment')
            components.append(('rounding' if abs(residual) < 1 else 'adjustment', label, residual))
    elif total:
        components = [('fare', 'Fare', total)]
    return build(components=components, total=total, tax_amount=receipt.gst_amount, tax_mode=tax_mode,
                 tax_percent=tax_percent, tax_label=tax_label, currency=currency, extras=extras)


def adds_up(invoice):
    """True when the lines sum to the taxable value and the taxable value, the tax (when added)
    and the extras sum to the total, exactly."""
    lines = sum((_d(x['amount']) for x in invoice['lines']), ZERO)
    extras = sum((_d(x['amount']) for x in invoice['extras']), ZERO)
    taxable = _d(invoice['taxable'])
    tax = _d(invoice['tax']['amount'])
    return lines == taxable and taxable + tax + extras == _d(invoice['total'])
