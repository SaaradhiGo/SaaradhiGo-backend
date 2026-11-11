"""Rows that predate the hash chain are vouched for, never rewritten.

THE FAILURE THIS CLOSES
-----------------------
Migration 0003 added the chain to a table that already had rows, and left
them -- correctly -- without a hash. The verifier then reported every one of
them as "content altered" and the first new row as a "broken link": a tamper
alarm that was permanently on. Found on QA on 2026-10-27 with 20 such rows;
production would have had the same alarm from day one.

THE RULING (owner, 2026-10-27)
------------------------------
Do not rewrite the old rows; the log is append-only for a reason. Do not
leave them unprotected either. One CHAIN_ANCHOR row records how many came
before it and a digest of their content, so they are tamper-evident without
ever being edited. The verifier accepts unhashed rows only as one unbroken
run before the anchor; an unhashed row anywhere after it is a failure.

Tampering in these tests goes through a RAW CURSOR, which is the path that
bypasses the model guard and, on SQLite, any trigger -- i.e. the attacker
the chain is for. "Legacy" rows are made with `bulk_create`, which skips
`save()` and therefore the chain, exactly as the pre-0003 rows were.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection

from servers.admin_audit.hashchain import (
    ANCHOR_ACTION,
    GENESIS,
    ChainStateError,
    inspect_chain,
    is_anchor,
    verify_chain,
    write_anchor,
)
from servers.admin_audit.models import AdminAuditLog

pytestmark = pytest.mark.django_db

TABLE = 'admin_audit_adminauditlog'


def _legacy(n, start=0):
    """`n` rows exactly as they were before the chain existed: no hash."""
    rows = AdminAuditLog.objects.bulk_create([
        AdminAuditLog(
            actor_label='ops@saaradhigo.com', action='kyc_update',
            target_type='driver', target_id=str(start + i),
            before={'approved': False}, after={'approved': True},
            reason='Documents verified (pre-chain).',
        )
        for i in range(n)
    ])
    assert all(r.content_hash == '' for r in rows)
    return rows


def _write(action='kyc_update', target_id=1):
    """A row written the normal way, so it joins the chain."""
    return AdminAuditLog.objects.create(
        actor_label='ops@saaradhigo.com', action=action,
        target_type='driver', target_id=str(target_id),
        before={'approved': False}, after={'approved': True},
        reason='Documents verified.',
    )


def _anchor():
    return write_anchor(AdminAuditLog, label='test anchor')


def _issues(problems):
    return [p['issue'] for p in problems]


def _raw(sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


# ---------------------------------------------------------------------------
# The anchor row itself
# ---------------------------------------------------------------------------

class TestWritingTheAnchor:
    def test_it_vouches_for_the_leading_run(self):
        legacy = _legacy(3)

        anchor = _anchor()

        assert is_anchor(anchor)
        assert anchor.after['pre_chain_rows'] == 3
        assert anchor.after['pre_chain_first_id'] == legacy[0].pk
        assert anchor.after['pre_chain_max_id'] == legacy[-1].pk
        assert len(anchor.after['pre_chain_digest']) == 64
        # It is a chained row like any other.
        assert anchor.previous_hash == GENESIS
        assert len(anchor.content_hash) == 64

    def test_it_is_appended_and_nothing_existing_is_touched(self):
        legacy = _legacy(2)
        before = list(AdminAuditLog.objects.order_by('id')
                      .values('id', 'reason', 'content_hash', 'previous_hash'))

        _anchor()

        after = list(AdminAuditLog.objects.order_by('id')
                     .values('id', 'reason', 'content_hash', 'previous_hash'))
        assert after[:len(before)] == before
        assert len(after) == len(legacy) + 1

    def test_a_table_with_no_pre_chain_rows_gets_no_anchor(self):
        _write(target_id=1)
        _write(target_id=2)

        assert _anchor() is None
        assert not AdminAuditLog.objects.filter(action=ANCHOR_ACTION).exists()

    def test_an_empty_table_gets_no_anchor(self):
        assert _anchor() is None
        assert AdminAuditLog.objects.count() == 0

    def test_the_chain_starts_once(self):
        _legacy(2)
        _anchor()

        with pytest.raises(ChainStateError, match='already'):
            _anchor()

    def test_it_refuses_to_anchor_over_an_unhashed_row_after_a_hashed_one(self):
        _legacy(2)
        _write(target_id=9)
        # An unhashed row AFTER the chain started is damage, not history.
        _legacy(1, start=100)

        with pytest.raises(ChainStateError, match='refusing'):
            _anchor()
        assert not AdminAuditLog.objects.filter(action=ANCHOR_ACTION).exists()

    def test_the_qa_shape_anchors_after_hashed_rows(self):
        """QA had 20 unhashed rows, then hashed rows, before any anchor.

        The anchor may land after hashed rows: it links to the tail like any
        row, so it commits to everything between through `previous_hash`.
        """
        _legacy(20)
        tail = _write(target_id=21)

        anchor = _anchor()

        assert anchor.after['pre_chain_rows'] == 20
        assert anchor.previous_hash == tail.content_hash

    def test_new_rows_chain_from_the_anchor(self):
        _legacy(2)
        anchor = _anchor()

        nxt = _write(target_id=5)

        assert nxt.previous_hash == anchor.content_hash


# ---------------------------------------------------------------------------
# The verifier
# ---------------------------------------------------------------------------

class TestACleanChainStillVerifies:
    def test_no_pre_chain_rows(self):
        for i in range(5):
            _write(target_id=i)

        report = inspect_chain(AdminAuditLog.objects.all())

        assert report['checked'] == 5
        assert report['problems'] == []
        assert report['pre_chain'] is None

    def test_the_two_tuple_api_is_unchanged(self):
        _write(target_id=1)

        checked, problems = verify_chain(AdminAuditLog.objects.all())

        assert (checked, problems) == (1, [])


class TestPreChainRowsWithAnAnchor:
    def test_they_verify_and_are_reported_as_protected(self):
        legacy = _legacy(4)
        anchor = _anchor()
        for i in range(3):
            _write(target_id=10 + i)

        report = inspect_chain(AdminAuditLog.objects.all())

        assert report['problems'] == []
        assert report['checked'] == 8
        assert report['pre_chain'] == {
            'rows': 4, 'first_id': legacy[0].pk, 'last_id': legacy[-1].pk,
            'anchor_id': anchor.pk,
        }

    def test_the_qa_shape_verifies_once_anchored(self):
        """Legacy run, then GENESIS-linked hashed rows, then the anchor."""
        _legacy(20)
        _write(target_id=21)
        _write(target_id=22)
        anchor = _anchor()
        _write(target_id=23)

        report = inspect_chain(AdminAuditLog.objects.all())

        assert report['problems'] == []
        assert report['pre_chain']['rows'] == 20
        assert report['pre_chain']['anchor_id'] == anchor.pk


class TestTamperingWithPreChainRowsIsDetected:
    def test_an_edited_pre_chain_row_breaks_the_anchor(self):
        legacy = _legacy(4)
        _anchor()
        _write(target_id=9)

        _raw(f'UPDATE {TABLE} SET reason = %s WHERE id = %s',
             ['nothing to see here', legacy[1].pk])

        report = inspect_chain(AdminAuditLog.objects.all())

        assert 'anchor mismatch' in _issues(report['problems'])
        # The row itself has no hash to fail, so the ANCHOR is what reports
        # it -- and nothing after the anchor is blamed.
        assert all(p['issue'] == 'anchor mismatch' for p in report['problems'])

    def test_a_deleted_pre_chain_row_breaks_the_anchor(self):
        legacy = _legacy(4)
        _anchor()
        _write(target_id=9)

        _raw(f'DELETE FROM {TABLE} WHERE id = %s', [legacy[2].pk])

        report = inspect_chain(AdminAuditLog.objects.all())

        assert _issues(report['problems']) == ['anchor mismatch']
        detail = report['problems'][0]['detail']
        assert 'vouches for 4' in detail and 'holds 3' in detail

    def test_a_deleted_last_pre_chain_row_is_caught_by_max_id_as_well(self):
        legacy = _legacy(3)
        _anchor()

        _raw(f'DELETE FROM {TABLE} WHERE id = %s', [legacy[-1].pk])

        _, problems = verify_chain(AdminAuditLog.objects.all())

        assert _issues(problems) == ['anchor mismatch']

    def test_an_edited_anchor_is_caught_like_any_row(self):
        _legacy(2)
        anchor = _anchor()

        _raw(f'UPDATE {TABLE} SET reason = %s WHERE id = %s',
             ['anchor rewritten', anchor.pk])

        _, problems = verify_chain(AdminAuditLog.objects.all())

        assert 'content altered' in _issues(problems)


class TestUnhashedRowsAfterTheAnchorFail:
    def test_an_unhashed_row_after_the_anchor_is_a_failure(self):
        _legacy(2)
        _anchor()
        _write(target_id=5)

        planted = _legacy(1, start=500)[0]

        report = inspect_chain(AdminAuditLog.objects.all())

        assert [(p['id'], p['issue']) for p in report['problems']] == [
            (planted.pk, 'unhashed row'),
        ]

    def test_an_unhashed_row_after_a_chain_with_no_pre_chain_rows_fails(self):
        _write(target_id=1)
        _write(target_id=2)

        planted = _legacy(1, start=500)[0]

        _, problems = verify_chain(AdminAuditLog.objects.all())

        assert [(p['id'], p['issue']) for p in problems] == [
            (planted.pk, 'unhashed row'),
        ]

    def test_a_second_anchor_is_a_failure(self):
        _legacy(2)
        _anchor()
        # Forged through the ORM path that would chain it correctly, i.e.
        # the strongest forgery: hashes all valid, still refused.
        forged = AdminAuditLog.objects.create(
            actor_label='forger', action=ANCHOR_ACTION,
            target_type='audit_chain', after={'pre_chain_rows': 0},
        )

        _, problems = verify_chain(AdminAuditLog.objects.all())

        assert [(p['id'], p['issue']) for p in problems] == [
            (forged.pk, 'second anchor'),
        ]


class TestPreChainRowsWithoutAnAnchorFail:
    def test_the_state_qa_was_in_is_reported_as_unanchored(self):
        """20 unhashed rows, then hashed rows, no anchor: fails, and says why."""
        legacy = _legacy(20)
        _write(target_id=21)

        report = inspect_chain(AdminAuditLog.objects.all())

        assert _issues(report['problems']) == ['unanchored pre-chain rows']
        assert report['problems'][0]['id'] == legacy[-1].pk
        assert 'migration 0005' in report['problems'][0]['detail']
        assert report['pre_chain']['anchor_id'] is None

    def test_they_are_not_reported_as_altered_or_broken(self):
        """The false alarm this change removes: no per-row noise."""
        _legacy(20)
        _write(target_id=21)

        _, problems = verify_chain(AdminAuditLog.objects.all())

        assert 'content altered' not in _issues(problems)
        assert 'broken link' not in _issues(problems)

    def test_a_limit_that_ends_inside_the_run_does_not_cry_wolf(self):
        _legacy(5)
        _anchor()

        checked, problems = verify_chain(AdminAuditLog.objects.all(), limit=3)

        assert checked == 3
        assert problems == []


# ---------------------------------------------------------------------------
# The migration and the command
# ---------------------------------------------------------------------------

class TestTheMigration:
    def test_add_anchor_writes_one_row_for_legacy_rows(self):
        from django.apps import apps
        from importlib import import_module

        migration = import_module(
            'servers.admin_audit.migrations.0005_chain_anchor')
        _legacy(3)

        migration.add_anchor(apps, None)

        anchors = AdminAuditLog.objects.filter(action=ANCHOR_ACTION)
        assert anchors.count() == 1
        assert anchors.get().actor_label == migration.ANCHOR_LABEL
        _, problems = verify_chain(AdminAuditLog.objects.all())
        assert problems == []

    def test_add_anchor_writes_nothing_on_a_fresh_table(self):
        from django.apps import apps
        from importlib import import_module

        migration = import_module(
            'servers.admin_audit.migrations.0005_chain_anchor')

        migration.add_anchor(apps, None)

        assert AdminAuditLog.objects.count() == 0


class TestTheCommand:
    def _run(self, **kwargs):
        out = StringIO()
        call_command('verify_audit_chain', stdout=out, **kwargs)
        return out.getvalue()

    def test_exit_0_and_says_protected_when_anchored(self):
        _legacy(3)
        anchor = _anchor()
        _write(target_id=7)

        output = self._run()  # no SystemExit

        assert '3 pre-chain row(s)' in output
        assert f'CHAIN_ANCHOR row {anchor.pk}' in output
        assert 'Audit chain intact' in output

    def test_exit_1_when_pre_chain_rows_are_unanchored(self):
        _legacy(3)
        _write(target_id=7)

        with pytest.raises(SystemExit) as exit_info:
            self._run()

        assert exit_info.value.code == 1

    def test_exit_1_when_an_unhashed_row_follows_the_anchor(self):
        _legacy(3)
        _anchor()
        _legacy(1, start=99)

        with pytest.raises(SystemExit) as exit_info:
            self._run()

        assert exit_info.value.code == 1

    def test_exit_1_when_a_pre_chain_row_was_edited(self):
        legacy = _legacy(3)
        _anchor()
        _raw(f'UPDATE {TABLE} SET reason = %s WHERE id = %s',
             ['tampered', legacy[0].pk])

        with pytest.raises(SystemExit) as exit_info:
            self._run()

        assert exit_info.value.code == 1
