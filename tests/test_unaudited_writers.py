"""The four writers that left no trace, by the owner's ruling of 2026-10-28:

    The four unaudited writers, one PR: zone-to-geo and driver home-city
    linking: full audit rows; trip-count recompute: one summary audit row
    per run, not per driver; load-fixture seeder: no audit, but refuse to
    run unless the environment is the load stack; add a test; MFA
    --replace: confirm the new factor before removing the old; audit both;
    never leave an account with zero factors (when MFA is required).

This file holds the first three. `test_enroll_operator_mfa_replace.py`
holds the fourth.
"""

import logging
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from conftest import SEED_ACTOR_EMAIL, seed_actor, seed_admin_access
from servers.admin_access import linking
from servers.admin_access.models import ACTIVE, CITY, PAUSED, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.driver.management.commands.recompute_driver_trip_counts import (
    AUDIT_ACTION as RECOMPUTE_ACTION,
)
from servers.driver.management.commands.recompute_driver_trip_counts import (
    MAX_IDS_IN_ROW,
)
from servers.driver.models import Driver
from servers.pricing.models import ServiceZone

pytestmark = pytest.mark.django_db

User = get_user_model()

POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[78.30, 17.40], [78.50, 17.40], [78.50, 17.50],
                     [78.30, 17.50], [78.30, 17.40]]],
}


#: The pricing migrations seed four zones (HYD, WGL, VJA, VTZ) whose codes
#: match the geo tree; `pricing/0010` could not link them in the test
#: database because no node existed yet. So every seed links these four.
SEEDED_ZONE_CODES = ('IN-TG-HYD', 'IN-TG-WGL', 'IN-AP-VJA', 'IN-AP-VTZ')


def _zone(code, name='A zone'):
    """The zone with this code, created if the migrations did not seed it."""
    zone = ServiceZone.objects.filter(code=code).first()
    if zone is None:
        zone = ServiceZone.objects.create(
            code=code, name=name, zone_type='city', city=name,
            polygon_geojson=POLYGON, priority=10, is_active=True,
        )
    return zone


def _rows_for(action, target_id):
    return [r for r in _rows(action) if r.target_id == str(target_id)]


def _driver(phone):
    user = User.objects.create_user(phone_number=phone, role='driver')
    return Driver.objects.create(user_id=user)


def _rows(action):
    return list(AdminAuditLog.objects.filter(action=action).order_by('id'))


# ===========================================================================
# Zone -> geo node linking leaves one row per zone
# ===========================================================================

class TestZoneLinkingIsAudited:
    def test_the_seed_links_an_existing_zone_and_records_it(self):
        zone = _zone('IN-TG-HYD', 'Hyderabad')
        actor = seed_actor()

        seed_admin_access()

        zone.refresh_from_db()
        node = GeoNode.objects.get(code='IN-TG-HYD')
        assert zone.geo_node_id == node.pk
        # One row per linked zone, no more.
        linked = ServiceZone.objects.filter(geo_node__isnull=False)
        assert sorted(r.target_id for r in _rows(linking.ZONE_LINKED_ACTION)) == sorted(
            str(z.pk) for z in linked)
        rows = _rows_for(linking.ZONE_LINKED_ACTION, zone.pk)
        assert len(rows) == 1
        row = rows[0]
        assert row.target_type == 'ServiceZone'
        assert row.target_id == str(zone.pk)
        assert row.before == {'zone_code': 'IN-TG-HYD', 'geo_node_id': None}
        assert row.after == {'zone_code': 'IN-TG-HYD', 'geo_node_id': node.pk,
                             'geo_node_code': 'IN-TG-HYD'}
        assert 'seed_admin_access' in row.reason
        assert row.actor_id == actor.pk
        assert row.actor_label == f'{SEED_ACTOR_EMAIL} via seed_admin_access'

    def test_a_zone_that_is_already_linked_writes_nothing(self):
        _zone('IN-TG-HYD', 'Hyderabad')
        seed_admin_access()
        first = len(_rows(linking.ZONE_LINKED_ACTION))
        assert first == ServiceZone.objects.filter(geo_node__isnull=False).count() >= 1

        seed_admin_access()

        assert len(_rows(linking.ZONE_LINKED_ACTION)) == first

    def test_a_zone_with_no_node_is_reported_not_recorded(self):
        orphan = _zone('IN-TG-HYD-LOAD', 'Load zone')

        out = StringIO()
        seed_admin_access(stdout=out)

        assert 'IN-TG-HYD-LOAD' in out.getvalue()
        orphan.refresh_from_db()
        assert orphan.geo_node_id is None
        assert _rows_for(linking.ZONE_LINKED_ACTION, orphan.pk) == []

    def test_the_caller_is_named_in_the_row(self):
        zone = _zone('IN-TG-HYD', 'Hyderabad')
        seed_admin_access()
        zone.geo_node = None
        zone.save(update_fields=['geo_node'])

        linking.link_zones_to_nodes(ServiceZone, GeoNode, caller='a shell session')

        assert 'a shell session' in _rows(linking.ZONE_LINKED_ACTION)[-1].reason

    def test_without_a_context_the_row_is_still_written_and_the_gap_is_visible(self):
        zone = _zone('IN-TG-HYD', 'Hyderabad')
        seed_admin_access()
        ServiceZone.objects.update(geo_node=None)
        before = len(_rows(linking.ZONE_LINKED_ACTION))

        linking.link_zones_to_nodes(ServiceZone, GeoNode)

        rows = _rows_for(linking.ZONE_LINKED_ACTION, zone.pk)
        assert len(rows) == 2 and rows[-1].actor_id is None
        assert len(_rows(linking.ZONE_LINKED_ACTION)) == before + ServiceZone.objects.filter(
            geo_node__isnull=False).count()

    def test_an_audit_failure_rolls_the_link_back(self, monkeypatch):
        """A scope change that cannot be recorded must not happen quietly."""
        zone = _zone('IN-TG-HYD', 'Hyderabad')
        seed_admin_access()
        ServiceZone.objects.update(geo_node=None)
        monkeypatch.setattr('servers.admin_audit.services.record_admin_action',
                            lambda *a, **k: None)

        with pytest.raises(linking.LinkingAuditFailed):
            linking.link_zones_to_nodes(ServiceZone, GeoNode)

        zone.refresh_from_db()
        assert zone.geo_node_id is None
        assert not ServiceZone.objects.filter(geo_node__isnull=False).exists(), (
            'the whole batch rolls back, not just the zone whose row failed')


