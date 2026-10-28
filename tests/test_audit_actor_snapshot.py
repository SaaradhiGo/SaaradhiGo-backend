"""The audit actor is a snapshot, and an actor's account is never deleted.

THE FAILURE THIS CLOSES
-----------------------
`AdminAuditLog.actor` was a FK with `on_delete=SET_NULL`. Deleting a staff
account therefore UPDATEd every row they had written -- and on PostgreSQL
the append-only trigger refused the UPDATE, so the delete raised from inside
the cascade. Where it did not raise, it blanked the author of an audit row
on request, which is the opposite of what the log is for.

THE RULING (owner, 2026-10-27)
------------------------------
Store the actor as a snapshot (id with no FK, email, role at write time).
Exempt no column from the append-only trigger. Staff are deactivated, never
deleted; block deletion of any user who appears as an actor. Rider and
driver erasure requests anonymise the row instead (`test_anonymise_user.py`).
Keep `verify_audit_chain` green.

HASH-CHAIN COMPATIBILITY
------------------------
The two new columns are hashed, but as OPTIONAL fields: they enter the
canonical form only when they hold a value, so a row from before migration
0006 -- which the migration leaves empty -- hashes exactly as it did when
written. The vectors in `TestOldRowsStillHash` were computed with the code
AS IT STOOD BEFORE this change; `_old_canonical` below reimplements that
code independently of `hashchain.py`, so the test cannot drift with it.
"""

import datetime
import hashlib
import json
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, connection
from django.test import RequestFactory

from servers.admin_audit.context import audit_context
from servers.admin_audit.hashchain import (
    GENESIS,
    HASHED_FIELDS,
    OPTIONAL_FIELDS,
    canonical_content,
    compute_hash,
    inspect_chain,
    verify_chain,
    write_anchor,
)
from servers.admin_audit.models import AdminAuditLog, actor_role_of, actor_snapshot
from servers.admin_audit.services import record_admin_action
from servers.auth_user.erasure import AccountDeletionRefused

pytestmark = pytest.mark.django_db

User = get_user_model()
TABLE = 'admin_audit_adminauditlog'
UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _operator(phone='+919700000101', email='ops@saaradhigo.test',
              full_name='Ops Person', ops_role='finance', **extra):
    return User.objects.create_user(
        phone_number=phone, email=email, full_name=full_name, role='admin',
        is_staff=True, ops_role=ops_role, password='x', **extra,
    )


def _rider(phone='+919700000201', email='rider@saaradhigo.test',
           full_name='A Rider'):
    return User.objects.create_user(
        phone_number=phone, email=email, full_name=full_name, role='rider',
    )


def _write(actor=None, action='kyc_update', target_id='7', **extra):
    return AdminAuditLog.objects.create(
        actor=actor, action=action, target_type='driver',
        target_id=str(target_id), before={'approved': False},
        after={'approved': True}, reason='Documents verified.', **extra,
    )


def _raw(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


# The hash as computed BEFORE migration 0006: eleven fields, every one
# present, `None` as the empty string. Written out rather than imported so a
# change to `hashchain.py` cannot quietly move the goalposts.
_OLD_FIELDS = ('actor_id', 'actor_label', 'action', 'target_type',
               'target_id', 'before', 'after', 'reason', 'ip_address',
               'user_agent', 'created_at')


def _old_normalise(value):
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          default=str)
    return str(value)


def _old_canonical(entry):
    parts = {f: _old_normalise(getattr(entry, f, None)) for f in _OLD_FIELDS}
    return json.dumps(parts, sort_keys=True, separators=(',', ':'))


def _old_hash(previous, entry):
    digest = hashlib.sha256()
    digest.update((previous or GENESIS).encode('utf-8'))
    digest.update(_old_canonical(entry).encode('utf-8'))
    return digest.hexdigest()


