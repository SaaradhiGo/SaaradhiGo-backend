"""SOS / panic event records.

Mandatory under MoRTH Motor Vehicles Aggregator Guidelines 2020: every
ride must surface an SOS / panic action that alerts the platform and the
rider's emergency contact in real time.

Both the initial event and every subsequent update (admin acknowledged,
resolved, etc.) are append-only — a rider or driver alleging the
platform suppressed an SOS must be answerable from the immutable audit.
SOSEvent rows are never deleted; SOSEventUpdate rows are insert-only.
"""

from django.conf import settings
from django.db import models

#: `anchor_source` values. See `anchoring.resolve_anchor` for the order.
ANCHOR_TRIP, ANCHOR_GPS, ANCHOR_HOME_CITY, ANCHOR_UNANCHORED = (
    'trip', 'gps', 'home_city', 'unanchored',
)
ANCHOR_SOURCE_CHOICES = [
    (ANCHOR_TRIP, "The trip's zone"),
    (ANCHOR_GPS, "The raiser's GPS position, resolved to a zone"),
    (ANCHOR_HOME_CITY, "The driver's home city"),
    (ANCHOR_UNANCHORED, 'No anchor: routed to every safety responder'),
]


class SOSEvent(models.Model):
    EVENT_TYPE_CHOICES = [
        ('panic', 'Panic'),                     # default — generic distress
        ('vehicle_breakdown', 'Vehicle breakdown'),
        ('medical', 'Medical emergency'),
        ('harassment', 'Harassment / safety concern'),
        ('accident', 'Accident'),
        ('other', 'Other'),
    ]
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('acknowledged', 'Acknowledged by ops'),
        ('resolved', 'Resolved'),
        ('false_alarm', 'False alarm'),
    ]
    INITIATED_BY_CHOICES = [
        ('rider', 'Rider'),
        ('driver', 'Driver'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sos_events',
        help_text='User who raised the event (nullable to survive user deletion).',
    )
    user_label = models.CharField(
        max_length=255,
        blank=True,
        help_text='Frozen human-readable label of the user at event time.',
    )
    initiated_by = models.CharField(max_length=10, choices=INITIATED_BY_CHOICES)
    trip = models.ForeignKey(
        'ride.Trip',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sos_events',
    )

    event_type = models.CharField(max_length=32, choices=EVENT_TYPE_CHOICES, default='panic')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open', db_index=True)

    # Where the event happened.
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    location_accuracy_m = models.FloatField(null=True, blank=True)

    note = models.TextField(blank=True, help_text='Optional free-text from the caller.')

    # Request context (helps ops triage).
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)

    # WHERE THE EVENT IS, FOR THE PEOPLE WHO ANSWER IT
    # ------------------------------------------------
    # Resolved once, when the row is created (`anchoring.resolve_anchor`):
    # the trip's zone if there is a trip, else the GPS position above
    # resolved to a city through the active ServiceZone polygons, else a
    # driver's home city. `anchor_source` says which. An event that could
    # not be placed is `unanchored`: it is visible to EVERY operator
    # holding `safety.respond` and at Global (`scoping.UNSCOPED_UNION`),
    # and raises a Command Centre alert on creation. An SOS must never be
    # invisible to the people who respond to it (owner, 2026-10-28).
    anchor_node = models.ForeignKey(
        'admin_access.GeoNode',
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name='sos_events',
        help_text='The admin-scope node this event is routed to. NULL means unanchored.',
    )
    anchor_source = models.CharField(
        max_length=12, choices=ANCHOR_SOURCE_CHOICES, blank=True, default='',
        db_index=True,
        help_text='How anchor_node was chosen. Set on creation; blank is resolved on save.',
    )

    # THE CASE FILE (safety module). Every SOS opens a `safety.SafetyIncident`
    # or joins the open one for its trip; see `safety.services.attach_sos`.
    # NULL only for a row written before the module existed or when the
    # attach itself failed -- the escalation sweep backfills those, so an SOS
    # is never left without a case.
    incident = models.ForeignKey(
        'safety.SafetyIncident', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='sos_events',
    )
    #: Set once by the escalation sweep when this SOS sat unacknowledged past
    #: its country's `sos_ack_minutes`. The conditional UPDATE that sets it is
    #: what makes the escalation fire exactly once per event.
    escalated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Names pinned to match the 0001_initial migration to keep
            # `makemigrations --check` clean.
            models.Index(fields=['status', '-created_at'], name='sos_status_created_idx'),
            models.Index(fields=['trip', '-created_at'], name='sos_trip_created_idx'),
        ]

    def __str__(self):
        return f'SOS#{self.id} [{self.status}] {self.event_type} by {self.user_label or "?"}'

    def save(self, *args, **kwargs):
        """Anchor on creation, whichever path created the row.

        Done here rather than only in `views.raise_sos` because the
        session console creates events too, and so do tests and scripts:
        an SOS that reaches the table must reach a responder, and the
        anchor is what decides which one. The alert for an unanchored
        event is written in the same transaction as the row, so there is
        no window in which the event exists and the alert does not.
        """
        creating = self._state.adding
        if creating and not self.anchor_source:
            from .anchoring import resolve_anchor

            self.anchor_node, self.anchor_source = resolve_anchor(self)
        super().save(*args, **kwargs)
        if creating and self.anchor_source == ANCHOR_UNANCHORED:
            from .anchoring import alert_unanchored

            alert_unanchored(self)
        if creating:
            # The case file. Never allowed to fail the SOS itself: the row is
            # already written, and the escalation sweep backfills a missing
            # incident.
            from servers.safety.services import attach_sos_safely

            attach_sos_safely(self)


class SOSEventUpdate(models.Model):
    """Insert-only history of changes to an SOSEvent (ack / resolve / note)."""
    event = models.ForeignKey(SOSEvent, on_delete=models.CASCADE, related_name='updates')
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    actor_label = models.CharField(max_length=255, blank=True)
    new_status = models.CharField(max_length=20, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['created_at']
