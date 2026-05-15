"""Admin-action audit log.

Append-only record of every consequential write made by a staff user
through the admin endpoints — KYC approvals, withdrawal decisions,
driver deletes, etc. Required for fraud investigation and dispute
resolution and as a precondition for an MVA 2020 ops process.

The model is intentionally narrow and free of FKs that could CASCADE
on deletion of the affected resource. We store IDs by value so the
audit row survives even if the target (driver, withdrawal) is later
removed.
"""

from django.conf import settings
from django.db import models


class AdminAuditLog(models.Model):
    # Who acted. Nullable to survive deletion of the staff user account.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='admin_actions',
    )
    actor_label = models.CharField(
        max_length=255,
        blank=True,
        help_text='Human-readable snapshot of the actor at action time '
                  '(survives later deletion/rename of the user).',
    )

    # What action and on what target.
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=64, db_index=True)
    target_id = models.CharField(max_length=64, blank=True, db_index=True)

    # State snapshots for diff reconstruction.
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)

    # Request context.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['target_type', 'target_id']),
            models.Index(fields=['actor', '-created_at']),
        ]

    def __str__(self):
        return f'[{self.created_at}] {self.actor_label or "?"} {self.action} ' \
               f'{self.target_type}#{self.target_id}'
