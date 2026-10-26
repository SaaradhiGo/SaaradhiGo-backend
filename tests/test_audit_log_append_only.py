"""The admin audit log cannot be changed or removed.

WHY THIS IS THE FIRST THING IN PHASE 1
--------------------------------------
`AdminAuditLog` is the only record of who approved a payout, who passed a
driver's KYC and who changed a price. Before this, it was an ordinary
Django model: anyone with database access — or any code path that reached
`.update()` — could rewrite it. An audit trail that can be edited is a log
file.

Both layers are asserted here:

  * the MODEL and its queryset raise `AuditLogIsAppendOnly`, which is what
    holds on SQLite (CI, local) and what makes the failure name itself at
    the call site;
  * the DATABASE rejects UPDATE and DELETE with a trigger, which is what
    holds against psql, a shell, or an ORM path nobody anticipated. That
    test is skipped on SQLite rather than silently passing, because a
    skipped test is honest and a vacuous one is not.
"""

import pytest
from django.db import connection

from servers.admin_audit.models import AdminAuditLog, AuditLogIsAppendOnly

pytestmark = pytest.mark.django_db

# The project's own convention: `-m postgres` is excluded from the default
# SQLite run by pytest.ini and executed by the dedicated PostgreSQL CI job.
# Marked rather than skipif'd so these run somewhere real instead of being
# permanently skipped.
needs_postgres = pytest.mark.postgres


@pytest.fixture
def entry():
    return AdminAuditLog.objects.create(
        actor_label='ops@saaradhigo.com',
        action='kyc_update',
        target_type='driver',
        target_id='7',
        before={'approved': False},
        after={'approved': True},
        reason='Documents verified.',
        ip_address='203.0.113.7',
    )


class TestItCanBeWrittenAndRead:
    def test_a_row_can_be_appended(self, entry):
        assert AdminAuditLog.objects.filter(pk=entry.pk).exists()

    def test_the_record_survives_intact(self, entry):
        fresh = AdminAuditLog.objects.get(pk=entry.pk)

        assert fresh.before == {'approved': False}
        assert fresh.after == {'approved': True}
        assert fresh.reason == 'Documents verified.'
        assert fresh.ip_address == '203.0.113.7'


class TestItCannotBeChanged:
    def test_saving_an_existing_row_is_refused(self, entry):
        entry.reason = 'Something else entirely.'

        with pytest.raises(AuditLogIsAppendOnly):
            entry.save()

    def test_a_queryset_update_is_refused(self, entry):
        with pytest.raises(AuditLogIsAppendOnly):
            AdminAuditLog.objects.filter(pk=entry.pk).update(reason='rewritten')

    def test_the_stored_reason_is_unchanged_after_a_refused_write(self, entry):
        try:
            AdminAuditLog.objects.filter(pk=entry.pk).update(reason='rewritten')
        except AuditLogIsAppendOnly:
            pass

        assert AdminAuditLog.objects.get(pk=entry.pk).reason == 'Documents verified.'


class TestItCannotBeRemoved:
    def test_deleting_an_instance_is_refused(self, entry):
        with pytest.raises(AuditLogIsAppendOnly):
            entry.delete()

    def test_a_queryset_delete_is_refused(self, entry):
        with pytest.raises(AuditLogIsAppendOnly):
            AdminAuditLog.objects.filter(pk=entry.pk).delete()

    def test_a_raw_delete_is_refused(self, entry):
        """`_raw_delete` is the path a cascade or a bulk cleanup would take."""
        with pytest.raises(AuditLogIsAppendOnly):
            AdminAuditLog.objects.filter(pk=entry.pk)._raw_delete(connection.alias)

    def test_the_row_is_still_there_afterwards(self, entry):
        for attempt in (
            lambda: entry.delete(),
            lambda: AdminAuditLog.objects.filter(pk=entry.pk).delete(),
        ):
            try:
                attempt()
            except AuditLogIsAppendOnly:
                pass

        assert AdminAuditLog.objects.filter(pk=entry.pk).exists()


class TestCorrectionsAreNewRows:
    def test_a_correction_is_appended_not_edited(self, entry):
        """The supported way to correct the record."""
        correction = AdminAuditLog.objects.create(
            actor_label='ops@saaradhigo.com',
            action='kyc_update_corrected',
            target_type='driver',
            target_id='7',
            before={'approved': True},
            after={'approved': False},
            reason=f'Correcting audit row {entry.pk}: approved in error.',
        )

        assert correction.pk != entry.pk
        assert AdminAuditLog.objects.filter(target_id='7').count() == 2
        # And the original still says what it originally said.
        assert AdminAuditLog.objects.get(pk=entry.pk).after == {'approved': True}


class TestTheDatabaseItselfRefuses:
    """What protects the table from psql, a shell, or an unanticipated ORM
    path.

    Marked `postgres`: excluded from the default SQLite run and executed by
    the PostgreSQL CI job, so it runs somewhere real rather than being
    permanently skipped.
    """

    @needs_postgres
    def test_raw_sql_update_is_rejected(self, entry):
        # The trigger raises with ERRCODE restrict_violation, which psycopg
        # surfaces as RestrictViolation and Django maps to IntegrityError.
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError, match='append-only'):
            with connection.cursor() as cursor:
                cursor.execute(
                    'UPDATE admin_audit_adminauditlog SET reason = %s WHERE id = %s',
                    ['rewritten by raw sql', entry.pk],
                )

    @needs_postgres
    def test_raw_sql_delete_is_rejected(self, entry):
        # The trigger raises with ERRCODE restrict_violation, which psycopg
        # surfaces as RestrictViolation and Django maps to IntegrityError.
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError, match='append-only'):
            with connection.cursor() as cursor:
                cursor.execute(
                    'DELETE FROM admin_audit_adminauditlog WHERE id = %s',
                    [entry.pk],
                )
