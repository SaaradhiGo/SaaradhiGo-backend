"""Money to integer minor units, step 2 of 3: convert exactly, then verify.

DATA ONLY -- see ride/0019 for why schema and data never share a migration,
and base/money_migration.py for the rules.

TripFareShadow had no currency; it takes its trip's, first.
"""

from django.db import migrations

from base import money_migration as mm
from base.money import normalize_currency

FIELDS = ['quoted_fare', 'shadow_estimate', 'shadow_actual']


def stamp_shadow_currency(apps, schema_editor):
    TripFareShadow = apps.get_model('pricing', 'TripFareShadow')
    for row in TripFareShadow.objects.select_related('trip').iterator():
        TripFareShadow.objects.filter(pk=row.pk).update(
            currency=normalize_currency(row.trip.currency))


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0011_money_minor_units_add'),
        ('ride', '0017_multi_country_market_fields'),
    ]

    operations = [
        migrations.RunPython(stamp_shadow_currency, migrations.RunPython.noop),
        migrations.RunPython(mm.forward('pricing', 'TripFareShadow', FIELDS),
                             mm.backward('pricing', 'TripFareShadow', FIELDS)),
    ]
