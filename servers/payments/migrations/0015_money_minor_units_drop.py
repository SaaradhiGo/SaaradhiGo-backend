"""Money to integer minor units, step 2 of 2: drop the Decimal columns."""

from django.db import migrations, models

DROPPED = {
    'payment': ['amount'],
    'transactionhistory': ['amount'],
    'tripsettlement': ['gross_fare', 'commission_amount', 'driver_net'],
}


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0014_money_minor_units_convert'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='tripsettlement', name='tripsettlement_commission_non_negative'),
        migrations.RemoveConstraint(
            model_name='tripsettlement', name='tripsettlement_gross_non_negative'),
        *[migrations.RemoveField(model_name=m, name=f)
          for m, fields in DROPPED.items() for f in fields],
        *[migrations.AlterField(model_name=m, name=f'{f}_minor', field=models.BigIntegerField())
          for m, fields in DROPPED.items() for f in fields],
        migrations.AddConstraint(
            model_name='tripsettlement',
            constraint=models.CheckConstraint(
                condition=models.Q(commission_amount_minor__gte=0),
                name='tripsettlement_commission_non_negative')),
        migrations.AddConstraint(
            model_name='tripsettlement',
            constraint=models.CheckConstraint(
                condition=models.Q(gross_fare_minor__gte=0),
                name='tripsettlement_gross_non_negative')),
    ]
