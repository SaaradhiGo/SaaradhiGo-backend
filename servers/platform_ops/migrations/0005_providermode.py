"""25 Nov (owner ruling): provider modes switched from the console. ADDITIVE: one new table."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('platform_ops', '0004_driver_application')]

    operations = [
        migrations.CreateModel(
            name='ProviderMode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('provider_type', models.CharField(max_length=16)),
                ('country', models.CharField(max_length=2)),
                ('mode', models.CharField(max_length=8)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('version', models.PositiveIntegerField(default=1)),
                ('changed_via_request_id', models.BigIntegerField(blank=True, null=True)),
                ('reason', models.TextField(blank=True, default='')),
                ('created_by_label', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'constraints': [models.UniqueConstraint(condition=models.Q(('is_active', True)),
                                                             fields=('provider_type', 'country'),
                                                             name='provider_mode_one_active')]},
        ),
    ]
