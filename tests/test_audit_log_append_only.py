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


# ---------------------------------------------------------------------------
# Tamper evidence
# ---------------------------------------------------------------------------
#
# The trigger stops mistakes and ordinary code. It cannot stop an actor
# holding the application's own database credentials: the app connects as
# the OWNER, and an owner can `ALTER TABLE ... DISABLE TRIGGER`. The real
# fix is a separate non-owner role (AUDIT.md §14, production blocker).
#
# Until then, every row commits to the whole history before it, so tampering
# cannot be silent. These tests tamper through a RAW CURSOR, which is
# exactly the path that bypasses both the model guard and (on SQLite) any
# trigger -- i.e. they simulate the attacker the chain is actually for.

from servers.admin_audit.hashchain import GENESIS, verify_chain


def _write(action, target_id):
    return AdminAuditLog.objects.create(
        actor_label='ops@saaradhigo.com', action=action,
        target_type='driver', target_id=str(target_id),
        before={'approved': False}, after={'approved': True},
        reason='Documents verified.',
    )


class TestTheChainIsBuilt:
    def test_the_first_row_links_to_genesis(self):
        first = _write('kyc_update', 1)

        assert first.previous_hash == GENESIS
        assert len(first.content_hash) == 64

    def test_each_row_links_to_the_one_before(self):
        a = _write('kyc_update', 1)
        b = _write('kyc_update', 2)
        c = _write('withdrawal_approve', 3)

        assert b.previous_hash == a.content_hash
        assert c.previous_hash == b.content_hash

    def test_a_clean_log_verifies(self):
        for i in range(5):
            _write('kyc_update', i)

        checked, problems = verify_chain(AdminAuditLog.objects.all())

        assert checked == 5
        assert problems == []

    def test_an_empty_log_verifies(self):
        checked, problems = verify_chain(AdminAuditLog.objects.all())

        assert checked == 0
        assert problems == []


class TestTamperingIsDetected:
    def test_an_edited_row_is_caught(self):
        """Edited through a raw cursor -- the path that bypasses the model."""
        rows = [_write('kyc_update', i) for i in range(4)]
        victim = rows[1]

        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE admin_audit_adminauditlog SET reason = %s WHERE id = %s',
                ['nothing to see here', victim.pk],
            )

        checked, problems = verify_chain(AdminAuditLog.objects.all())

        assert checked == 4
        assert any(p['id'] == victim.pk and p['issue'] == 'content altered'
                   for p in problems), problems

    def test_a_removed_row_is_caught(self):
        rows = [_write('kyc_update', i) for i in range(4)]
        removed = rows[1]

        with connection.cursor() as cursor:
            cursor.execute(
                'DELETE FROM admin_audit_adminauditlog WHERE id = %s',
                [removed.pk],
            )

        checked, problems = verify_chain(AdminAuditLog.objects.all())

        assert checked == 3
        # The row after the hole now links to a predecessor that is gone.
        assert any(p['issue'] == 'broken link' for p in problems), problems

    def test_the_damage_is_located_not_just_reported(self):
        """One edit must not make every later row look broken.

        The walk continues from what each row actually stores, so the report
        points at the row that changed rather than at everything after it.
        """
        rows = [_write('kyc_update', i) for i in range(6)]
        victim = rows[2]

        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE admin_audit_adminauditlog SET reason = %s WHERE id = %s',
                ['tampered', victim.pk],
            )

        _, problems = verify_chain(AdminAuditLog.objects.all())

        altered = [p for p in problems if p['issue'] == 'content altered']
        assert len(altered) == 1
        assert altered[0]['id'] == victim.pk


class TestTheChainSurvivesOrdinaryUse:
    def test_json_field_ordering_does_not_break_the_hash(self):
        """Two equal dicts built in different orders must hash the same.

        Without sorted keys this fails intermittently and looks like
        tampering, which would make the whole mechanism untrustworthy.
        """
        entry = AdminAuditLog.objects.create(
            actor_label='ops@saaradhigo.com', action='config_change',
            target_type='rate_card', target_id='9',
            before={'b': 2, 'a': 1}, after={'a': 1, 'b': 2},
        )

        _, problems = verify_chain(AdminAuditLog.objects.filter(pk=entry.pk))

        assert problems == []

    def test_a_row_with_empty_optional_fields_verifies(self):
        entry = AdminAuditLog.objects.create(
            action='logout', target_type='session', target_id='',
        )

        _, problems = verify_chain(AdminAuditLog.objects.filter(pk=entry.pk))

        assert problems == []


class TestTheCommand:
    def test_it_passes_on_a_clean_log(self):
        from django.core.management import call_command

        _write('kyc_update', 1)
        call_command('verify_audit_chain')  # no SystemExit

    def test_it_exits_non_zero_when_broken(self):
        from django.core.management import call_command

        entry = _write('kyc_update', 1)
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE admin_audit_adminauditlog SET reason = %s WHERE id = %s',
                ['tampered', entry.pk],
            )

        with pytest.raises(SystemExit) as exit_info:
            call_command('verify_audit_chain')

        assert exit_info.value.code == 1


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
class TestTruncateIsAlsoRefused:
    """Row-level triggers do NOT fire on TRUNCATE.

    Without a statement-level trigger the entire log could be erased in one
    statement by the credentials the application already holds.

    `transaction=True` MATTERS HERE. With the usual wrapped-transaction
    fixture the inserted row is uncommitted, and PostgreSQL then refuses
    TRUNCATE with "cannot TRUNCATE ... because it has pending trigger
    events" -- before our BEFORE TRUNCATE trigger is reached. The test would
    pass while proving nothing about the trigger. Committing first makes the
    statement reach the guard it is meant to exercise.
    """

    @pytest.fixture(autouse=True)
    def _let_teardown_clean_up(self):
        """Stand the trigger down after the test so the suite can tidy up.

        pytest-django's `transaction=True` teardown TRUNCATEs every table,
        and our guard blocks that too -- correctly. Disabling it afterwards
        is the only way this test can run.

        It also demonstrates the weakness the hash chain exists for: the
        application connects as the database OWNER, and an owner can turn
        the guard off in one statement. That is why tamper EVIDENCE is not
        optional, and why a separate non-owner application role is tracked
        as a production blocker in docs/admin/AUDIT.md §14.
        """
        # Re-enable first: the previous test's teardown left it off, and a
        # test that silently ran without the guard would prove nothing.
        with connection.cursor() as cursor:
            cursor.execute(
                'ALTER TABLE admin_audit_adminauditlog '
                'ENABLE TRIGGER saaradhigo_audit_no_truncate'
            )
        yield
        with connection.cursor() as cursor:
            cursor.execute(
                'ALTER TABLE admin_audit_adminauditlog '
                'DISABLE TRIGGER saaradhigo_audit_no_truncate'
            )

    def test_truncate_is_rejected(self):
        from django.db.utils import IntegrityError

        AdminAuditLog.objects.create(
            actor_label='ops@saaradhigo.com', action='kyc_update',
            target_type='driver', target_id='7',
        )

        with pytest.raises(IntegrityError, match='append-only'):
            with connection.cursor() as cursor:
                cursor.execute('TRUNCATE TABLE admin_audit_adminauditlog')

    def test_the_log_survives_the_attempt(self):
        AdminAuditLog.objects.create(
            actor_label='ops@saaradhigo.com', action='kyc_update',
            target_type='driver', target_id='8',
        )

        try:
            with connection.cursor() as cursor:
                cursor.execute('TRUNCATE TABLE admin_audit_adminauditlog')
        except Exception:
            pass

        assert AdminAuditLog.objects.filter(target_id='8').exists()
