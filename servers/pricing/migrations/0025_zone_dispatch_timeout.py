from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('pricing', '0024_seed_driver_requirements')]

    operations = [
        migrations.AddField(
            model_name='servicezone',
            name='dispatch_timeout_seconds',
            field=models.PositiveIntegerField(
                blank=True, null=True,
                help_text='Seconds to look for a driver (30-900). Blank: the parent zone or 180.'),
        ),
    ]
