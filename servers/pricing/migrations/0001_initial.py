from decimal import Decimal

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('driver', '0014_vehicle_expiries'),
    ]

    operations = [
        migrations.CreateModel(
            name='ServiceZone',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('code', models.CharField(max_length=64, unique=True)),
                ('name', models.CharField(max_length=128)),
                ('country', models.CharField(default='IN', max_length=2)),
                ('state_code', models.CharField(blank=True, max_length=8)),
                ('city', models.CharField(blank=True, max_length=64)),
                ('zone_type', models.CharField(choices=[
                    ('country', 'Country'),
                    ('state', 'State'),
                    ('city', 'City'),
                    ('subzone', 'Sub-zone'),
                ], max_length=16)),
                ('polygon_geojson', models.JSONField()),
                ('priority', models.IntegerField(default=10)),
                ('currency', models.CharField(default='INR', max_length=3)),
                ('timezone_name', models.CharField(default='Asia/Kolkata', max_length=64)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('parent', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='children',
                    to='pricing.servicezone',
                )),
            ],
            options={
                'ordering': ['-priority', 'code'],
            },
        ),
        migrations.AddIndex(
            model_name='servicezone',
            index=models.Index(
                fields=['is_active', 'zone_type'], name='zone_active_type_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='servicezone',
            index=models.Index(
                fields=['country', 'state_code', 'city'],
                name='zone_country_state_city_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='servicezone',
            index=models.Index(
                fields=['priority'], name='zone_priority_idx',
            ),
        ),
        migrations.CreateModel(
            name='RateCard',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('base_fare', models.DecimalField(decimal_places=2, max_digits=10)),
                ('per_km_fare', models.DecimalField(decimal_places=2, max_digits=10)),
                ('per_min_fare', models.DecimalField(decimal_places=2, max_digits=10)),
                ('min_fare', models.DecimalField(decimal_places=2, max_digits=10)),
                ('night_surge_multiplier', models.DecimalField(
                    decimal_places=2, default=Decimal('1.00'), max_digits=4,
                )),
                ('night_surge_start_hour', models.IntegerField(default=23)),
                ('night_surge_end_hour', models.IntegerField(default=5)),
                ('surge_cap_multiplier', models.DecimalField(
                    decimal_places=2, default=Decimal('1.50'), max_digits=4,
                )),
                ('commission_percent', models.DecimalField(
                    decimal_places=2, default=Decimal('18.00'), max_digits=5,
                )),
                ('gst_percent', models.DecimalField(
                    decimal_places=2, default=Decimal('5.00'), max_digits=5,
                )),
                ('effective_from', models.DateTimeField(
                    db_index=True, default=django.utils.timezone.now,
                )),
                ('effective_to', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('version', models.IntegerField(default=1)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('vehicle_type', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='zone_rate_cards',
                    to='driver.vehicletype',
                )),
                ('zone', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='rate_cards',
                    to='pricing.servicezone',
                )),
            ],
            options={
                'ordering': ['-effective_from', '-version'],
            },
        ),
        migrations.AddIndex(
            model_name='ratecard',
            index=models.Index(
                fields=['zone', 'vehicle_type', 'is_active'],
                name='ratecard_zone_vt_active_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='ratecard',
            index=models.Index(
                fields=['effective_from', 'effective_to'],
                name='ratecard_effective_idx',
            ),
        ),
    ]
