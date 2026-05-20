from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0008_remove_payment_razorpay_order_id_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='WebhookEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('gateway', models.CharField(db_index=True, max_length=32)),
                ('dedupe_key', models.CharField(max_length=512)),
                ('event_type', models.CharField(blank=True, max_length=64)),
                ('raw_payload', models.JSONField(blank=True, default=dict)),
                ('received_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('processed_at', models.DateTimeField(blank=True, null=True)),
                ('result', models.CharField(blank=True, max_length=32)),
            ],
            options={
                'ordering': ['-received_at'],
            },
        ),
        migrations.AddIndex(
            model_name='webhookevent',
            index=models.Index(fields=['gateway', '-received_at'], name='webhook_event_gw_recv_idx'),
        ),
        migrations.AddConstraint(
            model_name='webhookevent',
            constraint=models.UniqueConstraint(
                fields=['gateway', 'dedupe_key'],
                name='webhook_event_unique_per_gateway',
            ),
        ),
    ]
