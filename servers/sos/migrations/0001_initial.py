from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('ride', '0006_alter_trip_status_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='SOSEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_label', models.CharField(blank=True, help_text='Frozen human-readable label of the user at event time.', max_length=255)),
                ('initiated_by', models.CharField(choices=[('rider', 'Rider'), ('driver', 'Driver')], max_length=10)),
                ('event_type', models.CharField(choices=[('panic', 'Panic'), ('vehicle_breakdown', 'Vehicle breakdown'), ('medical', 'Medical emergency'), ('harassment', 'Harassment / safety concern'), ('accident', 'Accident'), ('other', 'Other')], default='panic', max_length=32)),
                ('status', models.CharField(choices=[('open', 'Open'), ('acknowledged', 'Acknowledged by ops'), ('resolved', 'Resolved'), ('false_alarm', 'False alarm')], db_index=True, default='open', max_length=20)),
                ('latitude', models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True)),
                ('longitude', models.DecimalField(blank=True, decimal_places=7, max_digits=10, null=True)),
                ('location_accuracy_m', models.FloatField(blank=True, null=True)),
                ('note', models.TextField(blank=True, help_text='Optional free-text from the caller.')),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.CharField(blank=True, max_length=512)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('trip', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name='sos_events', to='ride.trip')),
                ('user', models.ForeignKey(blank=True, help_text='User who raised the event (nullable to survive user deletion).', null=True, on_delete=models.deletion.SET_NULL, related_name='sos_events', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['status', '-created_at'], name='sos_status_created_idx'),
                    models.Index(fields=['trip', '-created_at'], name='sos_trip_created_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='SOSEventUpdate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_label', models.CharField(blank=True, max_length=255)),
                ('new_status', models.CharField(blank=True, max_length=20)),
                ('note', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('event', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='updates', to='sos.sosevent')),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
    ]
