"""Money to integer minor units, step 1 of 3: add the columns. SCHEMA ONLY.

Step 2 (0029) converts and verifies; step 3 (0030) drops the Decimal columns.

IncentiveQuest had no currency. Quests are not zone-scoped, so every existing
quest is in the default market's currency, which is the column default.
"""

from django.db import migrations, models

MONEY = {'WithdrawalRequest': ['amount'], 'IncentiveQuest': ['reward_amount']}


class Migration(migrations.Migration):

    dependencies = [
        ('driver', '0027_backfill_driver_home_city'),
    ]

    operations = [
        migrations.AddField(
            model_name='incentivequest', name='currency',
            field=models.CharField(db_index=True, default='INR', max_length=3)),
        *[migrations.AddField(model_name=m.lower(), name=f'{f}_minor',
                              field=models.BigIntegerField(null=True, blank=True))
          for m, fields in MONEY.items() for f in fields],
        # Nullable so reversing step 3 can re-add them to a table with rows.
        *[migrations.AlterField(model_name=m.lower(), name=f,
                                field=models.DecimalField(max_digits=10, decimal_places=2,
                                                          null=True, blank=True))
          for m, fields in MONEY.items() for f in fields],
    ]
