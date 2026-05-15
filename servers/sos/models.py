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

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['trip', '-created_at']),
        ]

    def __str__(self):
        return f'SOS#{self.id} [{self.status}] {self.event_type} by {self.user_label or "?"}'


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
