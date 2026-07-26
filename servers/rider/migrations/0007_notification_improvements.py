from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rider', '0006_rename_razorpay_fields_to_generic'),
        ('ride', '0006_alter_trip_status_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='notification',
            name='notif_type',
            field=models.CharField(
                blank=True,
                choices=[
                    ('ride_event', 'Ride event'),
                    ('payment', 'Payment'),
                    ('payout', 'Payout'),
                    ('wallet', 'Wallet'),
                    ('kyc', 'KYC'),
                    ('sos', 'SOS'),
                    ('system', 'System'),
                    ('marketing', 'Marketing'),
                ],
                db_index=True,
                default='',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='notification',
            name='trip',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=models.deletion.SET_NULL,
                related_name='notifications',
                to='ride.trip',
            ),
        ),
        migrations.AddField(
            model_name='notification',
            name='data',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='notification',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, db_index=True),
        ),
        migrations.AlterModelOptions(
            name='notification',
            options={'ordering': ['-created_at']},
        ),
        migrations.AddIndex(
            model_name='notification',
            index=models.Index(fields=['user_id', 'is_read', '-created_at'], name='notif_user_unread_idx'),
        ),
    ]
