"""Safety follow-ups (AUDIT 27.40): read-only City Ops, the coverage guard,
route deviation, and the capability list the console draws itself from.

Seed safety lives in tests/test_seed_never_modifies_accounts.py.
"""

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.admin_access import readiness
from servers.admin_access.bridge import granted
from servers.admin_access.models import ACTIVE, PAUSED, AdminAssignment, AdminRole, GeoNode
from servers.admin_access.roles import ALL_PERMISSIONS, ROLE_TEMPLATES
from servers.admin_audit.models import AdminAuditLog
from servers.command_centre.models import SAFETY_COVERAGE_GAP, OperatorAlert
from servers.ride.models import Trip, TripLocationPoint
from servers.ride.planned_route import store_planned_route_for
from servers.safety import coverage, detection, geometry
from servers.safety.models import SafetyCountryConfig, SafetySignal
from test_driver_active_trip_invariant import accept  # noqa: F401 - the real _accept_trip
from test_safety_module import (  # noqa: F401 - fixtures and helpers
    CONFIG, INCIDENTS, SIGNALS, incident_of, post, raise_sos, seed_config, world,
)
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    ROLES, _client_for, _driver, _rider, _trip, geo, ids_in, operator,
)

pytestmark = pytest.mark.django_db

User = get_user_model()
ME = '/api/v1/admin-access/me/'
ALERTS = '/api/v1/command-centre/admin/alerts/'
SOS_ADMIN = '/api/v1/sos/admin/'


def with_min(country='IN', minimum=1, **extra):
    return seed_config(country, min_safety_officers=minimum, **extra)


# ===========================================================================
# 1. City Ops: read-only Safety
# ===========================================================================

class TestCityOpsReadOnly:
    @pytest.fixture
    def pair(self, world):
        hyd = incident_of(raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id))
        aus = incident_of(raise_sos(world['aus_rider'], trip_id=world['aus_trip'].id))
        return hyd, aus

    def test_the_capability_and_who_holds_it(self, geo):
        holders = {t['code'] for t in ROLE_TEMPLATES if 'safety.read' in t['permissions']}
        assert holders == {'super_admin', 'city_ops_manager', 'safety_officer'}
        assert 'safety.respond' in AdminRole.objects.get(code='safety_officer').permissions

    def test_city_ops_sees_their_city_and_austin_is_not_found(self, geo, world, pair):
        hyd, aus = pair
        client, _ = operator('city_ops_manager', geo)

        assert ids_in(client.get(INCIDENTS)) == {hyd.id}
        assert client.get(f'{INCIDENTS}{hyd.id}/').status_code == 200
        assert client.get(f'{INCIDENTS}{aus.id}/').status_code == 404
        assert client.get(SIGNALS).status_code == 200
        assert client.get(CONFIG).status_code == 200

    def test_every_write_the_assignees_and_the_reveal_are_refused(self, geo, world, pair):
        hyd, _ = pair
        client, _ = operator('city_ops_manager', geo)
        event = hyd.sos_events.first()

        for action, body in (
            ('assign', {'assigned_to': 'me'}), ('note', {'body': 'x'}),
            ('status', {'status': 'investigating'}), ('link', {'kind': 'trip', 'id': 1}),
            ('unlink', {'kind': 'trip', 'id': 1}), ('refer-police', {}),
            ('close', {'summary': 's', 'outcome': 'no_action'}),
            ('suspend-driver', {'driver_id': 1, 'reason_code': 'safety'}),
            ('block-rider', {'rider_id': 1, 'reason_code': 'safety'}),
        ):
            assert post(client, f'{INCIDENTS}{hyd.id}/{action}/', body).status_code == 403, action
        assert post(client, INCIDENTS, {'category': 'other', 'severity': 'low',
                                         'title': 'x'}).status_code == 403
        assert client.get(f'{INCIDENTS}{hyd.id}/assignees/').status_code == 403
        assert post(client, '/api/v1/safety/admin/tickets/1/escalate/', {}).status_code == 403
        assert post(client, '/api/v1/safety/admin/signals/1/dismiss/', {'reason': 'x'}).status_code == 403
        assert post(client, '/api/v1/safety/admin/config/IN/', {'reason': 'x'}).status_code == 403
        # The SOS contact reveal needs safety.respond.
        assert post(client, f'{SOS_ADMIN}{event.id}/reveal/', {'reason': 'x'}).status_code == 403

    def test_the_case_file_offers_no_action_and_unmasks_nobody(self, geo, world, pair):
        hyd, _ = pair
        client, _ = operator('city_ops_manager', geo)

        response = client.get(f'{INCIDENTS}{hyd.id}/')
        data = response.json()['data']
        text = response.content.decode()

        assert data['allowed_actions'] == [] and data['read_only'] is True
        assert not any(v is True for v in data['actions'].values())
        assert not any(d['can_suspend'] for d in data['people']['drivers'])
        assert not any(r['can_block'] for r in data['people']['riders'])
        assert '+919876700001' not in text and '9876700001' not in text
        assert data['sos_events'] and all(
            row['user_phone'] != '+919876700001' for row in data['sos_events'])

    def test_the_safety_officer_still_gets_the_actions(self, geo, world, pair):
        hyd, _ = pair
        data = world['hyd_safety'][0].get(f'{INCIDENTS}{hyd.id}/').json()['data']

        assert data['read_only'] is False
        assert {'assign', 'note', 'status', 'refer_police'} <= set(data['allowed_actions'])

    @pytest.mark.parametrize('role', ['marketing', 'analyst', 'kyc_verifier', 'finance',
                                      'support_agent', 'support_lead'])
    def test_every_other_role_stays_refused(self, geo, world, pair, role):
        hyd, _ = pair
        client, _ = operator(role, geo)

        assert client.get(INCIDENTS).status_code == 403
        assert client.get(f'{INCIDENTS}{hyd.id}/').status_code == 403
        assert client.get(SIGNALS).status_code == 403
        assert client.get(CONFIG).status_code == 403


