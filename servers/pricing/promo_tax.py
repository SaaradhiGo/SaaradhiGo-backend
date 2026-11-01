"""US tax on a promo trip (owner ruling 2026-11-01, AUDIT 27.59 ruling 2).

THE RULE
--------
In an ADDITIVE-tax market (the US) tax is charged on the amount the rider is actually charged
for the fare, so a platform-funded promo discount reduces the taxable amount:

    taxable   = max(0, pretax_fare - discount)
    tax       = tax_percent x taxable                       (rounded once, half-up)
    rider pays = (pretax_fare - discount) + tax + per_trip_fee      (never below 0)

The per-trip regulatory fee is not taxed and not discounted. A trip without a promo is exactly
as before (`discount = 0` gives back the quote's own tax). An INCLUSIVE market (India) never
comes here: the tax is already inside the fare and nothing moves.

WHAT DOES NOT MOVE
------------------
The discount itself (module 12 computes it on the quoted total, exactly as before, and the
platform-funded ledger entry is that discount, capped at what the rider would otherwise pay), the
fare card, surge, the per-trip fee, the commission rate, the driver's cash credit for the
discount. What moves is the tax line: the "full fare" a US promo trip carries (what commission
and driver earnings are computed on, `Trip.estimated_fare`) is the pre-discount fare with the tax
worked out on the discounted amount, so it is lower than before by `relief`.

US ACCOUNTANT TO CONFIRM (go-live blocker #15): no rate is decided here; the percentage and the
fee are the zone's and the rate card's own configured values.
"""

from decimal import Decimal

from base.money import from_minor, quantize

D = Decimal


def additive_context(zone, vehicle_type=None):
    """`(tax_percent, fee)` Decimals for an additive-tax zone, or None (inclusive tax, no zone).

    The percentage is the one the zone's rate card carries (the card for the vehicle class when
    given, else any active card of the zone: tax is a property of the jurisdiction)."""
    if zone is None or (getattr(zone, 'tax_mode', '') or 'inclusive') != 'additive':
        return None
    from servers.pricing.models import RateCard
    from servers.pricing.services import get_active_rate_card

    card = get_active_rate_card(zone, vehicle_type) if vehicle_type is not None else None
    if card is None:
        card = RateCard.objects.filter(zone=zone, is_active=True).order_by('-version').first()
    if card is None:
        return None
    currency = getattr(zone, 'currency', None) or 'USD'
    fee = quantize(D(str(getattr(zone, 'per_trip_fee', None) or '0')), currency)
    return D(str(card.gst_percent or '0')), fee


def split_additive(total, percent, fee, currency):
    """`(pretax, tax, fee)` of a quoted additive total (major-unit Decimals).

    Exact for every quote `quote_fare` makes: the pre-tax fare is searched so that
    pretax + round(pretax x percent) + fee == total."""
    percent = D(str(percent))
    fee = min(D(str(fee)), D(str(total)))
    remainder = D(str(total)) - fee
    guess = quantize(remainder / (D(1) + percent / D(100)), currency)
    one = from_minor(1, currency)
    for step in (0, -1, 1, -2, 2):
        pretax = guess + one * step
        if pretax < 0:
            continue
        if pretax + quantize(pretax * percent / D(100), currency) == remainder:
            return pretax, remainder - pretax, fee
    return guess, remainder - guess, fee


def adjust(pretax, percent, fee, discount, currency, tax_before=None):
    """The rider's amounts for a promo discount in an additive market.

    Returns a dict (major-unit Decimals): `tax` on the discounted fare, `full` (the pre-discount
    amount with that tax, which is what commission and driver earnings use), `discount` (capped at
    `full`), `payable` (what the rider is charged) and `relief` (the tax no longer charged,
    always >= 0)."""
    percent = D(str(percent))
    pretax, fee, discount = D(str(pretax)), D(str(fee)), D(str(discount))
    before = (quantize(pretax * percent / D(100), currency) if tax_before is None
              else D(str(tax_before)))
    taxable = max(D('0'), pretax - discount)
    tax = min(quantize(taxable * percent / D(100), currency), before)
    full = pretax + tax + fee
    applied = min(discount, full)
    return {'taxable': taxable, 'tax': tax, 'tax_before': before, 'full': full,
            'discount': applied, 'payable': full - applied, 'relief': before - tax,
            'pretax': pretax, 'fee': fee}