# ===========================================================================
# Driver home-city backfill leaves one row per driver
# ===========================================================================

class TestDriverHomeCityBackfillIsAudited:
    def test_each_backfilled_driver_gets_a_row(self):
        drivers = [_driver('+917770001101'), _driver('+917770001102')]
        actor = seed_actor()

        seed_admin_access()

        hyd = GeoNode.objects.get(code='IN-TG-HYD')
        for driver in drivers:
            driver.refresh_from_db()
            assert driver.home_city_id == hyd.pk
        rows = _rows(linking.DRIVER_HOME_CITY_ACTION)
        assert sorted(int(r.target_id) for r in rows) == sorted(d.pk for d in drivers)
        for row in rows:
            assert row.target_type == 'driver'
            assert row.before == {'home_city_id': None}
            assert row.after == {'home_city_id': hyd.pk, 'home_city_code': 'IN-TG-HYD'}
            assert row.actor_id == actor.pk
            assert row.actor_label == f'{SEED_ACTOR_EMAIL} via seed_admin_access'
            assert 'seed_admin_access' in row.reason

    def test_a_second_seed_writes_nothing_more(self):
        _driver('+917770001101')
        seed_admin_access()
        assert len(_rows(linking.DRIVER_HOME_CITY_ACTION)) == 1

        seed_admin_access()

        assert len(_rows(linking.DRIVER_HOME_CITY_ACTION)) == 1

    def test_with_two_live_cities_nobody_is_assigned_and_nothing_is_recorded(self):
        seed_admin_access()
        GeoNode.objects.filter(code='IN-TG-WGL').update(status=ACTIVE)
        driver = _driver('+917770001101')
        before = AdminAuditLog.objects.count()

        assigned, skipped = linking.backfill_driver_home_city(Driver, GeoNode)

        driver.refresh_from_db()
        assert (assigned, skipped) == (0, 1)
        assert driver.home_city_id is None
        assert AdminAuditLog.objects.count() == before

    def test_an_audit_failure_rolls_the_assignment_back(self, monkeypatch):
        seed_admin_access()
        driver = _driver('+917770001101')
        monkeypatch.setattr('servers.admin_audit.services.record_admin_action',
                            lambda *a, **k: None)

        with pytest.raises(linking.LinkingAuditFailed):
            linking.backfill_driver_home_city(Driver, GeoNode)

        driver.refresh_from_db()
        assert driver.home_city_id is None


# ===========================================================================
# The migration path: historical models, no audit, said out loud
# ===========================================================================