def _legacy_hashed_rows(n, actor_id=None, start=0):
    """`n` rows exactly as the code before 0006 wrote them.

    `bulk_create` skips `save()` and therefore the model's own chaining, so
    the hashes here come from `_old_hash` and nothing else -- the row is
    what a database that was migrated with rows in it actually holds:
    hashed by the old algorithm, `actor_email`/`actor_role` empty.
    """
    rows = []
    previous = (AdminAuditLog.objects.order_by('-id')
                .values_list('content_hash', flat=True).first()) or GENESIS
    for i in range(n):
        row = AdminAuditLog(
            actor_id=actor_id, actor_label='ops@saaradhigo.com',
            action='kyc_update', target_type='driver',
            target_id=str(start + i), before={'approved': False},
            after={'approved': True}, reason='Documents verified (pre-0006).',
            ip_address='203.0.113.7',
            created_at=datetime.datetime(2026, 10, 1, 9, 0, i, tzinfo=UTC),
        )
        row.previous_hash = previous
        row.content_hash = _old_hash(previous, row)
        previous = row.content_hash
        rows.append(row)
    AdminAuditLog.objects.bulk_create(rows)
    assert all(r.actor_email == '' and r.actor_role == '' for r in rows)
    return rows


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------

class TestTheSnapshotIsFilledFromTheActor:
    def test_create_with_actor_fills_all_four_columns(self):
        operator = _operator()

        row = _write(actor=operator)
        fresh = AdminAuditLog.objects.get(pk=row.pk)

        assert fresh.actor_id == operator.pk
        assert fresh.actor_label == 'Ops Person'
        assert fresh.actor_email == 'ops@saaradhigo.test'
        assert fresh.actor_role == 'admin/finance'

    def test_an_explicit_label_wins_over_the_derived_one(self):
        operator = _operator()

        row = _write(actor=operator, actor_label='Named by the caller')

        assert row.actor_label == 'Named by the caller'
        assert row.actor_id == operator.pk
        assert row.actor_email == 'ops@saaradhigo.test'

    def test_a_rider_role_is_plain(self):
        rider = _rider()

        assert actor_role_of(rider) == 'rider'
        assert _write(actor=rider).actor_role == 'rider'

    def test_a_superuser_without_ops_role_is_still_described(self):
        boss = User.objects.create_user(
            phone_number='+919700000102', email='root@saaradhigo.test',
            role='admin', is_superuser=True, ops_role='',
        )

        assert actor_role_of(boss) == 'admin'

    def test_no_actor_leaves_the_snapshot_empty(self):
        row = _write(actor=None, actor_label='some command')

        assert row.actor_id is None
        assert row.actor_email == ''
        assert row.actor_role == ''
        assert row.actor_label == 'some command'

    def test_an_anonymous_user_is_nobody(self):
        from django.contrib.auth.models import AnonymousUser

        assert actor_snapshot(AnonymousUser())['actor_id'] is None

    def test_record_admin_action_snapshots_the_request_user(self):
        operator = _operator()
        request = RequestFactory().post('/x', REMOTE_ADDR='203.0.113.9')
        request.user = operator

        row = record_admin_action(request, action='kyc_approved',
                                  target_type='driver', target_id=3)

        assert (row.actor_id, row.actor_email, row.actor_role) == (
            operator.pk, 'ops@saaradhigo.test', 'admin/finance')
        assert row.ip_address == '203.0.113.9'

    def test_the_access_receivers_snapshot_the_context_actor(self):
        boss = _operator(phone='+919700000103', email='boss@saaradhigo.test',
                         full_name='The Boss', ops_role='admin')
        target = _rider()

        with audit_context(actor=boss, reason='Promoted to operator'):
            target.is_staff = True
            target.role = 'admin'
            target.save()

        row = AdminAuditLog.objects.filter(action='operator_authority_changed',
                                           target_id=str(target.pk)).get()
        assert row.actor_id == boss.pk
        assert row.actor_email == 'boss@saaradhigo.test'
        assert row.actor_role == 'admin/admin'
        assert row.actor_label == 'The Boss'

    def test_the_actor_property_is_not_a_database_lookup(self):
        """A listing of a thousand rows must not become a thousand queries."""
        operator = _operator()
        row = _write(actor=operator)

        fresh = AdminAuditLog.objects.get(pk=row.pk)

        assert fresh.actor is None
        assert fresh.actor_id == operator.pk
        assert row.actor is operator  # the transient object, on the writer's side

    def test_there_is_no_foreign_key_on_the_model(self):
        field = AdminAuditLog._meta.get_field('actor_id')

        assert field.remote_field is None
        assert field.get_internal_type() == 'BigIntegerField'
        assert field.column == 'actor_id'


