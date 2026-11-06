"""Seats per vehicle class, for the rider's choose-a-ride row (2026-11-06).

Additive: a nullable column. Existing classes are filled from the table the
rider app has shown since the pilot (bike 1, auto 3, cars 4, SUV 6), so the
apps can stop carrying their own copy. Any other class stays null.
"""

from django.db import migrations, models

SEATS = {
    'bike': 1,
    'auto': 3,
    'hatchback': 4,
    'mini': 4,
    'sedan': 4,
    'black': 4,
    'premium': 4,
    'car_premium': 4,
    'suv': 6,
}


def fill(apps, schema_editor):
    VehicleType = apps.get_model('driver', 'VehicleType')
    for vt in VehicleType.objects.all():
        seats = SEATS.get((vt.type or '').strip().lower())
        if seats is not None:
            VehicleType.objects.filter(pk=vt.pk).update(seats=seats)


class Migration(migrations.Migration):

    dependencies = [
        ('driver', '0038_withdrawal_conflict_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='vehicletype',
            name='seats',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
