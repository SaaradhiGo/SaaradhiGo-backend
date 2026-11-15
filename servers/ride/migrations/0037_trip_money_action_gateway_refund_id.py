from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('ride', '0036_trip_place_labels')]

    operations = [
        migrations.AddField(
            model_name='tripmoneyaction', name='gateway_refund_id',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