# ---------------------------------------------------------------------------
# The row survives whatever happens to the account
# ---------------------------------------------------------------------------

class TestTheRowSurvivesTheActor:
    def test_deactivating_the_actor_changes_nothing_on_the_row(self):
        operator = _operator()
        row = _write(actor=operator)

        operator.is_active = False
        operator.save()

        fresh = AdminAuditLog.objects.get(pk=row.pk)
        assert fresh.actor_id == operator.pk
        assert fresh.actor_email == 'ops@saaradhigo.test'
        assert verify_chain(AdminAuditLog.objects.all())[1] == []

    def test_renaming_the_actor_leaves_the_snapshot_as_it_was(self):
        operator = _operator()
        row = _write(actor=operator)

        operator.full_name = 'Someone Else'
        operator.email = 'other@saaradhigo.test'
        operator.ops_role = 'support'
        operator.save()

        fresh = AdminAuditLog.objects.get(pk=row.pk)
        assert fresh.actor_label == 'Ops Person'
        assert fresh.actor_email == 'ops@saaradhigo.test'
        assert fresh.actor_role == 'admin/finance'


# ---------------------------------------------------------------------------
# Deletion is refused
# ---------------------------------------------------------------------------

class TestDeletingAnActorIsRefused:
    def test_instance_delete_is_refused_and_nothing_is_removed(self):
        rider = _rider()
        row = _write(actor=rider)

        with pytest.raises(AccountDeletionRefused, match='appears as the actor'):
            rider.delete()

        assert User.objects.filter(pk=rider.pk).exists()
        assert AdminAuditLog.objects.filter(pk=row.pk).exists()

    def test_queryset_delete_is_refused_too(self):
        rider = _rider()
        _write(actor=rider)

        with pytest.raises(AccountDeletionRefused):
            User.objects.filter(pk=rider.pk).delete()

        assert User.objects.filter(pk=rider.pk).exists()

    def test_the_refusal_is_a_protected_error(self):
        """So the Django admin and anything handling PROTECT cascades cope."""
        rider = _rider()
        _write(actor=rider)

        with pytest.raises(IntegrityError) as caught:
            rider.delete()

        assert caught.value.protected_objects
        assert all(isinstance(o, AdminAuditLog) for o in caught.value.protected_objects)

    def test_the_message_points_at_anonymisation(self):
        rider = _rider()
        _write(actor=rider)

        with pytest.raises(AccountDeletionRefused, match='anonymise_user'):
            rider.delete()

    def test_a_cascade_is_caught_by_the_pre_delete_backstop(self):
        """A delete that reaches the collector without going through
        `customUser.delete()` or the user queryset -- a cascade from another
        model -- is refused by the receiver, inside the collector's
        transaction, which a savepoint isolates here."""
        from django.db import transaction
        from django.db.models.deletion import Collector

        rider = _rider()
        _write(actor=rider)
        collector = Collector(using='default')
        collector.collect([rider])

        with pytest.raises(AccountDeletionRefused):
            with transaction.atomic():
                collector.delete()

        assert User.objects.filter(pk=rider.pk).exists()

    def test_a_rider_who_never_acted_can_still_be_deleted(self):
        """The boundary of the rule, stated: it protects actors and staff.

        An erasure REQUEST for a rider is anonymised by process (the service
        and the command), not by making every rider row undeletable.
        """
        rider = _rider()

        rider.delete()

        assert not User.objects.filter(pk=rider.pk).exists()


