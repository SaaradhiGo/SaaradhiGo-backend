"""Driver accounts made before 25 Oct through "Apply to drive" had no Driver row (AUDIT §36).

Creates the unapproved, cityless row their sign-up should have made. Additive only; reversing
leaves the rows, which the permission would create again anyway.
"""
from django.conf import settings
from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    Driver = apps.get_model('driver', 'Driver')
    for user in User.objects.filter(role='driver', driver__isnull=True).only('pk'):
        Driver.objects.get_or_create(user_id=user)


class Migration(migrations.Migration):
    dependencies = [
        ('driver', '0040_mvag_2025_driver_records'),
    ]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
