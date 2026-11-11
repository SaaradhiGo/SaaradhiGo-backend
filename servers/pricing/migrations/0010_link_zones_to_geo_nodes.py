"""Attach existing service zones to the admin scope spine.

Links on the `code` the two already share. On a fresh database no node
exists yet, so this links nothing and `seed_admin_access` does the work;
on an existing database the four seeded zones (HYD, WGL, VJA, VTZ) attach
here. Both paths run `link_zones_to_nodes`, so "linked" means one thing.

Reversible by clearing the links, which loses no information: the code
match that produced them can be recomputed at any time.

NOT AUDITED, DELIBERATELY. This runs with historical models, inside a
migration where the audit table may not yet have the shape the live model
expects, and a row written through a historical `AdminAuditLog` would carry
no hash and break `verify_audit_chain` for every row after it. So
`link_zones_to_nodes` detects the historical models and links without audit
rows, logging a warning. The seed path (`seed_admin_access`) is audited, one
row per zone, with the operator who ran it. On QA and production this
migration ran on 2026-10-27; the links it made then are the gap, and
`admin_access.linking` documents it.
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