# ===========================================================================
# 2. Safety coverage guard
# ===========================================================================

def officer(geo, scope='IN-TG-HYD', **extra):
    client, user = operator('safety_officer', geo, scope_code=scope)
    for field, value in extra.items():
        setattr(user, field, value)
    if extra:
        user.save()
    return user


def gap_alerts():
    return OperatorAlert.objects.filter(kind=SAFETY_COVERAGE_GAP)


class TestCoverageGuard:
    def test_going_live_is_refused_with_the_failing_item(self, geo):
        with_min('IN', 1)
        wgl = GeoNode.objects.get(code='IN-TG-WGL')
        assert wgl.status == PAUSED

        with pytest.raises(readiness.CityNotReady) as refused:
            readiness.set_city_status(wgl, ACTIVE, actor=None, reason='launch')

        assert refused.value.code == 'CITY_NOT_READY'
        assert [i['key'] for i in refused.value.items] == ['safety_coverage']
        assert refused.value.items[0]['ok'] is False
        assert GeoNode.objects.get(code='IN-TG-WGL').status == PAUSED

    def test_going_live_succeeds_when_covered_and_is_audited(self, geo, world):
        with_min('IN', 1)
        officer(geo, scope='IN-TG')          # a state assignment covers Warangal
        wgl = GeoNode.objects.get(code='IN-TG-WGL')
        actor = world['super'][1]

        node, changed = readiness.set_city_status(wgl, ACTIVE, actor=actor, reason='Launch Warangal')

        assert changed and node.status == ACTIVE
        row = AdminAuditLog.objects.filter(action='city_status_changed').latest('id')
        assert row.reason == 'Launch Warangal' and row.actor_id == actor.id
        assert row.after['status_to'] == ACTIVE
        assert readiness.set_city_status(wgl, ACTIVE, actor=actor, reason='again')[1] is False

    def test_pausing_is_always_allowed(self, geo, world):
        node, changed = readiness.set_city_status(
            GeoNode.objects.get(code='IN-TG-HYD'), PAUSED, actor=world['super'][1], reason='pause')
        assert changed and node.status == PAUSED

    def test_a_country_with_no_config_is_not_ready(self, geo):
        assert not SafetyCountryConfig.objects.exists()
        items = readiness.city_readiness(GeoNode.objects.get(code='IN-TG-WGL'))

        assert items[0]['key'] == 'safety_coverage' and items[0]['ok'] is False
        assert 'not configured' in items[0]['detail']
        with pytest.raises(readiness.CityNotReady):
            readiness.set_city_status(GeoNode.objects.get(code='IN-TG-WGL'), ACTIVE,
                                      actor=None, reason='x')

    def test_a_blank_minimum_is_not_a_pass(self, geo):
        seed_config('IN', min_safety_officers=None)
        item = readiness.city_readiness(GeoNode.objects.get(code='IN-TG-HYD'))[0]
        assert item['ok'] is False and 'not configured' in item['detail']

    def test_only_safety_officers_count(self, geo):
        with_min('IN', 1)
        hyd = GeoNode.objects.get(code='IN-TG-HYD')
        operator('super_admin', geo)          # a Super Admin does not count
        officer(geo, scope='US-TX-AUS')       # nor does another city's officer
        assert coverage.coverage(hyd)['count'] == 0

        officer(geo, scope='IN-TG-HYD', is_active=False)
        officer(geo, scope='IN-TG-HYD', is_break_glass=True)
        officer(geo, scope='IN-TG-HYD', is_staff=False)
        assert coverage.coverage(hyd)['count'] == 0

        officer(geo, scope='IN')              # a country assignment covers the city
        assert coverage.coverage(hyd)['count'] == 1
        officer(geo, scope='US-TX-AUS')       # another country does not
        assert coverage.coverage(hyd)['count'] == 1

    def test_the_gap_alert_is_raised_once_and_cleared_when_fixed(self, geo):
        with_min('IN', 1)

        first = coverage.sweep()
        second = coverage.sweep()

        assert first['raised'] == 1 and second['raised'] == 0
        alert = gap_alerts().get()
        assert alert.severity == 'critical' and alert.city.code == 'IN-TG-HYD'
        assert alert.acknowledged_at is None
        # No PII: a city, a country and counts.
        assert set(alert.payload) == {'city', 'country', 'officers', 'minimum', 'configured'}

        officer(geo, scope='IN-TG-HYD')
        third = coverage.sweep()

        assert third['cleared'] == 1
        alert.refresh_from_db()
        assert alert.acknowledged_at is not None and alert.acknowledged_by_id is None
        assert coverage.sweep() == {'checked': 1, 'raised': 0, 'cleared': 0}
        # It comes back if the cover goes away again.
        AdminAssignment.objects.filter(role__code='safety_officer', scope__code='IN-TG-HYD'
                                       ).update(is_active=False)
        assert coverage.sweep()['raised'] == 1
        assert gap_alerts().count() == 2

    def test_a_paused_city_raises_nothing(self, geo, world):
        with_min('IN', 1)
        GeoNode.objects.filter(code='IN-TG-HYD').update(status=PAUSED)

        assert coverage.sweep() == {'checked': 0, 'raised': 0, 'cleared': 0}
        assert not gap_alerts().exists()

    def test_pausing_a_city_clears_its_open_gap(self, geo):
        with_min('IN', 1)
        coverage.sweep()
        readiness.set_city_status(GeoNode.objects.get(code='IN-TG-HYD'), PAUSED,
                                  actor=operator('super_admin', geo)[1], reason='pause')

        assert coverage.sweep()['cleared'] == 1

    def test_no_config_raises_the_alert_as_not_configured(self, geo):
        coverage.sweep()
        alert = gap_alerts().get()
        assert alert.payload['configured'] is False and alert.payload['minimum'] is None
        assert 'not configured' in alert.title

    def test_it_is_visible_with_the_citys_other_alerts_and_cannot_be_acknowledged(self, geo):
        with_min('IN', 1)
        coverage.sweep()
        alert = gap_alerts().get()

        for role in ('analyst', 'city_ops_manager', 'marketing', 'super_admin'):   # all hold report.read
            client, _ = operator(role, geo)
            kinds = {a['kind'] for a in client.get(ALERTS).json()['data']['results']}
            assert SAFETY_COVERAGE_GAP in kinds, role
        # Out of scope: an Austin-only operator does not see Hyderabad's gap.
        austin, _ = operator('analyst', geo, scope_code='US-TX-AUS')
        assert SAFETY_COVERAGE_GAP not in {
            a['kind'] for a in austin.get(ALERTS).json()['data']['results']}
        admin = operator('super_admin', geo)[0]
        assert post(admin, f'{ALERTS}{alert.id}/acknowledge/', {}).status_code == 403

    def test_the_minimum_is_a_versioned_audited_config_field(self, geo, world):
        with_min('IN', 1)
        admin = world['super'][0]

        done = post(admin, f'{CONFIG}IN/', {'min_safety_officers': 2, 'reason': 'two per city'})
        assert done.status_code == 200
        assert SafetyCountryConfig.objects.get(country='IN', is_active=True).min_safety_officers == 2
        assert SafetyCountryConfig.objects.filter(country='IN').count() == 2
        assert AdminAuditLog.objects.filter(action='safety_config_changed').exists()
        assert post(admin, f'{CONFIG}IN/', {'min_safety_officers': -1,
                                             'reason': 'x'}).status_code == 400
        # Cleared means "not configured", not "no minimum".
        assert post(admin, f'{CONFIG}IN/', {'min_safety_officers': None,
                                             'reason': 'off'}).status_code == 200
        assert readiness.city_readiness(GeoNode.objects.get(code='IN-TG-HYD'))[0]['ok'] is False

    def test_the_sweep_is_on_the_beat(self):
        from django.conf import settings

        tasks = {v['task'] for v in settings.CELERY_BEAT_SCHEDULE.values()}
        assert 'safety.coverage_sweep' in tasks

    def test_set_city_status_is_the_only_writer_of_geonode_status(self):
        """Seeds create a city already active (create-if-missing); nothing else
        in the tree assigns or updates GeoNode.status."""
        root = Path(__file__).resolve().parent.parent / 'servers'
        pattern = re.compile(
            r'(\b(city|node|geo_node|geonode)\.status\s*=[^=])|'
            r'(GeoNode\.objects[^\n]*\.update\([^)]*status)')
        offenders = []
        for path in root.rglob('*.py'):
            if 'migrations' in path.parts or path.name == 'readiness.py':
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f'{path.relative_to(root.parent)}:{number}: {line.strip()}')
        assert offenders == []

    def test_the_seed_does_not_flip_an_existing_city(self, geo):
        from conftest import seed_admin_access

        GeoNode.objects.filter(code='IN-TG-HYD').update(status=PAUSED)
        GeoNode.objects.filter(code='IN-TG-WGL').update(status=ACTIVE)

        seed_admin_access()

        assert GeoNode.objects.get(code='IN-TG-HYD').status == PAUSED
        assert GeoNode.objects.get(code='IN-TG-WGL').status == ACTIVE


