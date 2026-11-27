# Review 27 Nov: who pays the authority for an airport or venue fee (driver at the gate, or
# SaaradhiGo under a permit). Additive.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('pricing', '0033_trip_charges')]

    operations = [
        migrations.AddField(model_name='tripchargeitem', name='collection',
                            field=models.CharField(blank=True, default='', max_length=20)),
    ]
