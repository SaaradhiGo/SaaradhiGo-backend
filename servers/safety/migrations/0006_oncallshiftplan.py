"""25 Nov (owner ruling): the hourly safety rota. ADDITIVE: one new table."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('safety', '0005_incident_public_ref'),
        ('admin_access', '0003_alter_geonode_status'),
    ]

    operations = [
        migrations.CreateModel(
            name='OnCallShiftPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.PositiveIntegerField(default=1)),
                ('shifts', models.JSONField(blank=True, default=list)),
                ('status', models.CharField(db_index=True, default='active', max_length=10)),
                ('approval_request_id', models.BigIntegerField(blank=True, null=True)),
                ('reason', models.TextField(blank=True, default='')),
                ('created_by_label', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('city', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='shift_plans',
                                           to='admin_access.geonode')),
            ],
            options={'constraints': [models.UniqueConstraint(condition=models.Q(('status', 'active')),
                                                             fields=('city',), name='shiftplan_one_active_per_city')]},
        ),
    ]
