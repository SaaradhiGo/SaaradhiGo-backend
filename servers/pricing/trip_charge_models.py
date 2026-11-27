"""Trip extra charges catalogue (owner brief, 26 Nov): tolls, tolled roads, airport and venue fees,
waiting and interstate permits, per place, ops-entered and versioned like every pricing setting.

A row is one version of one item (`code`); a change archives the active version and writes the next
one from its start date. Amounts are per class in minor units, per crossing, per km or per minute.
"""

from django.db import models

ACTIVE, ARCHIVED = 'active', 'archived'

TOLL_PLAZA = 'toll_plaza'      # a point; charged once when the planned route passes it
TOLLED_ROAD = 'tolled_road'    # a path; charged per km driven inside its corridor (Hyderabad ORR)
AIRPORT = 'airport'            # a polygon; charged on a pickup and/or drop inside it
VENUE = 'venue'                # a polygon, as an airport
WAITING = 'waiting'            # no geometry; free minutes, then a rate per minute (rider-caused)
PERMIT = 'permit'              # a polygon (the home state); charged when the drop is outside it
KINDS = (TOLL_PLAZA, TOLLED_ROAD, AIRPORT, VENUE, WAITING, PERMIT)
TOLL_KINDS = (TOLL_PLAZA, TOLLED_ROAD)

RIDER, DRIVER, PLATFORM = 'rider', 'driver', 'platform'
WHO_PAYS = (RIDER, DRIVER, PLATFORM)

PICKUP, DROP, PICKUP_OR_DROP = 'pickup', 'drop', 'pickup_or_drop'


class TripChargeItem(models.Model):
    country = models.CharField(max_length=2)
    #: The place that owns it (who may edit it); where it applies is its geometry.
    scope_code = models.CharField(max_length=40, blank=True, default='')
    code = models.SlugField(max_length=60)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=10, default=ACTIVE)
    kind = models.CharField(max_length=16)
    name = models.CharField(max_length=120)
    #: GeoJSON geometry ([lng, lat] order): Point, LineString or Polygon; {} for waiting.
    geometry = models.JSONField(default=dict, blank=True)
    radius_m = models.PositiveIntegerField(default=0)
    corridor_m = models.PositiveIntegerField(default=0)
    #: Airports and venues: pickup, drop or pickup_or_drop.
    applies_on = models.CharField(max_length=16, blank=True, default='')
    who_pays = models.CharField(max_length=10, default=RIDER)
    currency = models.CharField(max_length=3)
    #: {vehicle_type: minor}; per crossing, per km (tolled road) or per minute (waiting).
    amounts = models.JSONField(default=dict)
    free_minutes = models.PositiveSmallIntegerField(default=0)
    #: Permits: who the collected amount is remitted to.
    remit_to = models.CharField(max_length=120, blank=True, default='')
    #: A QA sample value, not a real figure: shown as such everywhere.
    sample = models.BooleanField(default=False)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['country', 'code', 'version'],
                                               name='trip_charge_version_unique')]
        indexes = [models.Index(fields=['country', 'status'], name='trip_charge_country_status')]

    def __str__(self):
        return f'TripChargeItem {self.country} {self.code} v{self.version} {self.kind}'
