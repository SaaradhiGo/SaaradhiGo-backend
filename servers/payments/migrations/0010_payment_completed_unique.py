from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0009_webhookevent'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.UniqueConstraint(
                fields=['trip_id'],
                condition=models.Q(status='completed'),
                name='one_completed_payment_per_trip',
            ),
        ),
    ]
