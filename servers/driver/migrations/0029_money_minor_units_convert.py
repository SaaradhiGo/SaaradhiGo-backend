"""Money to integer minor units, step 2 of 3: convert exactly, then verify.

DATA ONLY -- see ride/0019 for why schema and data never share a migration,
and base/money_migration.py for the rules.
"""

from django.db import migrations

from base import money_migration as mm

MONEY = {'WithdrawalRequest': ['amount'], 'IncentiveQuest': ['reward_amount']}


class Migration(migrations.Migration):

    dependencies = [
        ('driver', '0028_money_minor_units_add'),
    ]

    operations = [
        migrations.RunPython(mm.forward('driver', m, fields),
                             mm.backward('driver', m, fields))
        for m, fields in MONEY.items()
    ]
