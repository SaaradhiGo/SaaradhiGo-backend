"""Money to integer minor units, step 2 of 2: drop the Decimal columns."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rider', '0014_money_minor_units_convert'),
    ]

    operations = [
        migrations.RemoveField(model_name='wallet', name='balance'),
        migrations.RemoveField(model_name='wallettransaction', name='amount'),
        migrations.AlterField(model_name='wallet', name='balance_minor',
                              field=models.BigIntegerField(default=0)),
        migrations.AlterField(model_name='wallettransaction', name='amount_minor',
                              field=models.BigIntegerField()),
    ]
