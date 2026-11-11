"""Money to integer minor units, step 1 of 3: add the columns. SCHEMA ONLY.

Step 2 (0014) converts and verifies; step 3 (0015) drops the Decimal columns.
"""

from django.db import migrations, models

MONEY = {'Payment': ['amount'], 'TransactionHistory': ['amount'], 'TripSettlement': ['gross_fare', 'commission_amount', 'driver_net']}


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0012_multi_country_market_fields'),
    ]

    operations = [
        *[migrations.AddField(model_name=m.lower(), name=f'{f}_minor',
                              field=models.BigIntegerField(null=True, blank=True))
          for m, fields in MONEY.items() for f in fields],
        # Nullable so reversing step 3 can re-add them to a table with rows.
        *[migrations.AlterField(model_name=m.lower(), name=f,
                                field=models.DecimalField(max_digits=10, decimal_places=2,
                                                          null=True, blank=True))
          for m, fields in MONEY.items() for f in fields],
    ]
