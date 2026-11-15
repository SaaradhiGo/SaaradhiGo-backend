from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('approvals', '0003_self_approval_grant')]

    operations = [
        migrations.AddField(model_name='approvalpolicy', name='maker_daily_limit',
                            field=models.BigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name='approvalpolicy', name='cumulative_per_target',
                            field=models.BooleanField(default=False)),
    ]
