"""Pickup points, restricted areas and a service area per class (25 Nov): the model. See zone_areas.py."""

from django.db import models
from django.utils import timezone


class ZoneArea(models.Model):
    zone = models.ForeignKey('pricing.ServiceZone', on_delete=models.CASCADE, related_name='areas')
    kind = models.CharField(max_length=12)
    name = models.CharField(max_length=120)
    polygon_geojson = models.JSONField()
    #: pickup: [{"name": "Gate 3", "lat": 17.24, "lng": 78.43}]
    points = models.JSONField(default=list, blank=True)
    #: class_area: the class it applies to ("sedan").
    vehicle_type = models.CharField(max_length=50, blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'pricing'
        ordering = ['zone_id', 'kind', 'name']

    def as_dict(self):
        return {'id': self.id, 'zone': self.zone_id, 'kind': self.kind, 'name': self.name,
                'polygon_geojson': self.polygon_geojson, 'points': self.points, 'vehicle_type': self.vehicle_type,
                'is_active': self.is_active, 'created_by': self.created_by_label, 'reason': self.reason,
                'created_at': self.created_at.isoformat()}