class TestTheMigrationPathIsTheDocumentedException:
    @pytest.fixture
    def historical(self):
        """The models as a migration sees them, at the current schema."""
        state = MigrationExecutor(connection).loader.project_state()
        return state.apps

    def test_historical_models_are_told_apart_from_live_ones(self, historical):
        assert linking.is_historical(historical.get_model('pricing', 'ServiceZone'))
        assert linking.is_historical(historical.get_model('driver', 'Driver'))
        assert not linking.is_historical(ServiceZone)
        assert not linking.is_historical(Driver)

    def test_zone_linking_from_a_migration_links_but_does_not_audit(self, historical, caplog):
        zone = _zone('IN-TG-HYD', 'Hyderabad')
        seed_admin_access()
        ServiceZone.objects.update(geo_node=None)
        before = AdminAuditLog.objects.count()

        with caplog.at_level(logging.WARNING, logger='servers.admin_access.linking'):
            linked, unmatched = linking.link_zones_to_nodes(
                historical.get_model('pricing', 'ServiceZone'),
                historical.get_model('admin_access', 'GeoNode'),
            )

        zone.refresh_from_db()
        assert linked >= 1 and unmatched == []
        assert zone.geo_node_id is not None
        assert AdminAuditLog.objects.count() == before
        assert 'NOT audited' in caplog.text

    def test_driver_backfill_from_a_migration_assigns_but_does_not_audit(self, historical, caplog):
        seed_admin_access()
        driver = _driver('+917770001101')
        before = AdminAuditLog.objects.count()

        with caplog.at_level(logging.WARNING, logger='servers.admin_access.linking'):
            assigned, skipped = linking.backfill_driver_home_city(
                historical.get_model('driver', 'Driver'),
                historical.get_model('admin_access', 'GeoNode'),
            )

        driver.refresh_from_db()
        assert (assigned, skipped) == (1, 0)
        assert driver.home_city_id is not None
        assert AdminAuditLog.objects.count() == before
        assert 'NOT audited' in caplog.text

    def test_the_two_migrations_say_so(self):
        import pathlib

        repo = pathlib.Path(__file__).resolve().parent.parent
        for rel in ('servers/pricing/migrations/0010_link_zones_to_geo_nodes.py',
                    'servers/driver/migrations/0027_backfill_driver_home_city.py'):
            text = (repo / rel).read_text(encoding='utf-8')
            assert 'not audited' in text.lower(), rel


# ===========================================================================
# Trip-count recompute: one summary row per run
# ===========================================================================

ACTOR_EMAIL = 'ops.lead@saaradhigo.test'


def _actor():
    return User.objects.create_user(phone_number='+917770009999', email=ACTOR_EMAIL,
                                    role='admin', is_staff=True)


def _recompute(*args):
    out = StringIO()
    call_command('recompute_driver_trip_counts', *args, stdout=out)
    return out.getvalue()


class TestRecomputeWritesOneSummaryRow:
    def test_one_row_per_apply_not_per_driver(self):
        actor = _actor()
        drifted = [_driver(f'+91777000{i:04d}') for i in range(3)]
        Driver.objects.filter(pk__in=[d.pk for d in drifted]).update(total_trips=7)
        _driver('+917770009000')   # already correct: 0 trips, counter 0

        _recompute('--apply', '--actor-email', ACTOR_EMAIL)

        rows = _rows(RECOMPUTE_ACTION)
        assert len(rows) == 1
        row = rows[0]
        assert row.target_type == 'driver_trip_counts'
        assert row.after['drivers_examined'] == 4
        assert row.after['drivers_changed'] == 3
        assert sorted(row.after['changed_driver_ids']) == sorted(d.pk for d in drifted)
        assert row.after['changed_driver_ids_truncated'] is False
        assert row.actor_id == actor.pk
        assert row.actor_label == f'{ACTOR_EMAIL} via recompute_driver_trip_counts'
        assert all(d.total_trips == 0 for d in Driver.objects.filter(pk__in=[d.pk for d in drifted]))

    def test_the_id_list_is_capped_but_the_count_is_not(self, monkeypatch):
        monkeypatch.setattr(
            'servers.driver.management.commands.recompute_driver_trip_counts.MAX_IDS_IN_ROW', 2)
        _actor()
        drifted = [_driver(f'+91777000{i:04d}') for i in range(5)]
        Driver.objects.filter(pk__in=[d.pk for d in drifted]).update(total_trips=9)

        _recompute('--apply', '--actor-email', ACTOR_EMAIL)

        row = _rows(RECOMPUTE_ACTION)[0]
        assert row.after['drivers_changed'] == 5
        assert len(row.after['changed_driver_ids']) == 2
        assert row.after['changed_driver_ids_listed'] == 2
        assert row.after['changed_driver_ids_truncated'] is True
        assert MAX_IDS_IN_ROW == 200

    def test_an_apply_with_no_drift_still_leaves_its_row(self):
        _actor()
        _driver('+917770009000')

        _recompute('--apply', '--actor-email', ACTOR_EMAIL)

        row = _rows(RECOMPUTE_ACTION)[0]
        assert row.after == {
            'drivers_examined': 1, 'drivers_changed': 0, 'changed_driver_ids': [],
            'changed_driver_ids_listed': 0, 'changed_driver_ids_truncated': False,
        }

    def test_a_report_run_writes_no_row(self):
        driver = _driver('+917770009000')
        Driver.objects.filter(pk=driver.pk).update(total_trips=3)

        _recompute()

        assert _rows(RECOMPUTE_ACTION) == []
        driver.refresh_from_db()
        assert driver.total_trips == 3

    def test_a_nameless_apply_is_refused_before_the_scan(self):
        _actor()
        driver = _driver('+917770009000')
        Driver.objects.filter(pk=driver.pk).update(total_trips=3)

        with pytest.raises(CommandError, match='--actor-email'):
            _recompute('--apply')

        driver.refresh_from_db()
        assert driver.total_trips == 3
        assert _rows(RECOMPUTE_ACTION) == []


