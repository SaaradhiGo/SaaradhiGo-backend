"""Write revision v1 for every existing zone, so history and rollback have a start.

One `ServiceZoneRevision` (version 1, kind `backfill`) per zone that has none, snapshotting
the zone exactly as it is. The actor is the label `system:migration`: nobody proposed or
approved it, and the row says so.

NOT AN AdminAuditLog ROW, ON PURPOSE. A migration holds historical models; a row written
through a historical `AdminAuditLog` would carry no hash and break `verify_audit_chain`
for every row after it (the same documented exception as `admin_access.linking`). The
revision row IS the record: it carries the label, the time and the full snapshot.

Idempotent (a zone that already has a revision is skipped) and additive: the reverse does
nothing, because a revision is evidence and the schema migration before this one drops the
table if it is ever rolled back.
"""

from django.db import migrations
from django.utils import timezone

MIGRATION_ACTOR = 'system:migration'
FIELDS = (
    'code', 'name', 'country', 'state_code', 'city', 'zone_type', 'priority',
    'polygon_geojson', 'currency', 'timezone_name', 'distance_unit', 'tax_mode',
    'per_trip_fee', 'per_trip_fee_label', 'is_active', 'metadata',
)


def backfill(apps, schema_editor):
    ServiceZone = apps.get_model('pricing', 'ServiceZone')
    Revision = apps.get_model('pricing', 'ServiceZoneRevision')
    now = timezone.now()
    for zone in ServiceZone.objects.all().order_by('id'):
        if Revision.objects.filter(zone=zone).exists():
            continue
        snapshot = {}
        for f in FIELDS:
            v = getattr(zone, f)
            snapshot[f] = str(v) if hasattr(v, 'as_tuple') else v
        parent = zone.parent
        node = zone.geo_node
        snapshot['parent'] = parent.code if parent is not None else None
        snapshot['geo_node'] = node.code if node is not None else None
        Revision.objects.create(
            zone=zone, version=1, kind='backfill', snapshot=snapshot,
            parent_code=snapshot['parent'] or '', geo_node_code=snapshot['geo_node'] or '',
            reason='Revision 1: the zone as it was when versioning began.',
            maker_label=MIGRATION_ACTOR, applied_at=now)


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0016_service_zone_revision'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
