"""Attribute existing drivers to the one live city.

Every driver row predates `home_city`, so there is nothing to infer from.
Exactly one city is live, which makes this the only possible attribution
rather than a guess — see `backfill_driver_home_city`. If a second city is
ever live when this runs it assigns nobody and logs, because a
misattributed driver looks correct while an unassigned one is reported.

Reversible: clearing the column restores the prior state exactly.

NOT AUDITED, DELIBERATELY. Same reasoning as `pricing/0010`: historical
models, an audit table whose shape is not guaranteed at this point in the
migration graph, and a hash chain that a historical write would break.
`backfill_driver_home_city` detects the historical models, assigns without
audit rows and logs a warning; the seed path is audited, one row per
driver. See `admin_access.linking`.
"""

from django.db import migrations

from servers.admin_access.linking import backfill_driver_home_city


def forward(apps, schema_editor):
    backfill_driver_home_city(
        apps.get_model('driver', 'Driver'),
        apps.get_model('admin_access', 'GeoNode'),
    )


def backward(apps, schema_editor):
    apps.get_model('driver', 'Driver').objects.update(home_city=None)


class Migration(migrations.Migration):

    dependencies = [
        ('driver', '0026_driver_home_city'),
        ('admin_access', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