# ===========================================================================
# 3. Route deviation
# ===========================================================================

def encode(points):
    """Google's encoded polyline (precision 5) for the tests."""
    out, last_lat, last_lng = [], 0, 0
    for lat, lng in points:
        for value, prev in ((round(lat * 1e5), last_lat), (round(lng * 1e5), last_lng)):
            delta = value - prev
            delta = ~(delta << 1) if delta < 0 else delta << 1
            while delta >= 0x20:
                out.append(chr((0x20 | (delta & 0x1F)) + 63))
                delta >>= 5
            out.append(chr(delta + 63))
        last_lat, last_lng = round(lat * 1e5), round(lng * 1e5)
    return ''.join(out)


LINE = [(17.30, 78.30), (17.49, 78.49)]
POLYLINE = encode(LINE)
DIRECTIONS = {'status': 'OK', 'routes': [{'overview_polyline': {'points': POLYLINE}}]}


class TestGeometry:
    def test_decode_round_trips(self):
        decoded = geometry.decode_polyline(POLYLINE)
        assert [(round(a, 5), round(b, 5)) for a, b in decoded] == LINE

    def test_distance_to_the_line(self):
        line = geometry.decode_polyline(POLYLINE)
        assert geometry.distance_to_polyline_m(17.40, 78.40, line) < 1
        off = geometry.distance_to_polyline_m(17.40, 78.45, line)
        assert 3500 < off < 4000
        assert geometry.distance_to_polyline_m(17.0, 78.0, []) is None
        assert geometry.decode_polyline('') == [] and geometry.decode_polyline(None) == []


