"""One masked call on a trip: the trip event (who, when, how long). Never a phone number."""

from django.db import models


class CallSession(models.Model):
    ROLES = [('rider', 'Rider'), ('driver', 'Driver')]

    trip = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='calls')
    from_role = models.CharField(max_length=6, choices=ROLES)
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
