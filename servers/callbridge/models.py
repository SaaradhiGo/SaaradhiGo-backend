"""One masked call on a trip: the trip event (who, when, how long). Never a phone number."""

from django.db import models


class CallSession(models.Model):
    #: 25 Nov (owner ruling): staff call a trip's rider or driver from the console, masked the same way.
    ROLES = [('rider', 'Rider'), ('driver', 'Driver'), ('staff', 'Staff')]

    trip = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='calls')
    from_role = models.CharField(max_length=6, choices=ROLES)
    #: A staff call: who was called (rider or driver) and the staff account that called.
    to_role = models.CharField(max_length=6, blank=True, default='')
    staff_id = models.BigIntegerField(null=True, blank=True)
    provider = models.CharField(max_length=20)
    #: The provider's own id for the bridge, for its status webhooks.
    provider_ref = models.CharField(max_length=100, blank=True, default='')
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    #: The bridge stops connecting after this (trip active, and 15 minutes after it ends).
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