def moving_trip(world, points, *, polyline=POLYLINE, alive=True):
    """An in-progress Hyderabad trip with a stored route and a driver trail.
    `points` is `[(minutes_ago, lat, lng), ...]`."""
    now = timezone.now()
    trip = world['hyd_trip']
    Trip.objects.filter(pk=trip.pk).update(
        started_at=now - timedelta(minutes=30), estimated_duration_min=Decimal('120'),
        pickup_lat=Decimal('17.30'), pickup_long=Decimal('78.30'),
        destination_lat=Decimal('17.49'), destination_long=Decimal('78.49'),
        planned_route_polyline=polyline, planned_route_source='directions',
        planned_route_at=now - timedelta(minutes=30),
        last_driver_activity_at=now - timedelta(seconds=20) if alive else now - timedelta(hours=1))
    for sequence, (minutes_ago, lat, lng) in enumerate(points, 1):
        TripLocationPoint.objects.create(
            trip=trip, driver=world['hyd_driver'], latitude=Decimal(str(lat)),
            longitude=Decimal(str(lng)), recorded_at=now - timedelta(minutes=minutes_ago),
            sequence=sequence)
    trip.refresh_from_db()
    return trip


DEVIATION = dict(route_deviation_meters=300, route_deviation_minutes=3)
OFF = 78.45          # about 3.7 km from the line
NEAR = 78.4005       # a few metres from the line


