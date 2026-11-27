# Trip charges step 2 (27 Nov): waiting terms locked with the fare, and the waiting charged. Additive.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('ride', '0042_trip_extras_on_trip')]

    operations = [
        migrations.AddField(model_name='trip', name='waiting_minutes',
                            field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='trip', name='waiting_minor',
                            field=models.BigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='farepricing', name='waiting_terms',
                            field=models.JSONField(blank=True, null=True)),
    ]
