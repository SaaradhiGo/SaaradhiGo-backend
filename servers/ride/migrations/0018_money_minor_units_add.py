"""Money to integer minor units, step 1 of 3: add the columns. SCHEMA ONLY.

Step 2 (0019) converts and verifies the data; step 3 (0020) drops the Decimal
columns. See 0019 for why schema and data never share a migration, and
docs/admin/AUDIT.md for the decision.
"""

from django.db import migrations, models

MONEY = {
    'Trip': ['estimated_fare', 'final_fare', 'cancellation_fee'],
    'FarePricing': ['base_fare', 'distance_fare', 'time_fare', 'total_fare',
                    'gross_fare', 'discount_amount', 'rider_payable'],
    'Receipt': ['total_fare', 'gst_amount'],
    'PromoCode': ['max_discount_amount', 'min_fare'],
    'PromoRedemption': ['discount_amount'],
}

# Non-null Decimal columns without a default. Made nullable here so that
# reversing step 2 can re-add them to a table that already has rows.
NULLABLE_FOR_REVERSE = {
    'farepricing': ['base_fare', 'distance_fare', 'time_fare', 'total_fare'],
    'receipt': ['total_fare'],
    'promocode': ['discount_value'],
    'promoredemption': ['discount_amount'],
}


def _minor_columns():
    for model, fields in MONEY.items():
        for field in fields:
            yield migrations.AddField(
                model_name=model.lower(), name=f'{field}_minor',
                field=models.BigIntegerField(null=True, blank=True))


def _nullable():
    for model, fields in NULLABLE_FOR_REVERSE.items():
        for field in fields:
            yield migrations.AlterField(
                model_name=model, name=field,
                field=models.DecimalField(max_digits=10, decimal_places=2,
                                          null=True, blank=True))


class Migration(migrations.Migration):

    dependencies = [
        ('ride', '0017_multi_country_market_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='promocode', name='currency',
            field=models.CharField(db_index=True, default='INR', max_length=3)),
        migrations.AddField(
            model_name='promoredemption', name='currency',
            field=models.CharField(db_index=True, default='INR', max_length=3)),
        migrations.AddField(
            model_name='promocode', name='discount_percent',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
        migrations.AddField(
            model_name='promocode', name='discount_flat_minor',
            field=models.BigIntegerField(blank=True, null=True)),
        *_minor_columns(),
        *_nullable(),
    ]
