"""Money to integer minor units, step 2 of 2: drop the Decimal columns."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0012_money_minor_units_convert'),
    ]

    operations = [
        migrations.RemoveField(model_name='tripfareshadow', name='quoted_fare'),
        migrations.RemoveField(model_name='tripfareshadow', name='shadow_estimate'),
        migrations.RemoveField(model_name='tripfareshadow', name='shadow_actual'),
    ]