class TestDeletingStaffIsRefused:
    def test_a_staff_user_with_no_audit_rows_is_refused(self):
        operator = _operator()
        assert not AdminAuditLog.objects.filter(actor_id=operator.pk).exists()

        with pytest.raises(AccountDeletionRefused, match='deactivated, never deleted'):
            operator.delete()

        assert User.objects.filter(pk=operator.pk).exists()

    def test_the_queryset_path_is_refused_for_staff(self):
        operator = _operator()

        with pytest.raises(AccountDeletionRefused):
            User.objects.filter(is_staff=True).delete()

        assert User.objects.filter(pk=operator.pk).exists()

    def test_a_superuser_is_staff_for_this_purpose(self):
        boss = User.objects.create_user(
            phone_number='+919700000104', role='rider', is_superuser=True,
        )

        with pytest.raises(AccountDeletionRefused):
            boss.delete()

    def test_deactivation_is_the_supported_path(self):
        operator = _operator()

        operator.is_active = False
        operator.save()

        assert not User.objects.get(pk=operator.pk).is_active

    def test_nothing_cascades_when_the_delete_is_refused(self):
        """The refusal aborts the collector, so a cascade cannot half-run."""
        from servers.admin_access.models import AdminAssignment
        from tests.conftest import grant_legacy_equivalent

        operator = _operator()
        grant_legacy_equivalent(operator, ops_role='finance')
        assignments = AdminAssignment.objects.filter(admin=operator).count()
        assert assignments >= 1
        before = AdminAuditLog.objects.count()

        with pytest.raises(AccountDeletionRefused):
            operator.delete()

        assert AdminAssignment.objects.filter(admin=operator).count() == assignments
        assert AdminAuditLog.objects.count() == before


# ---------------------------------------------------------------------------
# Hash-chain compatibility
# ---------------------------------------------------------------------------

