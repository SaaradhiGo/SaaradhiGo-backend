"""The one row that says the two linking migrations ran unaudited.

THE RULING (owner, 2026-10-28, ruling 3)
----------------------------------------
`pricing.0010_link_zones_to_geo_nodes` and
`driver.0027_backfill_driver_home_city` linked zones and drivers on QA and
production on 2026-10-27 and, by design (`admin_access.linking`), wrote no
audit row. `record_retroactive_linking_audit` appends ONE summary row that
says so: the migration names, what the linked columns hold now,
`recorded_retroactively: true`, the day it happened, and who recorded it.

It is idempotent (a second run skips and says so), append-only (nothing
before it changes), labelled `system:migration` with the recording operator
in the actor snapshot, and leaves the hash chain verifying.
"""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from conftest import seed_actor, seed_admin_access
from servers.admin_access.management.commands.record_retroactive_linking_audit import (
    ACTION,
    ACTOR_LABEL,
    MIGRATIONS,
    TARGET_TYPE,
)
from servers.admin_access.models import GeoNode
from servers.admin_audit.hashchain import verify_chain
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import Driver
from servers.pricing.models import ServiceZone

pytestmark = pytest.mark.django_db

User = get_user_model()

PERFORMED_ON = '2026-10-27'
REPO_MIGRATIONS = [
    'servers/pricing/migrations/0010_link_zones_to_geo_nodes.py',
    'servers/driver/migrations/0027_backfill_driver_home_city.py',
]


@pytest.fixture
def actor():
    return seed_actor()


@pytest.fixture
def linked(actor):
    """A database after the seed: zones linked, one driver with a home city."""
    user = User.objects.create_user(phone_number='+917770009001', role='driver')
    Driver.objects.create(user_id=user)
    seed_admin_access()
    assert ServiceZone.objects.filter(geo_node__isnull=False).count() >= 1
    assert Driver.objects.filter(home_city__isnull=False).count() == 1
    return actor


def _run(**overrides):
    out = StringIO()
    kwargs = {'performed_on': PERFORMED_ON, 'actor_email': seed_actor().email}
    kwargs.update(overrides)
    call_command('record_retroactive_linking_audit', stdout=out, **kwargs)
    return out.getvalue()


def _rows():
    return list(AdminAuditLog.objects.filter(action=ACTION).order_by('id'))


def _everything():
    return list(AdminAuditLog.objects.order_by('id').values())


class TestItWritesExactlyOneRow:
    def test_one_row_with_the_facts_in_its_payload(self, linked):
        total = AdminAuditLog.objects.count()
        zones = ServiceZone.objects.filter(geo_node__isnull=False).count()
        drivers = Driver.objects.filter(home_city__isnull=False).count()

        output = _run()

        assert AdminAuditLog.objects.count() == total + 1
        row = _rows()[0]
        assert row.target_type == TARGET_TYPE
        assert row.target_id == ''
        assert row.before == {}
        assert row.after == {
            'migrations': ['pricing.0010_link_zones_to_geo_nodes',
                           'driver.0027_backfill_driver_home_city'],
            'service_zones_linked': zones,
            'drivers_with_home_city': drivers,
            'recorded_retroactively': True,
            'performed_on': '2026-10-27',
            'recorded_by': linked.email,
        }
        assert 'Recorded retroactively' in row.reason
        assert 'performed_on is the day the migrations ran' in row.reason
        assert f'audit row {row.pk}' in output
        assert '1 row appended' in output

    def test_the_migration_names_are_real(self):
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        assert MIGRATIONS == ['pricing.0010_link_zones_to_geo_nodes',
                              'driver.0027_backfill_driver_home_city']
        for path in REPO_MIGRATIONS:
            assert (repo / path).exists(), path

    def test_the_counts_describe_the_table_now(self, linked):
        """Not a guess about the night: the columns as they are."""
        for zone in ServiceZone.objects.filter(geo_node__isnull=False)[:1]:
            zone.geo_node = None
            zone.save(update_fields=['geo_node'])
        expected_zones = ServiceZone.objects.filter(geo_node__isnull=False).count()

        _run()

        assert _rows()[0].after['service_zones_linked'] == expected_zones
        assert _rows()[0].after['drivers_with_home_city'] == 1

    def test_with_nothing_linked_the_counts_are_zero(self, actor):
        assert not GeoNode.objects.exists()

        _run()

        row = _rows()[0]
        assert row.after['service_zones_linked'] == 0
        assert row.after['drivers_with_home_city'] == 0


