"""Money to integer minor units, step 2 of 2: drop the Decimal columns.

A separate migration from 0018 so the data conversion commits on its own:
PostgreSQL refuses an ALTER TABLE on a table with pending trigger events, and
it runs each migration in its own transaction.
"""

from django.db import migrations, models

DROPPED = {
    'trip': ['estimated_fare', 'final_fare', 'cancellation_fee'],
    'farepricing': ['base_fare', 'distance_fare', 'time_fare', 'total_fare',
                    'gross_fare', 'discount_amount', 'rider_payable'],
    'receipt': ['total_fare', 'gst_amount'],
    'promocode': ['discount_value', 'max_discount_amount', 'min_fare'],
    'promoredemption': ['discount_amount'],
}

REQUIRED = {
    'farepricing': ['base_fare_minor', 'distance_fare_minor', 'time_fare_minor',
                    'total_fare_minor'],
    'receipt': ['total_fare_minor'],
    'promoredemption': ['discount_amount_minor'],
}

DEFAULT_ZERO = {
    'receipt': ['gst_amount_minor'],
    'promocode': ['min_fare_minor'],
}


class Migration(migrations.Migration):

    dependencies = [
        ('ride', '0019_money_minor_units_convert'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='farepricing', name='farepricing_discount_non_negative'),
        *[migrations.RemoveField(model_name=m, name=f)
          for m, fields in DROPPED.items() for f in fields],
        *[migrations.AlterField(model_name=m, name=f, field=models.BigIntegerField())
          for m, fields in REQUIRED.items() for f in fields],
        *[migrations.AlterField(model_name=m, name=f, field=models.BigIntegerField(default=0))
          for m, fields in DEFAULT_ZERO.items() for f in fields],
        migrations.AlterField(
            model_name='trip', name='cancellation_fee_minor',
            field=models.BigIntegerField(
                default=0,
                help_text='Charged to the cancelling party, per the published '
                          'cancellation policy. Minor units of `currency`.')),
        migrations.AddConstraint(
            model_name='farepricing',
            constraint=models.CheckConstraint(
                condition=(models.Q(discount_amount_minor__isnull=True)
                           | models.Q(discount_amount_minor__gte=0)),
                name='farepricing_discount_non_negative')),
    ]