class TestRouteDeviation:
    def test_flagged_above_the_distance_for_longer_than_the_time(self, geo, world):
        seed_config('IN', **DEVIATION)
        moving_trip(world, [(9, 17.40, 78.40), (6, 17.40, OFF), (4, 17.405, OFF), (1, 17.41, OFF)])

        summary = detection.evaluate()

        signal = SafetySignal.objects.get(kind='route_deviation')
        assert summary['created'] == 1
        assert signal.measured['off_route_minutes'] > 3 and signal.measured['max_distance_m'] > 300
        assert signal.measured['threshold_m'] == 300 and signal.incident_id is None
        listed = world['hyd_safety'][0].get(SIGNALS).json()['data']
        assert [s['kind'] for s in listed['results']] == ['route_deviation']
        assert 'route_deviation' not in listed       # the "not measured yet" stub is gone

    def test_not_flagged_when_off_the_route_for_less_than_the_time(self, geo, world):
        seed_config('IN', **DEVIATION)
        moving_trip(world, [(6, 17.40, 78.40), (2, 17.40, OFF), (1, 17.405, OFF)])

        assert detection.evaluate()['created'] == 0

    def test_not_flagged_when_back_on_the_route(self, geo, world):
        seed_config('IN', **DEVIATION)
        moving_trip(world, [(8, 17.40, OFF), (6, 17.40, OFF), (1, 17.40, 78.40)])

        assert detection.evaluate()['created'] == 0

    def test_not_flagged_below_the_distance(self, geo, world):
        seed_config('IN', **DEVIATION)
        moving_trip(world, [(9, 17.40, NEAR), (6, 17.401, NEAR), (1, 17.402, NEAR)])

        detection.evaluate()
        assert not SafetySignal.objects.filter(kind='route_deviation').exists()

    def test_a_parked_driver_off_the_route_is_seen_from_their_last_point(self, geo, world):
        seed_config('IN', **DEVIATION)
        moving_trip(world, [(10, 17.40, OFF)])      # one kept point, driver still alive

        detection.evaluate()

        assert SafetySignal.objects.filter(kind='route_deviation').count() == 1

    def test_a_silent_driver_is_lost_contact_not_a_deviation(self, geo, world):
        seed_config('IN', **DEVIATION)
        moving_trip(world, [(10, 17.40, OFF)], alive=False)

        assert detection.evaluate()['created'] == 0

    def test_no_stored_polyline_means_no_signal(self, geo, world):
        seed_config('IN', **DEVIATION)
        moving_trip(world, [(9, 17.40, OFF), (6, 17.40, OFF), (1, 17.41, OFF)], polyline=None)

        assert detection.evaluate()['created'] == 0
        assert not SafetySignal.objects.exists()

    def test_no_config_for_the_country_means_no_signal(self, geo, world):
        moving_trip(world, [(9, 17.40, OFF), (6, 17.40, OFF), (1, 17.41, OFF)])

        summary = detection.evaluate()

        assert summary['created'] == 0 and not SafetySignal.objects.exists()

    def test_blank_deviation_numbers_mean_off(self, geo, world):
        seed_config('IN', route_deviation_meters=None, route_deviation_minutes=None)
        moving_trip(world, [(9, 17.40, OFF), (6, 17.40, OFF), (1, 17.41, OFF)])

        assert detection.evaluate()['created'] == 0

    def test_another_countrys_numbers_are_not_borrowed(self, geo, world):
        seed_config('US', **DEVIATION)               # Hyderabad is India
        moving_trip(world, [(9, 17.40, OFF), (6, 17.40, OFF), (1, 17.41, OFF)])

        assert detection.evaluate()['created'] == 0

    def test_the_evaluator_makes_no_network_call(self, geo, world):
        seed_config('IN', **DEVIATION)
        moving_trip(world, [(5, 17.40, OFF), (4, 17.40, OFF), (1, 17.41, OFF)])
        with mock.patch('requests.get', side_effect=AssertionError('network')), \
                mock.patch('requests.post', side_effect=AssertionError('network')):
            detection.evaluate()
        assert SafetySignal.objects.filter(kind='route_deviation').count() == 1