# ===========================================================================
# Load fixtures: no audit, and nowhere but the load stack
# ===========================================================================

def _nothing_seeded():
    return (User.objects.count(), Driver.objects.count(), ServiceZone.objects.count())


class TestLoadFixturesRefuseOutsideTheLoadStack:
    @pytest.mark.parametrize('environment', ['qa', 'production', 'development', ''])
    def test_refused_before_any_write(self, environment, settings):
        settings.ENVIRONMENT = environment
        settings.IS_PRODUCTION = environment == 'production'
        settings.TEST_PHONE_NUMBERS = {'+919999000001': '100001'}
        before = _nothing_seeded()

        with pytest.raises(CommandError) as exc:
            call_command('seed_load_fixtures', '--riders', '1', '--drivers', '1',
                         stdout=StringIO())

        # Production is refused by the shared seed gate, everything else by
        # the load-stack check; either way the message names the environment.
        assert repr(environment) in str(exc.value)
        assert 'load' in str(exc.value)
        assert _nothing_seeded() == before

    def test_the_environment_variable_alone_does_not_open_the_gate(self, settings, monkeypatch):
        """The gate reads the setting, which is what the deployment booted with."""
        monkeypatch.setenv('ENVIRONMENT', 'load')
        settings.ENVIRONMENT = 'qa'
        settings.IS_PRODUCTION = False

        with pytest.raises(CommandError, match='load'):
            call_command('seed_load_fixtures', '--riders', '1', '--drivers', '1',
                         stdout=StringIO())

    def test_in_the_load_stack_it_seeds_and_audits_nothing(self, settings):
        settings.ENVIRONMENT = 'load'
        settings.IS_PRODUCTION = False
        settings.TEST_PHONE_NUMBERS = {'+919999000001': '100001'}
        before = AdminAuditLog.objects.count()

        out = StringIO()
        call_command('seed_load_fixtures', '--riders', '2', '--drivers', '2', stdout=out)

        assert 'seeded 2 riders, 2 approved drivers' in out.getvalue()
        assert Driver.objects.filter(approved=True).count() == 2
        assert AdminAuditLog.objects.count() == before

    def test_it_does_not_take_an_actor(self):
        """No audit means no actor: the gate is the control, by ruling."""
        from django.core.management import load_command_class

        parser = load_command_class('servers.auth_user', 'seed_load_fixtures').create_parser(
            'manage.py', 'seed_load_fixtures')
        flags = {flag for action in parser._actions for flag in action.option_strings}
        assert '--actor-email' not in flags


# ===========================================================================
# The seed's own summary stays readable
# ===========================================================================

def test_seed_output_counts_what_it_linked():
    _zone('IN-TG-HYD', 'Hyderabad')
    _driver('+917770001101')
    out = StringIO()

    seed_admin_access(stdout=out)

    text = out.getvalue()
    expected = ServiceZone.objects.filter(geo_node__isnull=False).count()
    assert f'zones linked: {expected}' in text and expected >= 1
    assert 'drivers given a home city: 1' in text
    assert GeoNode.objects.filter(kind=CITY, status=PAUSED).exists()
