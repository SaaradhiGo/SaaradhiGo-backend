"""Money to integer minor units, step 1 of 3: add the columns. SCHEMA ONLY.

Step 2 (0014) converts and verifies; step 3 (0015) drops the Decimal columns.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rider', '0012_multi_country_market_fields'),
    ]

    operations = [
        migrations.AddField(model_name='wallet', name='balance_minor',
                            field=models.BigIntegerField(null=True, blank=True)),
        migrations.AddField(model_name='wallettransaction', name='amount_minor',
                            field=models.BigIntegerField(null=True, blank=True)),
        # Nullable so reversing step 3 can re-add it to a table with rows.
        # (`balance` has a default, so it needs no such help.)
        migrations.AlterField(model_name='wallettransaction', name='amount',
                              field=models.DecimalField(max_digits=12, decimal_places=2,
                                                        null=True, blank=True)),
    ]
