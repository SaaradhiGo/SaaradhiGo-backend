"""Versioned history of a service zone (module 9).

Every APPLIED change to a zone writes one `ServiceZoneRevision`: a full snapshot of
the zone as it became, with the version number, the reason, who proposed it (maker),
who approved it (checker) and when it was applied. History and rollback both read
this table: rolling back is "propose restoring revision N", which is an ordinary
`zone.change` request and so needs a checker like any other change.

Append-only. A revision is evidence of what a zone looked like on a given day (and so
of which zone a pickup belonged to), so neither `save()` on an existing row nor
`delete()` is allowed. The zone itself is never deleted (trips and rate cards point
at it); deactivating it is a revision too.
"""

from django.db import models
from django.utils import timezone

#: The zone columns a revision snapshots, and a restore writes back. `is_active` is
#: part of the snapshot (a deactivation is a revision) but a plain restore of a
#: revision never silently reactivates a zone: see `zone_workflow.propose_restore`.
SNAPSHOT_FIELDS = (
    'code', 'name', 'country', 'state_code', 'city', 'zone_type', 'priority',
    'polygon_geojson', 'currency', 'timezone_name', 'distance_unit', 'tax_mode',
    'per_trip_fee', 'per_trip_fee_label', 'is_active', 'metadata',
)

KIND_CREATE = 'create'
KIND_UPDATE = 'update'
KIND_DEACTIVATE = 'deactivate'
KIND_REACTIVATE = 'reactivate'
KIND_RESTORE = 'restore'
KIND_BACKFILL = 'backfill'
KIND_CHOICES = [
    (KIND_CREATE, 'Created'), (KIND_UPDATE, 'Changed'), (KIND_DEACTIVATE, 'Deactivated'),
    (KIND_REACTIVATE, 'Reactivated'), (KIND_RESTORE, 'Restored from a revision'),
    (KIND_BACKFILL, 'Backfilled (existing zone)'),
]

MIGRATION_ACTOR = 'system:migration'


class ServiceZoneRevision(models.Model):
    zone = models.ForeignKey('pricing.ServiceZone', on_delete=models.PROTECT,
                             related_name='revisions')
    version = models.PositiveIntegerField()
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    #: Every zone field, plus the two relations as codes (the ids can be re-pointed).
    snapshot = models.JSONField()
    parent_code = models.CharField(max_length=64, blank=True, default='')
    geo_node_code = models.CharField(max_length=32, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    #: Set for a restore: the version whose values were put back.
    restored_from_version = models.PositiveIntegerField(null=True, blank=True)

    # Maker and checker as snapshots, like an audit row: they survive the account.
    maker_id = models.BigIntegerField(null=True, blank=True)
    maker_label = models.CharField(max_length=255, blank=True, default='')
    maker_email = models.CharField(max_length=254, blank=True, default='')
    checker_id = models.BigIntegerField(null=True, blank=True)
    checker_label = models.CharField(max_length=255, blank=True, default='')
    checker_email = models.CharField(max_length=254, blank=True, default='')
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    applied_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['zone_id', '-version']
        constraints = [
            models.UniqueConstraint(fields=['zone', 'version'], name='zone_revision_version_unique'),
        ]

    def __str__(self):
        return f'{self.zone_id} v{self.version} {self.kind}'

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise PermissionError('A zone revision is append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError('A zone revision is append-only.')

    def as_dict(self, *, with_polygon=False):
        snap = dict(self.snapshot or {})
        polygon = snap.get('polygon_geojson')
        if not with_polygon:
            snap.pop('polygon_geojson', None)
        return {
            'id': self.id, 'version': self.version, 'kind': self.kind,
            'snapshot': snap,
            'vertex_count': _vertex_count(polygon),
            'parent': self.parent_code or None, 'geo_node': self.geo_node_code or None,
            'reason': self.reason, 'restored_from_version': self.restored_from_version,
            'maker': {'id': self.maker_id, 'label': self.maker_label,
                      'email': self.maker_email},
            'checker': {'id': self.checker_id, 'label': self.checker_label,
                        'email': self.checker_email} if self.checker_label else None,
            'approval_request_id': self.approval_request_id,
            'applied_at': self.applied_at.isoformat(),
        }


def _vertex_count(polygon):
    try:
        if polygon.get('type') == 'Polygon':
            return sum(max(len(r) - 1, 0) for r in polygon['coordinates'])
        return sum(max(len(r) - 1, 0) for part in polygon['coordinates'] for r in part)
    except (AttributeError, KeyError, TypeError):
        return 0
