from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('driver', '0013_alter_withdrawalrequest_payout_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='vehicle',
            name='insurance_expiry',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='vehicle',
            name='permit_expiry',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='vehicle',
            name='fitness_expiry',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='vehicle',
            name='puc_expiry',
            field=models.DateField(blank=True, null=True),
        ),
    ]
