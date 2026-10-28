"""The rows the night of 2026-10-27 should have had are appended, marked
as late, and never faked as contemporaneous.

THE SHAPE OF THE REPAIR
-----------------------
`migrate_operators --plan qa --apply` made three changes on QA and wrote no
audit row. The command under test appends ONE row per plan line that says
so in its own payload: `recorded_retroactively: true`, the original moment
in `performed_at`, and the command that wrote it. `created_at` is left as
the moment of appending, because that is the truth.

It is idempotent (a second run writes nothing), append-only (no existing
row changes), refuses to describe a state that is not there, and leaves
the hash chain verifying -- these rows go through the ordinary model path.
"""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from servers.admin_access.management.commands.migrate_operators import QA_PLAN
from servers.admin_access.management.commands.record_retroactive_access_audit import (
    ASSIGNMENT_ACTION,
    BREAK_GLASS_ACTION,
    RECORDED_BY,
)
from servers.admin_access.models import AdminAssignment
from servers.admin_audit.hashchain import verify_chain
from servers.admin_audit.models import AdminAuditLog

pytestmark = pytest.mark.django_db

User = get_user_model()

ACTOR = 'srini@saaradhigo.com'
PERFORMED_AT = '2026-10-27T01:14:00Z'
RETRO_ACTIONS = (ASSIGNMENT_ACTION, BREAK_GLASS_ACTION)


@pytest.fixture
def tonight():
    """QA as it was left: seeded, three accounts, the plan applied."""
    people = {}
    for index, entry in enumerate(QA_PLAN):
        phone = f'+91970000020{index}'
        people[entry['email']] = User.objects.create_user(
            phone_number=phone, username=phone, email=entry['email'],
            full_name=entry['email'].split('@')[0], role='admin', is_staff=True,
        )
    call_command('seed_admin_access', actor_email=ACTOR, stdout=StringIO())
    call_command('migrate_operators', plan='qa', apply=True, actor_email=ACTOR,
                 stdout=StringIO())
    return people


def _run(**overrides):
    out = StringIO()
    kwargs = {'plan': 'qa', 'actor_email': ACTOR, 'performed_at': PERFORMED_AT}
    kwargs.update(overrides)
    call_command('record_retroactive_access_audit', stdout=out, **kwargs)
    return out.getvalue()


def _retro_rows():
    return list(AdminAuditLog.objects.filter(action__in=RETRO_ACTIONS).order_by('id'))


def _everything():
    return list(AdminAuditLog.objects.order_by('id').values())


class TestItWritesExactlyThreeRows:
    def test_three_rows_for_the_qa_plan(self, tonight):
        total_before = AdminAuditLog.objects.count()

        output = _run()

        assert AdminAuditLog.objects.count() == total_before + 3
        rows = _retro_rows()
        assert len(rows) == 3
        assert sorted(r.action for r in rows) == sorted(
            [ASSIGNMENT_ACTION, ASSIGNMENT_ACTION, BREAK_GLASS_ACTION])
        assert '3 row(s) appended, 0 already present' in output

    def test_each_row_names_the_actor_and_marks_itself_late(self, tonight):
        _run()

        actor = tonight[ACTOR]
        for row in _retro_rows():
            assert row.actor_id == actor.pk
            assert row.actor_label == f'{ACTOR} via record_retroactive_access_audit'
            assert row.after['recorded_retroactively'] is True
            assert row.after['performed_at'] == '2026-10-27T01:14:00+00:00'
            assert row.after['recorded_by'] == RECORDED_BY
            assert row.after['plan'] == 'qa'
            assert row.before == {}
            assert 'Recorded retroactively' in row.reason
            # Appended now, not back-dated.
            assert row.created_at.isoformat() != row.after['performed_at']

    def test_the_assignment_rows_describe_the_grant(self, tonight):
        _run()

        rows = [r for r in _retro_rows() if r.action == ASSIGNMENT_ACTION]
        grants = {(r.after['role'], r.after['scope']) for r in rows}
        assert grants == {('super_admin', 'GLOBAL'), ('city_ops_manager', 'IN-TG-HYD')}
        for row in rows:
            assignment = AdminAssignment.objects.get(pk=int(row.target_id))
            assert row.target_type == 'AdminAssignment'
            assert row.after['admin_id'] == assignment.admin_id
            assert row.after['is_active'] is True

    def test_the_break_glass_row_points_at_the_account(self, tonight):
        _run()

        row = next(r for r in _retro_rows() if r.action == BREAK_GLASS_ACTION)
        assert row.target_type == 'User'
        assert row.target_id == str(tonight['admin@saaradhigo.com'].pk)
        assert row.after['is_break_glass'] is True

    def test_the_timestamp_is_normalised_to_utc(self, tonight):
        _run(performed_at='2026-10-27T06:44:00+05:30')

        assert all(r.after['performed_at'] == '2026-10-27T01:14:00+00:00'
                   for r in _retro_rows())


class TestItIsIdempotentAndAppendOnly:
    def test_a_second_run_writes_nothing_and_says_so(self, tonight):
        _run()
        before = _everything()

        output = _run()

        assert _everything() == before
        assert '0 row(s) appended, 3 already present' in output
        assert output.count('already recorded') == 3

    def test_no_existing_row_is_touched(self, tonight):
        before = _everything()

        _run()

        after = _everything()
        assert after[:len(before)] == before
        assert len(after) == len(before) + 3


class TestTheChainStillVerifies:
    def test_verify_audit_chain_exits_0_afterwards(self, tonight):
        _run()
        _run()

        out = StringIO()
        call_command('verify_audit_chain', stdout=out)   # no SystemExit

        assert 'Audit chain intact' in out.getvalue()
        assert verify_chain(AdminAuditLog.objects.all())[1] == []


class TestItRefusesToGuess:
    def test_a_naive_timestamp_is_refused(self, tonight):
        with pytest.raises(CommandError, match='no timezone'):
            _run(performed_at='2026-10-27T01:14:00')
        assert _retro_rows() == []

    def test_garbage_is_refused(self, tonight):
        with pytest.raises(CommandError, match='not an ISO-8601'):
            _run(performed_at='last night')
        assert _retro_rows() == []

    def test_an_unknown_actor_is_refused(self, tonight):
        with pytest.raises(CommandError, match='matches no account'):
            _run(actor_email='ghost@saaradhigo.test')
        assert _retro_rows() == []

    def test_a_missing_assignment_refuses_the_whole_run(self, tonight):
        AdminAssignment.objects.filter(role__code='city_ops_manager').delete()

        with pytest.raises(CommandError, match='holds no active city_ops_manager'):
            _run()

        # Not even the lines that WERE present: nothing was written.
        assert _retro_rows() == []

    def test_an_unflagged_account_refuses_the_whole_run(self, tonight):
        account = tonight['admin@saaradhigo.com']
        account.is_break_glass = False
        account.save()

        with pytest.raises(CommandError, match='not flagged break-glass'):
            _run()

        assert _retro_rows() == []

    def test_a_missing_account_refuses_the_whole_run(self):
        # The actor exists; the other two accounts in the plan do not.
        User.objects.create_user(phone_number='+919700000300',
                                 username='+919700000300', email=ACTOR,
                                 role='admin', is_staff=True)
        call_command('seed_admin_access', actor_email=ACTOR, stdout=StringIO())

        with pytest.raises(CommandError, match='no such account'):
            _run()

        assert _retro_rows() == []
