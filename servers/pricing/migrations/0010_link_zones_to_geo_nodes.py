"""Attach existing service zones to the admin scope spine.

Links on the `code` the two already share. On a fresh database no node
exists yet, so this links nothing and `seed_admin_access` does the work;
on an existing database the four seeded zones (HYD, WGL, VJA, VTZ) attach
here. Both paths run `link_zones_to_nodes`, so "linked" means one thing.

Reversible by clearing the links, which loses no information: the code
match that produced them can be recomputed at any time.
"""

from django.db import migrations

from servers.admin_access.linking import link_zones_to_nodes


def forward(apps, schema_editor):
    link_zones_to_nodes(
        apps.get_model('pricing', 'ServiceZone'),
        apps.get_model('admin_access', 'GeoNode'),
    )


def backward(apps, schema_editor):
    apps.get_model('pricing', 'ServiceZone').objects.update(geo_node=None)


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0009_servicezone_geo_node'),
        ('admin_access', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