class TestOldRowsStillHash:
    """Vectors computed with hashchain.py as it stood BEFORE migration 0006."""

    def _vector_row(self):
        row = AdminAuditLog(
            actor_id=42, actor_label='Srini (ops)', action='kyc_approved',
            target_type='driver', target_id='7',
            before={'approved': False}, after={'approved': True, 'note': 'ok'},
            reason='Documents verified.', ip_address='203.0.113.7',
            user_agent='pytest/1.0',
            created_at=datetime.datetime(2026, 10, 27, 12, 34, 56, 789000, tzinfo=UTC),
        )
        return row

    def test_the_canonical_form_of_an_old_shaped_row_is_byte_identical(self):
        row = self._vector_row()

        assert canonical_content(row) == (
            '{"action":"kyc_approved","actor_id":"42","actor_label":"Srini (ops)",'
            '"after":"{\\"approved\\":true,\\"note\\":\\"ok\\"}",'
            '"before":"{\\"approved\\":false}",'
            '"created_at":"2026-10-27T12:34:56.789000+00:00",'
            '"ip_address":"203.0.113.7","reason":"Documents verified.",'
            '"target_id":"7","target_type":"driver","user_agent":"pytest/1.0"}'
        )
        assert canonical_content(row) == _old_canonical(row)

    def test_the_hash_of_an_old_shaped_row_is_unchanged(self):
        row = self._vector_row()

        assert compute_hash(GENESIS, row) == (
            '1df7b592a6ff5016db95f34a23a6d3cc3ad4c7464700a0905b363915ce6e713d')
        assert compute_hash('ab' * 32, row) == (
            '63df9770095415f569fb286a1ffb2fbc721183381016417f718dc82efe1f421b')

    def test_a_row_with_every_optional_field_empty_is_unchanged(self):
        row = AdminAuditLog(
            action='logout', target_type='session', target_id='',
            created_at=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

        assert compute_hash(GENESIS, row) == (
            'f45474eab64547756c264cbf8416a826555f618c14268eceea594d7650901fc5')

    def test_a_historical_model_without_the_columns_hashes_the_same(self):
        """What the verifier sees on a half-migrated database, and what
        migration 0005's `write_anchor` sees through `apps.get_model`."""
        class Historical:  # no actor_email / actor_role attributes at all
            pass

        old = Historical()
        for name in _OLD_FIELDS:
            setattr(old, name, getattr(self._vector_row(), name))

        assert canonical_content(old) == canonical_content(self._vector_row())

    def test_the_new_columns_are_hashed_when_present(self):
        row = self._vector_row()
        row.actor_email = 'srini@saaradhigo.test'
        row.actor_role = 'admin/admin'

        canon = canonical_content(row)

        assert '"actor_email":"srini@saaradhigo.test"' in canon
        assert '"actor_role":"admin/admin"' in canon
        assert compute_hash(GENESIS, row) != (
            '1df7b592a6ff5016db95f34a23a6d3cc3ad4c7464700a0905b363915ce6e713d')

    def test_the_field_lists_say_what_this_file_assumes(self):
        assert HASHED_FIELDS[:11] == _OLD_FIELDS
        assert set(HASHED_FIELDS[11:]) == OPTIONAL_FIELDS == {'actor_email', 'actor_role'}


class TestTheChainSpansTheMigration:
    def test_rows_hashed_by_the_old_code_then_new_rows_verify(self):
        # A rider, not an operator: creating an operator writes its own
        # `operator_account_created` row, which would sit before the
        # "legacy" rows and muddy the count.
        actor = _rider()
        _legacy_hashed_rows(4, actor_id=actor.pk)

        _write(actor=actor, target_id=10)
        _write(actor=None, actor_label='a command', target_id=11)
        _write(actor=actor, target_id=12)

        report = inspect_chain(AdminAuditLog.objects.all())

        assert report['problems'] == []
        assert report['checked'] == 7
        assert report['pre_chain'] is None  # all seven are hashed rows

    def test_pre_chain_rows_and_an_anchor_still_work_with_the_new_columns(self):
        """The 0005 shape -- unhashed rows, anchor, hashed rows -- on top of
        the 0006 columns. `write_anchor` no longer passes `actor=`."""
        unhashed = AdminAuditLog.objects.bulk_create([
            AdminAuditLog(actor_label='pre-chain', action='kyc_update',
                          target_type='driver', target_id=str(i))
            for i in range(3)
        ])
        assert all(r.content_hash == '' for r in unhashed)
        anchor = write_anchor(AdminAuditLog, label='test anchor')
        _write(actor=_operator(), target_id=9)

        report = inspect_chain(AdminAuditLog.objects.all())

        assert report['problems'] == []
        assert report['pre_chain']['anchor_id'] == anchor.pk
        assert anchor.actor_id is None and anchor.actor_email == ''

    def test_the_command_exits_zero(self):
        actor = _rider()
        _legacy_hashed_rows(3, actor_id=actor.pk)
        _write(actor=actor, target_id=10)
        out = StringIO()

        call_command('verify_audit_chain', stdout=out)  # no SystemExit

        assert 'Audit chain intact: 4 row(s) verified (4 hashed)' in out.getvalue()

    def test_planting_an_email_on_an_old_row_is_caught(self):
        """Tampered through a raw cursor: the attacker the chain is for."""
        legacy = _legacy_hashed_rows(3)
        _write(target_id=10)

        _raw(f'UPDATE {TABLE} SET actor_email = %s WHERE id = %s',
             ['forged@saaradhigo.test', legacy[1].pk])

        _, problems = verify_chain(AdminAuditLog.objects.all())
        assert [(p['id'], p['issue']) for p in problems] == [
            (legacy[1].pk, 'content altered')]

    def test_blanking_the_email_on_a_new_row_is_caught(self):
        operator = _operator()
        victim = _write(actor=operator, target_id=10)
        _write(actor=operator, target_id=11)

        _raw(f'UPDATE {TABLE} SET actor_email = %s WHERE id = %s', ['', victim.pk])

        _, problems = verify_chain(AdminAuditLog.objects.all())
        assert [(p['id'], p['issue']) for p in problems] == [
            (victim.pk, 'content altered')]

    def test_changing_the_role_on_a_new_row_is_caught(self):
        operator = _operator()
        victim = _write(actor=operator, target_id=10)

        _raw(f'UPDATE {TABLE} SET actor_role = %s WHERE id = %s',
             ['admin/support', victim.pk])

        _, problems = verify_chain(AdminAuditLog.objects.all())
        assert [p['issue'] for p in problems] == ['content altered']


# ---------------------------------------------------------------------------
# The migration itself, on PostgreSQL, with rows in the table
# ---------------------------------------------------------------------------

@pytest.mark.postgres
class TestTheMigrationOnPostgres:
    """Apply 0006 to a table that already has hashed rows naming an actor.

    Marked `postgres`: excluded from the default SQLite run and executed by
    the PostgreSQL CI job. This is the only backend with the append-only
    trigger, so it is the only place the question "does the migration
    UPDATE anything?" has teeth: if it did, the trigger would refuse it and
    `migrate` would fail here.

    Runs inside the test transaction (DDL is transactional on PostgreSQL),
    so the schema and `django_migrations` are rolled back afterwards.
    `SET CONSTRAINTS ALL IMMEDIATE` flushes the deferred FK checks queued by
    the inserts, without which PostgreSQL refuses to ALTER a table "because
    it has pending trigger events".
    """

    APP = 'admin_audit'
    BEFORE = '0005_chain_anchor'
    AFTER = '0006_actor_snapshot'

    @pytest.fixture(autouse=True)
    def _postgres_only(self):
        if connection.vendor != 'postgresql':
            pytest.skip('needs the PostgreSQL append-only trigger')

    def _executor(self):
        from django.db.migrations.executor import MigrationExecutor

        return MigrationExecutor(connection)

    def _migrate(self, target):
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
        self._executor().migrate([(self.APP, target)])

    def _fk_constraints(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = %s::regclass AND contype = 'f'", [TABLE])
            return [r[0] for r in cursor.fetchall()]

    def _triggers(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tgname, tgenabled FROM pg_trigger "
                "WHERE tgrelid = %s::regclass AND NOT tgisinternal", [TABLE])
            return dict(cursor.fetchall())

    def test_it_applies_over_rows_and_the_chain_verifies_afterwards(self):
        actor = _rider(phone='+919700000301', email='acted@saaradhigo.test')

        # --- the database as it was before 0006 -------------------------
        self._migrate(self.BEFORE)
        assert self._fk_constraints(), 'at 0005 actor_id is a FK'
        OldLog = self._executor().loader.project_state(
            (self.APP, self.BEFORE)).apps.get_model(self.APP, 'AdminAuditLog')
        assert not any(f.name == 'actor_email' for f in OldLog._meta.fields)

        previous = GENESIS
        old_ids = []
        for i in range(3):
            row = OldLog(
                actor_id=actor.pk, actor_label='A Rider', action='kyc_update',
                target_type='driver', target_id=str(i),
                before={'approved': False}, after={'approved': True},
                reason='Written before 0006.', ip_address='203.0.113.7',
                user_agent='', created_at=datetime.datetime(2026, 10, 1, 9, 0, i, tzinfo=UTC),
            )
            row.previous_hash = previous
            row.content_hash = _old_hash(previous, row)
            row.save(force_insert=True)
            previous = row.content_hash
            old_ids.append(row.pk)

        # --- migrate forward: DDL only, no row touched -------------------
        self._migrate(self.AFTER)

        assert self._fk_constraints() == [], 'the FK constraint is gone'
        triggers = self._triggers()
        assert triggers.get('saaradhigo_audit_no_update') == 'O'
        assert triggers.get('saaradhigo_audit_no_delete') == 'O'

        rows = list(AdminAuditLog.objects.filter(pk__in=old_ids).order_by('id'))
        assert [r.actor_id for r in rows] == [actor.pk] * 3
        assert all(r.actor_email == '' and r.actor_role == '' for r in rows)

        report = inspect_chain(AdminAuditLog.objects.all())
        assert report['problems'] == [], report['problems']
        assert report['checked'] == 3

        # --- new rows chain on, and the actor still cannot be deleted ----
        fresh = _write(actor=actor, target_id=99)
        assert fresh.actor_email == 'acted@saaradhigo.test'
        assert fresh.previous_hash == rows[-1].content_hash

        out = StringIO()
        call_command('verify_audit_chain', stdout=out)  # exit 0
        assert 'Audit chain intact: 4 row(s) verified (4 hashed)' in out.getvalue()

        with pytest.raises(AccountDeletionRefused):
            actor.delete()
        assert User.objects.filter(pk=actor.pk).exists()

    def test_the_trigger_still_refuses_an_update_to_the_new_columns(self):
        """The ruling: no column is exempt."""
        row = _write(actor=_operator(), target_id=1)

        with pytest.raises(IntegrityError, match='append-only'):
            _raw(f'UPDATE {TABLE} SET actor_email = %s WHERE id = %s',
                 ['x@y.test', row.pk])
