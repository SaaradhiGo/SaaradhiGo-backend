"""Audit-chain forks from concurrent writers: prevented, sealed, and never a cover for tampering.

THE FAILURE (QA, found 22 Nov)
------------------------------
`verify_audit_chain` reported "AUDIT CHAIN BROKEN: 145 problem(s) across 3423
row(s)". Every break was a row written milliseconds after another that linked
to the same predecessor: the old `select_for_update` on the tail released its
lock before the INSERT, and could not see a concurrent writer's new row anyway.
Writers now hold one advisory lock from reading the tail to COMMIT
(`chain_write_lock`); the old forks are sealed by `seal_audit_chain_forks`.

Forks are made here the way the race made them: a row whose previous_hash is
an EARLIER row's hash, saved with its hash already set so `save()` does not
relink it. Tampering goes through a raw cursor, the path the chain is for.
"""

import threading
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections

from servers.admin_audit.hashchain import (
    SEAL_ACTION,
    ChainStateError,
    compute_hash,
    inspect_chain,
    write_fork_seal,
)
from servers.admin_audit.models import AdminAuditLog

TABLE = 'admin_audit_adminauditlog'


def _write(n=1):
    return AdminAuditLog.objects.create(
        actor_label='ops@saaradhigo.com', action='kyc_update', target_type='driver',
        target_id=str(n), before={'approved': False}, after={'approved': True},
        reason='Documents verified.',
    )


def _fork(onto, n=99):
    """A row linked to `onto` although a later row already exists: the race."""
    row = AdminAuditLog(actor_label='ops@saaradhigo.com', action='kyc_update',
                        target_type='driver', target_id=str(n), before={}, after={'x': n},
                        reason='Written at the same moment as another row.')
    from django.utils import timezone
    row.created_at = timezone.now()
    row.previous_hash = onto.content_hash
    row.content_hash = compute_hash(row.previous_hash, row)
    row.save(force_insert=True)
    return row


def _issues():
    return [p['issue'] for p in inspect_chain(AdminAuditLog.objects.all())['problems']]


def _raw(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


@pytest.mark.django_db
class TestSealingForks:
    def _forked_log(self):
        a = _write(1)
        _write(2)
        fork = _fork(a)
        _write(3)
        return fork

    def test_a_fork_is_reported_as_a_broken_link(self):
        self._forked_log()
        assert _issues() == ['broken link']

    def test_sealing_makes_the_chain_verify(self):
        fork = self._forked_log()

        seal = write_fork_seal(AdminAuditLog, label='test', reason='forks from concurrent writes')

        assert seal.action == SEAL_ACTION
        assert seal.after['fork_ids'] == [fork.pk]
        assert _issues() == []

    def test_rows_after_the_seal_still_chain_normally(self):
        self._forked_log()
        write_fork_seal(AdminAuditLog, label='test', reason='forks from concurrent writes')
        _write(4)
        _write(5)
        assert _issues() == []

    def test_an_edit_to_a_sealed_row_is_still_detected(self):
        self._forked_log()
        write_fork_seal(AdminAuditLog, label='test', reason='forks from concurrent writes')
        victim = AdminAuditLog.objects.order_by('id').first()

        _raw(f'UPDATE {TABLE} SET reason = %s WHERE id = %s', ['Rewritten.', victim.pk])

        issues = _issues()
        assert 'content altered' in issues and 'seal mismatch' in issues

    def test_removing_a_sealed_row_is_detected(self):
        self._forked_log()
        write_fork_seal(AdminAuditLog, label='test', reason='forks from concurrent writes')
        victim = AdminAuditLog.objects.order_by('id')[1]

        _raw(f'DELETE FROM {TABLE} WHERE id = %s', [victim.pk])

        assert 'seal mismatch' in _issues()

    def test_a_new_fork_after_the_seal_is_not_forgiven(self):
        self._forked_log()
        write_fork_seal(AdminAuditLog, label='test', reason='forks from concurrent writes')
        a = _write(6)
        _write(7)
        _fork(a, n=100)
        assert _issues() == ['broken link']

    def test_it_refuses_to_seal_an_altered_row(self):
        self._forked_log()
        victim = AdminAuditLog.objects.order_by('id').first()
        _raw(f'UPDATE {TABLE} SET reason = %s WHERE id = %s', ['Rewritten.', victim.pk])

        with pytest.raises(ChainStateError):
            write_fork_seal(AdminAuditLog, label='test', reason='forks from concurrent writes')
        assert not AdminAuditLog.objects.filter(action=SEAL_ACTION).exists()

    def test_it_refuses_a_link_to_a_hash_that_never_existed(self):
        _write(1)
        _write(2)
        row = _fork(AdminAuditLog.objects.order_by('id').first(), n=50)
        # Point it at a hash no row ever had: an insertion, not a race.
        bogus = 'f' * 64
        _raw(f'UPDATE {TABLE} SET previous_hash = %s, content_hash = %s WHERE id = %s',
             [bogus, compute_hash(bogus, row), row.pk])

        with pytest.raises(ChainStateError):
            write_fork_seal(AdminAuditLog, label='test', reason='forks from concurrent writes')

    def test_a_clean_log_has_nothing_to_seal(self):
        _write(1)
        with pytest.raises(ChainStateError):
            write_fork_seal(AdminAuditLog, label='test', reason='forks from concurrent writes')

    def test_the_command_dry_run_writes_nothing(self):
        self._forked_log()
        out = StringIO()
        call_command('seal_audit_chain_forks', stdout=out)
        assert 'Dry run' in out.getvalue()
        assert not AdminAuditLog.objects.filter(action=SEAL_ACTION).exists()

    def test_the_command_needs_a_reason(self):
        self._forked_log()
        with pytest.raises(CommandError):
            call_command('seal_audit_chain_forks', confirm=True, reason='short', stdout=StringIO())


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_writers_never_fork_the_chain():
    """Eight threads, each writing rows as fast as it can: the race that forked QA."""
    errors = []

    def worker(k):
        try:
            for i in range(15):
                _write(k * 100 + i)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert AdminAuditLog.objects.count() == 120
    assert _issues() == []


@pytest.mark.django_db
class TestViewerHonoursSeals1124:
    """The audit page's own check (verify_ids) must agree with inspect_chain once forks are sealed."""

    def test_sealed_forks_verify_on_the_page(self):
        from servers.admin_audit.viewer import verify_ids
        a = _write(1)
        _write(2)
        _fork(a)
        last = _write(3)
        assert verify_ids(a.id, last.id)['status'] == 'broken'
        write_fork_seal(AdminAuditLog, label='ops@saaradhigo.com', reason='Seal pre-fix forks.')
        tail = AdminAuditLog.objects.order_by('-id').first()
        result = verify_ids(a.id, tail.id)
        assert result['status'] == 'ok', result['problems']

    def test_tampering_a_sealed_row_still_breaks_the_page(self):
        from servers.admin_audit.viewer import verify_ids
        a = _write(1)
        _write(2)
        fork = _fork(a)
        _write(3)
        write_fork_seal(AdminAuditLog, label='ops@saaradhigo.com', reason='Seal pre-fix forks.')
        _raw(f'UPDATE {TABLE} SET reason = %s WHERE id = %s', ['rewritten', fork.id])
        tail = AdminAuditLog.objects.order_by('-id').first()
        issues = {p['issue'] for p in verify_ids(a.id, tail.id)['problems']}
        assert 'content altered' in issues and 'seal mismatch' in issues
