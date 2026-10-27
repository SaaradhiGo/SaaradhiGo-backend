"""Money to integer minor units, step 1 of 3: add the columns. SCHEMA ONLY.

Step 2 (0012) converts and verifies; step 3 (0013) drops the Decimal columns.

RateCard and ServiceZone.per_trip_fee are pricing CONFIGURATION -- rates and
the inputs the quote multiplies -- and stay Decimal by decision.
"""

from django.db import migrations, models

FIELDS = ['quoted_fare', 'shadow_estimate', 'shadow_actual']


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0010_link_zones_to_geo_nodes'),
    ]

    operations = [
        migrations.AddField(
            model_name='tripfareshadow', name='currency',
            field=models.CharField(db_index=True, default='INR', max_length=3)),
        *[migrations.AddField(model_name='tripfareshadow', name=f'{f}_minor',
                              field=models.BigIntegerField(null=True, blank=True))
          for f in FIELDS],
    ]
