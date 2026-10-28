"""Command Centre: alerts that must reach a person. The seed of the module.

WHY A TABLE AND NOT A PUSH
--------------------------
The platform already fans an SOS out as push notifications and SMS
(`sos.tasks.dispatch_sos`) and the console bell reads per-user
`rider.Notification` rows. Neither answers the question the owner asked on
2026-10-28: "which SOS could not be routed to a city, and has anybody taken
it?" A push is gone once dismissed; a per-user notification belongs to one
account. An `OperatorAlert` is a platform-level row with an acknowledgement
on it, so the Command Centre can show what is waiting on a person, and the
audit log can show who answered.

FIRST PRODUCER
--------------
`sos.anchoring.alert_unanchored`: an SOS with no trip, no usable GPS fix
and no driver home city. Such an event is already visible to every Safety
Officer regardless of scope; the alert is what makes it IMPOSSIBLE TO MISS
rather than merely possible to find.

NOT SCOPED, BY DESIGN
---------------------
An alert exists because a row could not be placed. Listing is gated on a
capability (`report.read`), acknowledging on the capability that owns the
alert's kind (`ACK_PERMISSION`), and neither is narrowed by geography. See
`admin_access.scoping.GLOBAL_BY_DESIGN`.
"""

from django.conf import settings
from django.db import models

INFO, WARNING, CRITICAL = 'info', 'warning', 'critical'
SEVERITY_CHOICES = [(INFO, 'Info'), (WARNING, 'Warning'), (CRITICAL, 'Critical')]

#: Alert kinds, and the permission that may acknowledge each. A kind that is
#: not listed cannot be acknowledged over the API -- a deliberate default,
#: because "anyone may dismiss it" is how an alert stops being one.
SOS_UNANCHORED = 'sos_unanchored'
ACK_PERMISSION = {
    SOS_UNANCHORED: 'safety.respond',
}


class OperatorAlert(models.Model):
    kind = models.CharField(max_length=40, db_index=True)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default=WARNING)
    title = models.CharField(max_length=200)
    #: Enough to open the thing the alert is about (`sos_id`, ...). Never a
    #: phone number or a name: the list is read by more roles than may see
    #: those, and the masked detail endpoints exist for the rest.
    payload = models.JSONField(default=dict, blank=True)
    #: What raised it, for de-duplication and for the console's deep link.
    source_type = models.CharField(max_length=40, blank=True, default='')
    source_id = models.CharField(max_length=64, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True, db_index=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='acknowledged_operator_alerts',
    )
    acknowledged_note = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source_type', 'source_id'], name='opalert_source_idx'),
        ]

    def __str__(self):
        state = 'open' if self.acknowledged_at is None else 'acknowledged'
        return f'Alert#{self.id} [{state}] {self.kind}: {self.title}'

    @property
    def is_open(self):
        return self.acknowledged_at is None
