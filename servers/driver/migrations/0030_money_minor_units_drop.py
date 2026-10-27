"""Money to integer minor units, step 2 of 2: drop the Decimal columns."""

from django.db import migrations, models

DROPPED = {
    'withdrawalrequest': ['amount'],
    'incentivequest': ['reward_amount'],
}


class Migration(migrations.Migration):

    dependencies = [
        ('driver', '0029_money_minor_units_convert'),
    ]

    operations = [
        *[migrations.RemoveField(model_name=m, name=f)
          for m, fields in DROPPED.items() for f in fields],
        *[migrations.AlterField(model_name=m, name=f'{f}_minor', field=models.BigIntegerField())
          for m, fields in DROPPED.items() for f in fields],
    ]
