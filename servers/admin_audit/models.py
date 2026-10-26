"""Admin-action audit log.

Append-only record of every consequential write made by a staff user
through the admin endpoints — KYC approvals, withdrawal decisions,
driver deletes, etc. Required for fraud investigation and dispute
resolution and as a precondition for an MVA 2020 ops process.

"Append-only" is ENFORCED, not merely intended. PostgreSQL rejects
UPDATE and DELETE on this table with a trigger (migration 0002), and the
model and its queryset raise `AuditLogIsAppendOnly` so the same rule holds
on SQLite and so the failure names itself at the call site.

A correction is a new row. It is never an edit, because an edited audit row
is a different claim about the past and the only thing this table is for is
that nobody can make one.

The model is intentionally narrow and free of FKs that could CASCADE
on deletion of the affected resource. We store IDs by value so the
audit row survives even if the target (driver, withdrawal) is later
removed.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditLogIsAppendOnly(Exception):
    """Raised on any attempt to change or remove an audit row.

    A plain exception rather than a Django validation error: this is not a
    user correcting input, it is code doing something that must never
    happen, and it should reach a log and a human.
    """


class _AppendOnlyQuerySet(models.QuerySet):
    """A queryset that can be read and added to, and nothing else.

    The DATABASE is the real guarantee (see migration 0002: UPDATE and
    DELETE are rejected by a trigger on PostgreSQL). This layer exists so
    the same rule holds on SQLite, which CI and local development use, and
    so the failure arrives as a clear exception at the call site rather
    than as a database error three frames down.
    """

    def update(self, *args, **kwargs):
        raise AuditLogIsAppendOnly(
            'The admin audit log is append-only; rows cannot be updated.'
        )

    def delete(self, *args, **kwargs):
        raise AuditLogIsAppendOnly(
            'The admin audit log is append-only; rows cannot be deleted.'
        )

    def _raw_delete(self, *args, **kwargs):
        raise AuditLogIsAppendOnly(
            'The admin audit log is append-only; rows cannot be deleted.'
        )


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

    # `default=timezone.now`, NOT `auto_now_add`.
    #
    # auto_now_add sets the value in pre_save(), which runs during the
    # INSERT -- after the hash has been computed. The row would then store a
    # timestamp microseconds away from the one that was hashed, and every
    # row would fail verification despite nobody touching it. A default is
    # applied when the instance is built, so the hashed value and the stored
    # value are the same value. Still not editable.
    created_at = models.DateTimeField(default=timezone.now, db_index=True,
                                      editable=False)

    # --- tamper evidence (see hashchain.py) -----------------------------
    #
    # The append-only trigger stops mistakes. It does not stop an actor with
    # the application's own database credentials, who can disable it. These
    # two columns make that detectable: every row commits to the whole
    # history before it, so an edit, a removal or a reorder breaks every
    # hash that follows.
    previous_hash = models.CharField(max_length=64, blank=True, default='')
    content_hash = models.CharField(max_length=64, blank=True, default='',
                                    db_index=True)

    # Reading and appending only. See `_AppendOnlyQuerySet`.
    objects = _AppendOnlyQuerySet.as_manager()

    def _link_into_chain(self):
        """Attach this row to the end of the chain.

        Taken under `select_for_update` on the current tail, because two
        requests inserting at the same moment would otherwise read the same
        predecessor and produce two rows claiming the same place -- a fork
        that `verify_chain` would report forever after. The lock is held for
        one indexed row and released with the transaction.
        """
        from django.db import transaction

        from .hashchain import GENESIS, compute_hash

        with transaction.atomic():
            tail = (type(self).objects.select_for_update()
                    .order_by('-id').values('content_hash').first())
            self.previous_hash = (tail or {}).get('content_hash') or GENESIS
            self.content_hash = compute_hash(self.previous_hash, self)

    def save(self, *args, **kwargs):
        """Insert only.

        An audit row is a statement about something that already happened.
        Editing one is not a correction, it is a different claim about the
        past, and the whole value of this table is that nobody can make
        one. A correction is a NEW row.
        """
        if self.pk is not None and not kwargs.get('force_insert'):
            raise AuditLogIsAppendOnly(
                'The admin audit log is append-only; row '
                f'{self.pk} cannot be modified.'
            )
        if not self.content_hash:
            # `created_at` is auto_now_add and therefore not set until the
            # INSERT. It is part of the hashed content, so it is filled in
            # here rather than left for the database to decide, or the row
            # would hash to something different from what it stores.
            if self.created_at is None:
                self.created_at = timezone.now()
            self._link_into_chain()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogIsAppendOnly(
            'The admin audit log is append-only; row '
            f'{self.pk} cannot be deleted.'
        )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Names pinned explicitly so they match the 0001_initial migration;
            # without name=, Django auto-generates a hashed name and
            # `makemigrations --check` produces phantom rename migrations.
            models.Index(fields=['target_type', 'target_id'], name='admin_audit_target_idx'),
            models.Index(fields=['actor', '-created_at'], name='admin_audit_actor_idx'),
        ]

    def __str__(self):
        return f'[{self.created_at}] {self.actor_label or "?"} {self.action} ' \
               f'{self.target_type}#{self.target_id}'