@pytest.fixture
def accepting(db):
    """A requested trip, an approved driver with a vehicle, ready for _accept_trip."""
    from servers.driver.models import Driver, Vehicle, VehicleType
    from servers.ride.models import TripStatus

    rider = User.objects.create_user(phone_number='+919300000777', role='rider')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    status, _ = TripStatus.objects.get_or_create(status_code='requested')
    trip = Trip.objects.create(
        user_id=rider, status_id=status, requested_vehicle_type=vt,
        pickup_lat=Decimal('17.30'), pickup_long=Decimal('78.30'),
        destination_lat=Decimal('17.49'), destination_long=Decimal('78.49'),
        pickup_address='P', destination_address='D', estimated_fare=Decimal('150.00'))
    user = User.objects.create_user(phone_number='+919400000777', role='driver')
    driver = Driver.objects.create(user_id=user, approved=True, status='online')
    driver.active_vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vt, vehicle_number='TS09ZZ0777')
    driver.save(update_fields=['active_vehicle'])
    return trip, user


@pytest.fixture
def delivered_inline(monkeypatch):
    """Celery runs inline: `.delay` calls the task body."""
    calls = []

    def delay(trip_id):
        calls.append(trip_id)
        return store_planned_route_for(trip_id)

    monkeypatch.setattr('servers.ride.tasks.store_planned_route.delay', delay)
    return calls


