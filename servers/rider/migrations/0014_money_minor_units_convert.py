"""Money to integer minor units, step 2 of 3: convert exactly, then verify.

DATA ONLY -- see ride/0019 for why schema and data never share a migration,
and base/money_migration.py for the rules.
"""

from django.db import migrations

from base import money_migration as mm


class Migration(migrations.Migration):

    dependencies = [
        ('rider', '0013_money_minor_units_add'),
    ]

    operations = [
        migrations.RunPython(mm.forward('rider', 'Wallet', ['balance']),
                             mm.backward('rider', 'Wallet', ['balance'])),
        migrations.RunPython(mm.forward('rider', 'WalletTransaction', ['amount']),
                             mm.backward('rider', 'WalletTransaction', ['amount'])),
    ]
