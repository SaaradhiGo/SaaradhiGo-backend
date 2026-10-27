"""Money to integer minor units, step 2 of 3: convert exactly, then verify.

DATA ONLY. Step 1 (0018) added the columns; step 3 (0020) drops the Decimal
ones. Kept apart because PostgreSQL refuses DDL on a table with pending
trigger events in the same transaction, and each migration is one
transaction -- so no migration here mixes an UPDATE with an ALTER, in either
direction.

See base/money_migration.py for the conversion and verification rules.

PromoCode and PromoRedemption get their currency first, because the
conversion needs each row's digits: a promo takes its zone's currency (an
all-zones code, the default market's); a redemption its trip's, else its
promo's. PromoCode.discount_value then splits by `discount_type` into
`discount_percent` (a rate) or `discount_flat_minor` (money).
"""

from decimal import Decimal

from django.db import migrations

from base import money_migration as mm
from base.money import DEFAULT_CURRENCY, exponent, normalize_currency


def stamp_promo_currency(apps, schema_editor):
    PromoCode = apps.get_model('ride', 'PromoCode')
    PromoRedemption = apps.get_model('ride', 'PromoRedemption')
    from base.countries import get_country

    for promo in PromoCode.objects.select_related('zone').iterator():
        zone = promo.zone
        code = DEFAULT_CURRENCY
        if zone is not None:
            code = zone.currency or get_country(zone.country).currency
        PromoCode.objects.filter(pk=promo.pk).update(currency=normalize_currency(code))

    for red in PromoRedemption.objects.select_related('trip', 'promo').iterator():
        code = (red.trip.currency if red.trip_id and red.trip.currency
                else red.promo.currency)
        PromoRedemption.objects.filter(pk=red.pk).update(currency=normalize_currency(code))


def split_promo_discount(apps, schema_editor):
    PromoCode = apps.get_model('ride', 'PromoCode')
    for promo in PromoCode.objects.all().iterator():
        digits = exponent(normalize_currency(promo.currency))
        if promo.discount_type == 'flat':
            promo.discount_flat_minor = mm._exact_minor(
                promo.discount_value, digits, f'PromoCode#{promo.pk}.discount_value')
            promo.discount_percent = None
        else:
            promo.discount_percent = promo.discount_value
            promo.discount_flat_minor = None
        promo.save(update_fields=['discount_flat_minor', 'discount_percent'])

    # Verification: a second read, every row.
    bad = []
    for promo in PromoCode.objects.all().iterator():
        digits = exponent(normalize_currency(promo.currency))
        if promo.discount_type == 'flat':
            ok = (promo.discount_percent is None and promo.discount_flat_minor is not None
                  and Decimal(promo.discount_flat_minor)
                  == Decimal(promo.discount_value) * (Decimal(10) ** digits))
        else:
            ok = (promo.discount_flat_minor is None
                  and promo.discount_percent == promo.discount_value)
        if not ok:
            bad.append(promo.pk)
    if bad:
        raise mm.MoneyMigrationError(f'PromoCode discount split differs for ids {bad[:10]}')


def unsplit_promo_discount(apps, schema_editor):
    PromoCode = apps.get_model('ride', 'PromoCode')
    for promo in PromoCode.objects.all().iterator():
        digits = exponent(normalize_currency(promo.currency))
        if promo.discount_type == 'flat':
            promo.discount_value = Decimal(promo.discount_flat_minor or 0).scaleb(-digits)
        else:
            promo.discount_value = promo.discount_percent or Decimal('0')
        promo.save(update_fields=['discount_value'])


MONEY = {
    'Trip': ['estimated_fare', 'final_fare', 'cancellation_fee'],
    'FarePricing': ['base_fare', 'distance_fare', 'time_fare', 'total_fare',
                    'gross_fare', 'discount_amount', 'rider_payable'],
    'Receipt': ['total_fare', 'gst_amount'],
    'PromoCode': ['max_discount_amount', 'min_fare'],
    'PromoRedemption': ['discount_amount'],
}

def _convert():
    for model, fields in MONEY.items():
        yield migrations.RunPython(
            mm.forward('ride', model, fields), mm.backward('ride', model, fields))


class Migration(migrations.Migration):

    dependencies = [
        ('ride', '0018_money_minor_units_add'),
        ('pricing', '0010_link_zones_to_geo_nodes'),
    ]

    operations = [
        migrations.RunPython(stamp_promo_currency, migrations.RunPython.noop),
        migrations.RunPython(split_promo_discount, unsplit_promo_discount),
        *_convert(),
    ]
