"""City launch status (module 15, AUDIT 27.36 product decision 1).

ADDITIVE, WITH A BACKFILL
-------------------------
* Schema: `GeoNode.status` gains the value `draft` (and its default becomes `draft`). No column
  is added, dropped or retyped; the CharField is as wide as before.
* Data: today's cities are sorted so that nothing on QA stops.
    - a city that is `active` stays `active` (live);
    - a city that is `paused` AND has an active service zone linked to it was reachable by
      riders before this module (nothing gated on status), so it becomes `active` (live):
      Austin on QA is the example;
    - a city that is `paused` with no active zone could never take a booking, so it becomes
      `draft`: a city that was never launched. It can be launched from Platform Settings once
      its readiness checklist is green.
  Country and state nodes are not touched (a country's live state is derived from its cities).
  One audit row is written per city that changes. The migration never fails because of the audit
  write.
* Reverse: restores `paused` for rows this migration turned into `draft`/`active` is NOT
  attempted (the previous values are in the audit rows); the reverse is a no-op.
"""

from django.db import migrations, models


def backfill(apps, schema_editor):
    GeoNode = apps.get_model('admin_access', 'GeoNode')
    ServiceZone = apps.get_model('pricing', 'ServiceZone')
    changed = []
    for node in GeoNode.objects.filter(kind='city', status='paused').order_by('path'):
        zoned = ServiceZone.objects.filter(geo_node_id=node.pk, is_active=True).exists()
        new = 'active' if zoned else 'draft'
        GeoNode.objects.filter(pk=node.pk).update(status=new)
        changed.append((node.code, 'paused', new))
    if not changed:
        return
    try:
        from servers.admin_audit.models import AdminAuditLog

        for code, old, new in changed:
            AdminAuditLog.objects.create(
                actor_label='migration admin_access.0002_city_status_draft',
                action='city_status_backfilled', target_type='geonode', target_id=code,
                before={'status': old}, after={'status': new},
                reason='Module 15: existing cities sorted into draft or live. A paused city with '
                       'an active zone was bookable before, so it is live; one without could '
                       'never take a booking, so it is a draft.')
    except Exception:  # noqa: BLE001 - an audit hiccup must not fail a migration
        import logging
        logging.getLogger(__name__).exception('city status backfill: audit rows not written')


class Migration(migrations.Migration):

    dependencies = [
        ('admin_access', '0001_initial'),
        ('pricing', '0017_backfill_zone_revisions'),
        ('admin_audit', '0006_actor_snapshot'),
    ]

    operations = [
        migrations.AlterField(
            model_name='geonode',
            name='status',
            field=models.CharField(
                choices=[('draft', 'Draft'), ('active', 'Live'), ('paused', 'Paused')],
                default='draft', max_length=10),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
