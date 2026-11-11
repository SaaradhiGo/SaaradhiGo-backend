"""Money to integer minor units, step 2 of 3: convert exactly, then verify.

DATA ONLY -- see ride/0019 for why schema and data never share a migration,
and base/money_migration.py for the rules.
"""

from django.db import migrations

from base import money_migration as mm

MONEY = {'Payment': ['amount'], 'TransactionHistory': ['amount'], 'TripSettlement': ['gross_fare', 'commission_amount', 'driver_net']}


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0013_money_minor_units_add'),
    ]

    operations = [
        migrations.RunPython(mm.forward('payments', m, fields),
                             mm.backward('payments', m, fields))
        for m, fields in MONEY.items()
    ]
