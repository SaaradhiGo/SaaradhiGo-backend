"""12 Nov (console QA): one active vehicle per driver, and a plate on one active vehicle anywhere.

Before the constraints, the duplicates are archived (status 'archived', kept for history): per
driver the vehicle the driver has as active is kept, else the oldest; per plate the same rule.
Additive: a new choice and two partial unique indexes; no row is deleted.
"""
from django.db import migrations, models
from django.db.models.functions import Replace, Upper


def _norm(plate):
    return ''.join((plate or '').split()).replace('-', '').upper()


def archive_duplicates(apps, schema_editor):
    Vehicle = apps.get_model('driver', 'Vehicle')
    Driver = apps.get_model('driver', 'Driver')
    active_of = dict(Driver.objects.exclude(active_vehicle__isnull=True).values_list('id', 'active_vehicle_id'))
    kept_as_active = set(active_of.values())
    archived = []

    def keep_one(rows):
        rows = sorted(rows, key=lambda v: (v.id not in kept_as_active, v.id))
        return rows[0], rows[1:]

    by_driver = {}
    for v in Vehicle.objects.filter(status='active').order_by('id'):
        by_driver.setdefault(v.driver_id_id, []).append(v)
    for rows in by_driver.values():
        if len(rows) > 1:
            _, extra = keep_one(rows)
            archived += [v.id for v in extra]
    by_plate = {}
    for v in Vehicle.objects.filter(status='active').exclude(id__in=archived).order_by('id'):
        by_plate.setdefault(_norm(v.vehicle_number), []).append(v)
    for plate, rows in by_plate.items():
        if plate and len(rows) > 1:
            _, extra = keep_one(rows)
            archived += [v.id for v in extra]
    if archived:
        Vehicle.objects.filter(id__in=archived).update(status='archived')
        Driver.objects.filter(active_vehicle_id__in=archived).update(active_vehicle=None)
        print(f'\n  archived duplicate vehicles: {sorted(archived)}')


class Migration(migrations.Migration):

    dependencies = [
        ('driver', '0047_driver_seed_flag'),
    ]

    operations = [
        migrations.AlterField(
            model_name='vehicle',
            name='status',
            field=models.CharField(choices=[('active', 'Active'), ('inactive', 'Inactive'),
                                            ('under_maintenance', 'Under Maintenance'), ('archived', 'Archived')],
                                   default='active', max_length=20),
        ),
        migrations.RunPython(archive_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='vehicle',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'active')), fields=('driver_id',),
                                               name='vehicle_one_active_per_driver'),
        ),
        migrations.AddConstraint(
            model_name='vehicle',
            constraint=models.UniqueConstraint(
                Upper(Replace(Replace(models.F('vehicle_number'), models.Value(' '), models.Value('')),
                              models.Value('-'), models.Value(''))),
                condition=models.Q(('status', 'active')), name='vehicle_active_plate_unique'),
        ),
    ]