class TestWhoIsTheActor:
    def test_the_label_is_the_migration_and_the_snapshot_is_the_operator(self, linked):
        _run()

        row = _rows()[0]
        assert row.actor_label == ACTOR_LABEL == 'system:migration'
        assert row.actor_id == linked.pk
        assert row.actor_email == linked.email
        assert row.actor_role != ''

    def test_without_an_actor_it_is_refused_at_parse_time(self, linked):
        total = AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match='--actor-email'):
            call_command('record_retroactive_linking_audit',
                         performed_on=PERFORMED_ON, stdout=StringIO())

        assert AdminAuditLog.objects.count() == total

    def test_a_non_operator_is_refused_before_any_write(self, linked):
        User.objects.create_user(phone_number='+917770009002', role='rider',
                                 email='rider@saaradhigo.test')
        total = AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match='not an operator'):
            _run(actor_email='rider@saaradhigo.test')

        assert AdminAuditLog.objects.count() == total


class TestItIsIdempotentAndAppendOnly:
    def test_a_second_run_writes_nothing_and_says_so(self, linked):
        _run()
        before = _everything()

        output = _run()

        assert _everything() == before
        assert 'Already recorded' in output
        assert 'Nothing was written' in output
        assert len(_rows()) == 1

    def test_a_second_run_with_a_different_day_still_skips(self, linked):
        _run()

        output = _run(performed_on='2026-10-28')

        assert len(_rows()) == 1
        assert _rows()[0].after['performed_on'] == '2026-10-27'
        assert '2026-10-27' in output

    def test_no_existing_row_changes(self, linked):
        before = _everything()

        _run()

        after = _everything()
        assert after[:len(before)] == before
        assert len(after) == len(before) + 1

    def test_created_at_is_now_not_the_day_recorded(self, linked):
        from django.utils import timezone

        _run()

        row = _rows()[0]
        assert abs((timezone.now() - row.created_at).total_seconds()) < 60
        assert row.after['performed_on'] == PERFORMED_ON

    def test_the_chain_verifies_afterwards(self, linked):
        _run()

        checked, problems = verify_chain(AdminAuditLog.objects.all())
        assert checked == AdminAuditLog.objects.count()
        assert problems == []

    def test_verify_audit_chain_exits_cleanly(self, linked):
        """The command the owner will run next, through its real entry."""
        _run()

        out = StringIO()
        call_command('verify_audit_chain', stdout=out)   # SystemExit(1) on a break
        assert 'Audit chain intact' in out.getvalue()


class TestItRefusesToGuess:
    @pytest.mark.parametrize('value', ['27/10/2026', '2026-10-27T01:14:00Z', 'yesterday', ''])
    def test_a_bad_date_is_refused_before_any_write(self, linked, value):
        total = AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match='ISO-8601 date'):
            _run(performed_on=value)

        assert AdminAuditLog.objects.count() == total

    def test_an_audit_write_failure_is_an_error_not_a_success(self, linked, monkeypatch):
        import servers.admin_access.management.commands.record_retroactive_linking_audit as mod

        monkeypatch.setattr(mod, 'record_admin_action', lambda *a, **k: None)

        with pytest.raises(CommandError, match='could not be written'):
            _run()

        assert _rows() == []
