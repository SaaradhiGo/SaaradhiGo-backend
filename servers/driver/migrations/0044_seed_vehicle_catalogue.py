"""Seed the vehicle catalogue (10 Nov; AUDIT §37).

USA: NHTSA vPIC (GetModelsForMakeYear, model years 2011-2026), passenger cars as sedan and
hatchback, multipurpose passenger vehicles as suv, for 37 mainstream makes. India: a curated
list of common models per vehicle type, marked curated for admins to extend. No model is
invented; a driver whose vehicle is missing chooses "Other" and is reviewed.
"""
import json
from pathlib import Path

from django.db import migrations

DATA = Path(__file__).resolve().parent.parent / 'data'
US_TYPES = {'car': ('sedan', 'hatchback'), 'suv': ('suv',)}


def seed(apps, schema_editor):
    Entry = apps.get_model('driver', 'VehicleCatalogueEntry')
    rows = []
    india = json.loads((DATA / 'in_curated.json').read_text(encoding='utf-8'))
    for vtype, makes in india['types'].items():
        for make, models in makes.items():
            rows += [Entry(country='IN', vehicle_type=vtype, make=make, model=m, source=india['source'],
                           curated=True) for m in models]
    us = json.loads((DATA / 'us_vpic.json').read_text(encoding='utf-8'))
    for make, by_type in us['makes'].items():
        for kind, models in by_type.items():
            for vtype in US_TYPES[kind]:
                rows += [Entry(country='US', vehicle_type=vtype, make=make, model=m, source=us['source'][:128])
                         for m in models]
    Entry.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [('driver', '0043_encrypt_bank_accounts')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