class TestPlannedRouteOnAccept:
    def test_the_polyline_is_stored_after_the_accept_commits(
            self, accepting, delivered_inline, django_capture_on_commit_callbacks):
        trip, driver_user = accepting
        with mock.patch('servers.ride.maps_utils.google_directions',
                        return_value=DIRECTIONS) as maps, \
                mock.patch('servers.redis_client.get_cached_map_data', return_value=None):
            with django_capture_on_commit_callbacks(execute=False) as callbacks:
                result = accept(trip, driver_user)

            assert result['success'] is True
            trip.refresh_from_db()
            # Nothing was fetched inside the accept transaction.
            assert maps.call_count == 0 and trip.planned_route_polyline is None
            assert len(callbacks) >= 1

            for callback in callbacks:
                callback()

        trip.refresh_from_db()
        assert delivered_inline == [trip.id]
        assert trip.planned_route_polyline == POLYLINE
        assert trip.planned_route_source == 'directions' and trip.planned_route_at is not None
        assert maps.call_count == 1
        assert maps.call_args.args == (17.3, 78.3, 17.49, 78.49)    # pickup to drop

    def test_a_maps_failure_leaves_it_null_and_the_accept_succeeds(
            self, accepting, delivered_inline, django_capture_on_commit_callbacks):
        trip, driver_user = accepting
        with mock.patch('servers.ride.maps_utils.google_directions', return_value=None), \
                mock.patch('servers.redis_client.get_cached_map_data', return_value=None):
            with django_capture_on_commit_callbacks(execute=True):
                result = accept(trip, driver_user)

        trip.refresh_from_db()
        assert result['success'] is True and trip.driver_id is not None
        assert trip.status_id.status_code == 'accepted'
        assert trip.planned_route_polyline is None and trip.planned_route_at is None

    def test_a_maps_exception_never_reaches_the_accept(
            self, accepting, delivered_inline, django_capture_on_commit_callbacks):
        trip, driver_user = accepting
        with mock.patch('servers.ride.maps_utils.google_directions', side_effect=RuntimeError('boom')), \
                mock.patch('servers.redis_client.get_cached_map_data', return_value=None):
            with django_capture_on_commit_callbacks(execute=True):
                result = accept(trip, driver_user)

        trip.refresh_from_db()
        assert result['success'] is True and trip.planned_route_polyline is None

    def test_a_broker_that_is_down_does_not_fail_the_accept(
            self, accepting, monkeypatch, django_capture_on_commit_callbacks):
        trip, driver_user = accepting

        def down(trip_id):
            raise ConnectionError('broker down')

        monkeypatch.setattr('servers.ride.tasks.store_planned_route.delay', down)
        with django_capture_on_commit_callbacks(execute=True):
            result = accept(trip, driver_user)

        assert result['success'] is True

    def test_a_cached_directions_polyline_is_reused_without_a_second_call(
            self, accepting, delivered_inline, django_capture_on_commit_callbacks):
        trip, driver_user = accepting
        with mock.patch('servers.ride.maps_utils.google_directions') as maps, \
                mock.patch('servers.redis_client.get_cached_map_data',
                           return_value=DIRECTIONS) as cache:
            with django_capture_on_commit_callbacks(execute=True):
                accept(trip, driver_user)

        trip.refresh_from_db()
        assert maps.call_count == 0
        assert cache.call_args.args[0] == 'maps:directions:17.3,78.3:17.49,78.49'
        assert trip.planned_route_polyline == POLYLINE and trip.planned_route_source == 'maps_cache'

    def test_a_trip_that_already_has_a_route_is_not_fetched_again(self, accepting):
        trip, _ = accepting
        Trip.objects.filter(pk=trip.pk).update(planned_route_polyline='abc')
        with mock.patch('servers.ride.maps_utils.google_directions') as maps:
            assert store_planned_route_for(trip.id) is None
        assert maps.call_count == 0

    def test_nothing_rider_or_driver_facing_carries_the_route(self):
        from servers.ride import serializers

        source = Path(serializers.__file__).read_text(encoding='utf-8')
        assert 'planned_route' not in source


# ===========================================================================
# 5. The capability list the console draws itself from
# ===========================================================================

class TestCapabilityList:
    @pytest.mark.parametrize('role', ROLES)
    def test_me_lists_exactly_what_the_bridge_grants(self, geo, role):
        client, user = operator(role, geo)

        data = client.get(ME).json()['data']
        expected = sorted(p for p in ALL_PERMISSIONS if granted(user, p)[0])

        assert data['permissions'] == expected, role
        template = next(t for t in ROLE_TEMPLATES if t['code'] == role)
        assert set(data['permissions']) == set(template['permissions']) or role == 'super_admin'
        entry = data['roles'][0]
        assert entry['role'] == role and entry['scope_path'].endswith('/')
        assert set(entry['permissions']) == set(template['permissions'])

    def test_an_unmigrated_operator_is_listed_what_the_legacy_path_grants(self, geo):
        user = User.objects.create_user(
            phone_number='+919555000321', username='+919555000321', email='legacy@saaradhigo.test',
            role='admin', is_staff=True, ops_role='support')
        data = _client_for(user).get(ME).json()['data']

        assert data['access_model'] == 'legacy_unscoped' and data['unrestricted'] is True
        assert data['permissions'] == sorted(p for p in ALL_PERMISSIONS if granted(user, p)[0])
        assert 'support.reply' in data['permissions']

    def test_it_names_capabilities_and_places_and_nothing_else(self, geo):
        client, _ = operator('city_ops_manager', geo)
        data = client.get(ME).json()['data']

        assert data['unrestricted'] is False
        assert {n['code'] for n in data['scopes']} == {'IN-TG-HYD'}
        assert data['scopes'][0]['path'] == '/IN/IN-TG/IN-TG-HYD/' or \
            data['scopes'][0]['path'].endswith('IN-TG-HYD/')
