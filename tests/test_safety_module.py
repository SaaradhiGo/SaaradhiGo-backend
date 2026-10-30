"""Safety module (operator console, module 6): incidents, escalation, signals.

Over real HTTP with a JWT, for the seeded roles. Every operator is at
Hyderabad (`IN-TG-HYD`) unless a test says otherwise; Super Admin is at Global;
"another city" is Austin. An object outside an operator's scope is 404, never
403; in scope without the capability is 403.
"""

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from servers.admin_access.models import AdminAssignment, AdminRole
from servers.admin_audit.models import AdminAuditLog
from servers.command_centre.models import (
    SAFETY_INCIDENT_UNASSIGNED, SAFETY_SIGNAL, SAFETY_SOS_ESCALATED, OperatorAlert,
)
from servers.driver.models import Driver
from servers.ride.models import Trip, TripLocationPoint
from servers.rider.models import RiderBlock
from servers.safety import config as safety_config
from servers.safety import detection, escalation, services
from servers.safety.models import (
    SafetyCountryConfig, SafetyIncident, SafetyIncidentUpdate, SafetySignal,
)
from servers.safety.scope import incidents_visible_to, responders_for
from servers.sos.models import SOSEvent
from servers.sos.tasks import dispatch_sos
from servers.support.models import SupportTicket
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    ROLES, _client_for, _driver, _rider, _status, _trip, _zone, geo, ids_in, operator,
)

pytestmark = pytest.mark.django_db

User = get_user_model()

INCIDENTS = '/api/v1/safety/admin/incidents/'
SIGNALS = '/api/v1/safety/admin/signals/'
CONFIG = '/api/v1/safety/admin/config/'
SOS = '/api/v1/sos/'
SOS_ADMIN = '/api/v1/sos/admin/'

SAFETY_ROLES = {'super_admin', 'safety_officer'}

AUSTIN_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-97.85, 30.15], [-97.60, 30.15], [-97.60, 30.40],
                     [-97.85, 30.40], [-97.85, 30.15]]],
}

IN_NUMBERS = [{'label': 'unified', 'number': '112'}]
IN_CONFIG = dict(
    sos_ack_minutes=2, incident_assign_minutes=5, long_stop_minutes=8, long_stop_radius_m=50,
    stop_exempt_radius_m=150, long_stop_incident_minutes=20, eta_overrun_percent=50,
    eta_overrun_minutes=15, eta_overrun_incident_percent=100, eta_overrun_incident_minutes=30,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def world(geo):
    hyd_zone = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus_zone = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
                     polygon_geojson=AUSTIN_POLYGON)
    hyd_rider = _rider('+919876700001', 'Hyd Rider')
    aus_rider = _rider('+15125570001', 'Austin Rider')
    hyd_driver = _driver('+919876700002', 'Hyd Driver', geo['IN-TG-HYD'])
    aus_driver = _driver('+15125570002', 'Austin Driver', geo['US-TX-AUS'])
    return {
        'hyd_zone': hyd_zone, 'aus_zone': aus_zone,
        'hyd_rider': hyd_rider, 'aus_rider': aus_rider,
        'hyd_driver': hyd_driver, 'aus_driver': aus_driver,
        'hyd_trip': _trip(hyd_rider, hyd_zone, hyd_driver),
        'aus_trip': _trip(aus_rider, aus_zone, aus_driver),
        'hyd_safety': operator('safety_officer', geo),
        'aus_safety': operator('safety_officer', geo, scope_code='US-TX-AUS'),
        'super': operator('super_admin', geo),
    }


def raise_sos(user, **body):
    client = _client_for(user)
    response = client.post(SOS, {'event_type': 'panic', **body}, format='json')
    assert response.status_code == 201, response.content
    return SOSEvent.objects.get(id=response.json()['data']['sos_id'])


def incident_of(event):
    event.refresh_from_db()
    assert event.incident_id, 'every SOS has a case'
    return SafetyIncident.objects.get(id=event.incident_id)


def seed_config(country='IN', **overrides):
    values = {**IN_CONFIG, **overrides}
    return SafetyCountryConfig.objects.create(
        country=country, version=1, is_active=True, emergency_numbers=IN_NUMBERS
        if country == 'IN' else [{'label': 'unified', 'number': '911'}], **values)


def post(client, path, body=None):
    return client.post(path, body or {}, format='json')


def audit_count(action=None):
    qs = AdminAuditLog.objects.all()
    return qs.filter(action=action).count() if action else qs.count()


@pytest.fixture
def pushes(monkeypatch):
    """Every push the SOS and escalation paths try to send."""
    sent = []

    def fake(user, title, body, data=None):
        sent.append({'user': user.id, 'title': title, 'data': data or {}})
        return True

    monkeypatch.setattr('servers.auth_user.services.send_push_notification', fake)
    return sent


def with_token(user):
    user.fcm_token = f'tok-{user.id}'
    user.save(update_fields=['fcm_token'])
    return user


# ---------------------------------------------------------------------------
# Scope and capability
# ---------------------------------------------------------------------------

class TestScopeAndCapability:
    @pytest.fixture
    def pair(self, world):
        hyd = incident_of(raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id))
        aus = incident_of(raise_sos(world['aus_rider'], trip_id=world['aus_trip'].id))
        return hyd, aus

    def test_a_safety_officer_sees_their_city_only(self, world, pair):
        hyd, aus = pair
        client = world['hyd_safety'][0]

        assert ids_in(client.get(INCIDENTS)) == {hyd.id}
        assert client.get(f'{INCIDENTS}{hyd.id}/').status_code == 200
        assert client.get(f'{INCIDENTS}{aus.id}/').status_code == 404

    def test_out_of_scope_is_not_found_for_every_write(self, world, pair):
        _, aus = pair
        client = world['hyd_safety'][0]
        for action, body in (
            ('assign', {'assigned_to': 'me'}), ('note', {'body': 'x'}),
            ('status', {'status': 'investigating'}), ('link', {'kind': 'trip', 'id': 1}),
            ('unlink', {'kind': 'trip', 'id': 1}), ('refer-police', {}),
            ('close', {'summary': 's', 'outcome': 'no_action'}),
            ('suspend-driver', {'driver_id': 1, 'reason_code': 'safety'}),
            ('block-rider', {'rider_id': 1, 'reason_code': 'safety'}),
        ):
            assert post(client, f'{INCIDENTS}{aus.id}/{action}/', body).status_code == 404, action
        assert client.get(f'{INCIDENTS}{aus.id}/assignees/').status_code == 404

    def test_super_admin_sees_both(self, world, pair):
        hyd, aus = pair
        client = world['super'][0]

        assert ids_in(client.get(INCIDENTS)) == {hyd.id, aus.id}
        assert client.get(f'{INCIDENTS}{aus.id}/').status_code == 200

    @pytest.mark.parametrize('role', ROLES)
    def test_every_role_on_the_queue_and_the_case(self, geo, world, pair, role):
        client, _ = operator(role, geo)
        hyd, _ = pair
        # The four GETs admit `safety.read` too (City Ops, read-only); every
        # write stays `safety.respond`.
        may_look = role in SAFETY_ROLES or role == 'city_ops_manager'
        wanted = 200 if may_look else 403

        assert client.get(INCIDENTS).status_code == wanted, role
        assert client.get(f'{INCIDENTS}{hyd.id}/').status_code == wanted, role
        assert client.get(SIGNALS).status_code == wanted, role
        assert client.get(CONFIG).status_code == wanted, role
        assert post(client, f'{INCIDENTS}{hyd.id}/note/', {'body': 'x'}).status_code in (
            (201, 200) if role in SAFETY_ROLES else (403,)), role

    def test_city_ops_reads_the_queue_but_holds_no_write_capability(self, geo, world, pair):
        """City Ops gets the read-only door (safety.read) and nothing else:
        the full read-only matrix is in tests/test_safety_followups.py."""
        client, _ = operator('city_ops_manager', geo)
        assert client.get(INCIDENTS).status_code == 200
        permissions = AdminRole.objects.get(code='city_ops_manager').permissions
        assert 'safety.respond' not in permissions
        assert 'safety.read' in permissions

    def test_safety_config_is_super_admin_only_by_default(self):
        from servers.admin_access.roles import ROLE_TEMPLATES

        holders = {t['code'] for t in ROLE_TEMPLATES if 'safety.config' in t['permissions']}
        assert holders == {'super_admin'}

    def test_the_scope_param_narrows_but_never_widens(self, world, pair):
        hyd, aus = pair
        client = world['super'][0]
        assert ids_in(client.get(f'{INCIDENTS}?scope=US-TX-AUS')) == {aus.id}
        assert world['hyd_safety'][0].get(f'{INCIDENTS}?scope=US-TX-AUS').status_code == 403

    def test_the_one_helper_decides_for_the_list_and_the_case(self, world, pair):
        hyd, aus = pair
        _, user = world['hyd_safety']
        assert set(incidents_visible_to(user).values_list('id', flat=True)) == {hyd.id}


class TestUnanchored:
    def test_an_unanchored_incident_is_visible_to_every_scoped_responder(self, world):
        rider = _rider('+919876700099', 'Lost Rider')
        incident = incident_of(raise_sos(rider))         # no trip, no fix, no home city

        assert incident.anchor_source == 'unanchored' and incident.anchor_node is None
        for who in ('hyd_safety', 'aus_safety', 'super'):
            client = world[who][0]
            assert incident.id in ids_in(client.get(INCIDENTS)), who
            assert client.get(f'{INCIDENTS}{incident.id}/').status_code == 200, who
        assert incident.id in ids_in(world['aus_safety'][0].get(f'{INCIDENTS}?scope=US-TX-AUS'))

    def test_any_responder_can_act_on_it(self, world):
        incident = incident_of(raise_sos(_rider('+919876700098', 'Lost Rider 2')))
        client = world['aus_safety'][0]

        assert post(client, f'{INCIDENTS}{incident.id}/assign/', {'assigned_to': 'me'}).status_code == 200
        assert post(client, f'{INCIDENTS}{incident.id}/note/', {'body': 'on it'}).status_code == 201

    def test_no_one_without_the_capability_sees_it(self, geo, world):
        incident = incident_of(raise_sos(_rider('+919876700097', 'Lost Rider 3')))
        for role in ('support_agent', 'analyst', 'marketing'):
            client, _ = operator(role, geo)
            assert client.get(f'{INCIDENTS}{incident.id}/').status_code == 403, role

    def test_a_rider_known_only_by_their_phone_is_visible_within_their_country(self, geo, world):
        """Anchored at the country from the phone: any responder inside it sees it."""
        rider = _rider('+919876700096', 'Phone Only Rider')
        incident, _ = services.open_incident(
            source='manual', category='other', severity='medium', title='Country anchored',
            rider_user=rider)

        assert incident.anchor_source == 'rider_country'
        assert incident.id in ids_in(world['hyd_safety'][0].get(INCIDENTS))      # India
        assert incident.id not in ids_in(world['aus_safety'][0].get(INCIDENTS))  # Texas


# ---------------------------------------------------------------------------
# Every SOS has a case
# ---------------------------------------------------------------------------

class TestEverySosHasACase:
    def test_an_sos_opens_an_incident_with_the_people_linked(self, world):
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        incident = incident_of(event)

        assert (incident.source, incident.severity, incident.status) == ('sos', 'critical', 'open')
        assert incident.anchor_node.code == 'IN-TG-HYD' and incident.country == 'IN'
        assert incident.trip_id == world['hyd_trip'].id
        kinds = {(l.kind, l.target_id) for l in incident.links.all()}
        assert ('sos', event.id) in kinds and ('trip', world['hyd_trip'].id) in kinds
        assert ('rider', world['hyd_rider'].id) in kinds
        assert ('driver', world['hyd_driver'].id) in kinds
        assert incident.updates.filter(kind='opened').exists()

    def test_one_open_incident_per_trip(self, world):
        trip = world['hyd_trip']
        first = raise_sos(world['hyd_rider'], trip_id=trip.id)
        second = raise_sos(world['hyd_driver'].user_id, trip_id=trip.id, event_type='medical')

        assert first.incident_id == second.incident_id
        assert SafetyIncident.objects.filter(trip=trip).count() == 1
        assert incident_of(second).sos_events.count() == 2

    def test_a_closed_incident_does_not_swallow_a_new_sos(self, world):
        trip = world['hyd_trip']
        first = incident_of(raise_sos(world['hyd_rider'], trip_id=trip.id))
        SOSEvent.objects.filter(incident=first).update(status='resolved')
        services.close_incident(first, actor=None, summary='done', outcome='no_action')

        again = incident_of(raise_sos(world['hyd_rider'], trip_id=trip.id, event_type='accident'))

        assert again.id != first.id and again.status == 'open'

    def test_the_database_refuses_a_second_open_incident_for_a_trip(self, world):
        from django.db import IntegrityError, transaction

        trip = world['hyd_trip']
        SafetyIncident.objects.create(trip=trip, title='a')
        with pytest.raises(IntegrityError), transaction.atomic():
            SafetyIncident.objects.create(trip=trip, title='b')

    def test_an_sos_without_a_trip_opens_its_own(self, world):
        a = raise_sos(world['hyd_driver'].user_id)
        b = raise_sos(world['aus_driver'].user_id)

        assert incident_of(a).id != incident_of(b).id
        assert incident_of(a).anchor_source == 'sos'
        assert incident_of(a).anchor_node.code == 'IN-TG-HYD'

    def test_a_blocked_rider_can_still_raise_an_sos_which_opens_an_incident(self, world):
        rider = world['hyd_rider']
        RiderBlock.objects.create(rider=rider.rider, reason_code='safety')

        event = raise_sos(rider, trip_id=world['hyd_trip'].id)

        assert incident_of(event).status == 'open'

    def test_a_failed_attach_never_fails_the_sos_and_the_sweep_backfills(self, world, monkeypatch):
        def boom(event):
            raise RuntimeError('db hiccup')

        monkeypatch.setattr(services, 'attach_sos', boom)
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        event.refresh_from_db()
        assert event.incident_id is None
        monkeypatch.undo()

        escalation.sweep()

        event.refresh_from_db()
        assert event.incident_id is not None

    def test_acknowledging_the_sos_moves_the_case_and_lands_on_the_timeline(self, world):
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        client = world['hyd_safety'][0]

        assert post(client, f'{SOS_ADMIN}{event.id}/acknowledge/', {'note': 'calling'}).status_code == 200

        incident = incident_of(event)
        assert incident.status == 'investigating'
        assert incident.updates.filter(kind='sos', data__sos_status='acknowledged').exists()
        row = client.get(f'{SOS_ADMIN}{event.id}/').json()['data']
        assert row['incident_id'] == incident.id


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

class TestFanOut:
    def test_the_push_goes_only_to_in_scope_responders(self, geo, world, pushes):
        hyd = with_token(world['hyd_safety'][1])
        aus = with_token(world['aus_safety'][1])
        sup = with_token(world['super'][1])
        city_ops = with_token(operator('city_ops_manager', geo)[1])
        legacy_admin = with_token(User.objects.create_user(
            phone_number='+919800055555', username='+919800055555', role='admin', is_staff=True))
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)

        dispatch_sos(event.id)

        who = {p['user'] for p in pushes}
        assert who == {hyd.id, sup.id}            # Hyderabad responder + Global holder
        assert aus.id not in who and city_ops.id not in who and legacy_admin.id not in who

    def test_an_austin_event_pages_austin_not_hyderabad(self, world, pushes):
        hyd = with_token(world['hyd_safety'][1])
        aus = with_token(world['aus_safety'][1])
        event = raise_sos(world['aus_rider'], trip_id=world['aus_trip'].id)

        dispatch_sos(event.id)

        who = {p['user'] for p in pushes}
        assert aus.id in who and hyd.id not in who

    def test_an_unanchored_event_pages_every_responder(self, world, pushes):
        hyd = with_token(world['hyd_safety'][1])
        aus = with_token(world['aus_safety'][1])
        event = raise_sos(_rider('+919876700095', 'Unplaced'))

        dispatch_sos(event.id)

        assert {hyd.id, aus.id} <= {p['user'] for p in pushes}

    def test_the_emergency_contact_sms_is_unchanged(self, world, monkeypatch, pushes):
        sent = []
        monkeypatch.setattr('base.utils.send_otp_via_sns',
                            type('T', (), {'delay': staticmethod(lambda *a, **k: sent.append(a))}))
        rider = world['hyd_rider']
        rider.emergency_contact = '+919876700077'
        rider.save(update_fields=['emergency_contact'])
        event = raise_sos(rider, trip_id=world['hyd_trip'].id)

        dispatch_sos(event.id)

        assert sent and sent[0][0] == '+919876700077'

    def test_responders_for_names_people_not_roles(self, geo, world):
        _, hyd = world['hyd_safety']
        node = geo['IN-TG-HYD']
        assert hyd in responders_for(node)
        assert world['aus_safety'][1] not in responders_for(node)
        assert {u.id for u in responders_for(None)} >= {hyd.id, world['aus_safety'][1].id}


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def age(event, minutes):
    SOSEvent.objects.filter(pk=event.pk).update(
        created_at=timezone.now() - timedelta(minutes=minutes))


class TestEscalation:
    def test_an_unacknowledged_sos_escalates_once(self, world, pushes,
                                                  django_capture_on_commit_callbacks):
        seed_config('IN')
        seed_config('US')
        with_token(world['hyd_safety'][1])
        with_token(world['super'][1])
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        age(event, 3)

        with django_capture_on_commit_callbacks(execute=True):
            first = escalation.sweep()
        with django_capture_on_commit_callbacks(execute=True):
            second = escalation.sweep()

        assert (first['sos_escalated'], second['sos_escalated']) == (1, 0)
        alerts = OperatorAlert.objects.filter(kind=SAFETY_SOS_ESCALATED)
        assert alerts.count() == 1 and alerts.get().city.code == 'IN-TG-HYD'
        assert {p['user'] for p in pushes if p['data'].get('type') == 'sos_escalation'} == {
            world['hyd_safety'][1].id, world['super'][1].id}
        assert len([p for p in pushes if p['data'].get('type') == 'sos_escalation']) == 2
        incident = incident_of(event)
        assert incident.updates.filter(kind='escalated').count() == 1
        assert AdminAuditLog.objects.filter(action='safety_sos_escalated').count() == 1

    def test_it_stays_quiet_inside_the_window_and_once_acknowledged(self, world):
        seed_config('IN')
        young = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        age(young, 1)
        acked = raise_sos(world['hyd_driver'].user_id)
        age(acked, 10)
        post(world['hyd_safety'][0], f'{SOS_ADMIN}{acked.id}/acknowledge/')

        assert escalation.sweep()['sos_escalated'] == 0
        assert not OperatorAlert.objects.filter(kind=SAFETY_SOS_ESCALATED).exists()

    def test_the_alert_stays_raised_after_it_is_acknowledged_and_does_not_refire(self, world):
        seed_config('IN')
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        age(event, 5)
        escalation.sweep()
        alert = OperatorAlert.objects.get(kind=SAFETY_SOS_ESCALATED)
        post(world['hyd_safety'][0], f'/api/v1/command-centre/admin/alerts/{alert.id}/acknowledge/')

        escalation.sweep()

        assert OperatorAlert.objects.filter(kind=SAFETY_SOS_ESCALATED).count() == 1

    def test_no_config_means_no_timer_and_no_foreign_default(self, world, pushes):
        """Only India is configured. An old Austin SOS must not borrow its 2 minutes."""
        seed_config('IN')
        us = raise_sos(world['aus_rider'], trip_id=world['aus_trip'].id)
        age(us, 120)

        summary = escalation.sweep()

        assert summary['sos_escalated'] == 0
        assert not OperatorAlert.objects.filter(kind=SAFETY_SOS_ESCALATED).exists()

    def test_no_config_at_all_means_no_timer(self, world):
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        age(event, 600)

        assert escalation.sweep()['sos_escalated'] == 0

    def test_a_blank_timer_is_off_even_when_the_country_is_configured(self, world):
        seed_config('IN', sos_ack_minutes=None)
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        age(event, 600)

        assert escalation.sweep()['sos_escalated'] == 0

    def test_a_critical_incident_unassigned_in_time_escalates_once(self, world, pushes,
                                                                   django_capture_on_commit_callbacks):
        seed_config('IN')
        with_token(world['hyd_safety'][1])
        incident = incident_of(raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id))
        SafetyIncident.objects.filter(pk=incident.pk).update(
            opened_at=timezone.now() - timedelta(minutes=6))

        with django_capture_on_commit_callbacks(execute=True):
            a = escalation.sweep()
        with django_capture_on_commit_callbacks(execute=True):
            b = escalation.sweep()

        assert (a['incidents_escalated'], b['incidents_escalated']) == (1, 0)
        assert OperatorAlert.objects.filter(kind=SAFETY_INCIDENT_UNASSIGNED).count() == 1
        assert sorted(p['user'] for p in pushes if p['data'].get('type') == 'incident_escalation') == sorted([
            world['hyd_safety'][1].id, world['super'][1].id])

    def test_an_assigned_incident_does_not_escalate(self, world):
        seed_config('IN')
        client, me = world['hyd_safety']
        incident = incident_of(raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id))
        post(client, f'{INCIDENTS}{incident.id}/assign/', {'assigned_to': 'me'})
        SafetyIncident.objects.filter(pk=incident.pk).update(
            opened_at=timezone.now() - timedelta(minutes=60))

        assert escalation.sweep()['incidents_escalated'] == 0

    def test_the_city_ops_manager_counts_the_escalation_and_may_only_look_at_the_queue(self, geo, world):
        seed_config('IN')
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        age(event, 5)
        escalation.sweep()
        city_ops = operator('city_ops_manager', geo)[0]
        marketing = operator('marketing', geo)[0]

        alerts = city_ops.get('/api/v1/command-centre/admin/alerts/').json()['data']
        assert any(a['kind'] == SAFETY_SOS_ESCALATED for a in alerts['results'])
        assert city_ops.get(INCIDENTS).status_code == 200      # read-only (safety.read)
        assert post(city_ops, INCIDENTS, {'category': 'other', 'severity': 'low',
                                          'title': 'x'}).status_code == 403
        theirs = marketing.get('/api/v1/command-centre/admin/alerts/').json()['data']
        assert not any(a['kind'] == SAFETY_SOS_ESCALATED for a in theirs['results'])


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def parked_trip(world, geo, *, minutes_stopped, started_minutes_ago=30, estimate=20,
                alive=True, lat='17.45', lng='78.40'):
    """An in-progress Hyderabad trip whose driver has not moved for a while."""
    now = timezone.now()
    trip = world['hyd_trip']
    Trip.objects.filter(pk=trip.pk).update(
        started_at=now - timedelta(minutes=started_minutes_ago),
        estimated_duration_min=Decimal(str(estimate)),
        pickup_lat=Decimal('17.30'), pickup_long=Decimal('78.30'),
        destination_lat=Decimal('17.49'), destination_long=Decimal('78.49'),
        last_driver_activity_at=now - timedelta(seconds=20) if alive else now - timedelta(hours=1),
    )
    TripLocationPoint.objects.create(
        trip=trip, driver=world['hyd_driver'], latitude=Decimal(lat), longitude=Decimal(lng),
        recorded_at=now - timedelta(minutes=minutes_stopped), sequence=1)
    trip.refresh_from_db()
    return trip


class TestSignals:
    def test_a_long_stop_is_detected_and_waits_in_the_queue(self, geo, world):
        seed_config('IN')
        parked_trip(world, geo, minutes_stopped=10)

        summary = detection.evaluate()

        signal = SafetySignal.objects.get(kind='long_stop')
        assert summary['created'] == 1 and signal.threshold_version == 1
        assert signal.measured['stopped_minutes'] >= 10 and signal.measured['radius_m'] == 50
        assert signal.incident_id is None                   # below the incident line (20 min)
        listed = world['hyd_safety'][0].get(SIGNALS).json()['data']
        assert [s['id'] for s in listed['results']] == [signal.id]

    def test_a_stop_above_the_incident_line_opens_an_incident_and_an_alert(self, geo, world):
        seed_config('IN')
        parked_trip(world, geo, minutes_stopped=25)

        detection.evaluate()
        detection.evaluate()                                 # idempotent

        signal = SafetySignal.objects.get(kind='long_stop')
        assert signal.incident_id and signal.incident.source == 'signal'
        assert signal.incident.category == 'long_stop' and signal.incident.trip_id == world['hyd_trip'].id
        assert OperatorAlert.objects.filter(kind=SAFETY_SIGNAL).count() == 1
        assert SafetyIncident.objects.filter(trip=world['hyd_trip']).count() == 1
        assert SafetySignal.objects.count() == 1

    def test_a_signal_joins_the_trips_open_incident(self, geo, world):
        seed_config('IN')
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        parked_trip(world, geo, minutes_stopped=25)

        detection.evaluate()

        assert SafetyIncident.objects.filter(trip=world['hyd_trip']).count() == 1
        assert SafetySignal.objects.get().incident_id == event.incident_id

    def test_a_short_stop_a_moving_driver_and_a_silent_driver_raise_nothing(self, geo, world):
        seed_config('IN')
        parked_trip(world, geo, minutes_stopped=3)           # not long enough
        assert detection.evaluate()['created'] == 0
        TripLocationPoint.objects.all().delete()
        parked_trip(world, geo, minutes_stopped=30, alive=False)   # not alive: lost contact, not a stop
        assert detection.evaluate()['created'] == 0

    def test_a_stop_at_the_pickup_or_drop_is_not_a_long_stop(self, geo, world):
        seed_config('IN')
        parked_trip(world, geo, minutes_stopped=30, lat='17.4901', lng='78.4901')   # at the drop

        assert detection.evaluate()['created'] == 0

    def test_a_driver_who_kept_moving_inside_the_window_is_not_stopped(self, geo, world):
        seed_config('IN')
        trip = parked_trip(world, geo, minutes_stopped=1)
        now = timezone.now()
        TripLocationPoint.objects.create(
            trip=trip, driver=world['hyd_driver'], latitude=Decimal('17.40'),
            longitude=Decimal('78.35'), recorded_at=now - timedelta(minutes=5), sequence=0)

        assert detection.evaluate()['created'] == 0

    def test_eta_overrun_is_detected(self, geo, world):
        seed_config('IN')
        trip = world['hyd_trip']
        Trip.objects.filter(pk=trip.pk).update(
            started_at=timezone.now() - timedelta(minutes=45), estimated_duration_min=Decimal('20'))

        detection.evaluate()

        signal = SafetySignal.objects.get(kind='eta_overrun')
        assert signal.measured['over_minutes'] >= 24 and signal.measured['over_percent'] >= 120
        assert signal.incident_id is None                   # 25 min over: under the 30 min incident line

    def test_a_big_eta_overrun_opens_an_incident(self, geo, world):
        seed_config('IN')
        Trip.objects.filter(pk=world['hyd_trip'].pk).update(
            started_at=timezone.now() - timedelta(minutes=90), estimated_duration_min=Decimal('20'))

        detection.evaluate()

        signal = SafetySignal.objects.get(kind='eta_overrun')
        assert signal.incident_id and signal.incident.category == 'eta_overrun'

    def test_a_trip_inside_its_estimate_is_fine(self, geo, world):
        seed_config('IN')
        Trip.objects.filter(pk=world['hyd_trip'].pk).update(
            started_at=timezone.now() - timedelta(minutes=25), estimated_duration_min=Decimal('20'))

        assert detection.evaluate()['created'] == 0

    def test_a_trip_with_no_stored_route_raises_no_deviation(self, geo, world):
        """The deviation rule is real now (tests/test_safety_followups.py); a trip
        with no stored polyline is skipped, never flagged."""
        seed_config('IN', route_deviation_meters=300, route_deviation_minutes=3)
        assert world['hyd_trip'].planned_route_polyline is None
        detection.evaluate()
        assert not SafetySignal.objects.filter(kind='route_deviation').exists()

    def test_no_config_no_detection_and_no_foreign_defaults(self, geo, world):
        seed_config('US')                                    # Hyderabad has none
        parked_trip(world, geo, minutes_stopped=30)

        summary = detection.evaluate()

        assert summary['created'] == 0 and summary['unconfigured'] >= 1
        assert not SafetySignal.objects.exists()

    def test_dismissing_needs_a_reason_and_is_idempotent_and_audited(self, geo, world):
        seed_config('IN')
        parked_trip(world, geo, minutes_stopped=10)
        detection.evaluate()
        signal = SafetySignal.objects.get()
        client = world['hyd_safety'][0]

        assert post(client, f'{SIGNALS}{signal.id}/dismiss/').status_code == 400
        before = audit_count('safety_signal_dismissed')
        first = post(client, f'{SIGNALS}{signal.id}/dismiss/', {'reason': 'parked at a petrol pump'})
        second = post(client, f'{SIGNALS}{signal.id}/dismiss/', {'reason': 'parked at a petrol pump'})

        assert first.json()['data']['changed'] and not second.json()['data']['changed']
        assert audit_count('safety_signal_dismissed') == before + 1
        signal.refresh_from_db()
        assert signal.dismissed_by and signal.dismiss_reason
        assert client.get(SIGNALS).json()['data']['count'] == 0
        assert client.get(f'{SIGNALS}?state=dismissed').json()['data']['count'] == 1
        detection.evaluate()                                  # a dismissed signal is not re-raised
        signal.refresh_from_db()
        assert signal.dismissed_at is not None

    def test_a_signal_is_scoped_like_its_trip(self, geo, world):
        seed_config('IN')
        parked_trip(world, geo, minutes_stopped=10)
        detection.evaluate()
        signal = SafetySignal.objects.get()

        assert signal.id in ids_in(world['hyd_safety'][0].get(SIGNALS))
        assert signal.id not in ids_in(world['aus_safety'][0].get(SIGNALS))
        assert post(world['aus_safety'][0], f'{SIGNALS}{signal.id}/dismiss/',
                    {'reason': 'x'}).status_code == 404

    def test_detection_makes_no_rider_or_driver_facing_change(self, geo, world, pushes):
        seed_config('IN')
        trip = parked_trip(world, geo, minutes_stopped=25)
        before = Trip.objects.filter(pk=trip.pk).values().get()

        detection.evaluate()

        assert Trip.objects.filter(pk=trip.pk).values().get() == before
        assert not [p for p in pushes if p['user'] in (world['hyd_rider'].id,
                                                      world['hyd_driver'].user_id_id)]


# ---------------------------------------------------------------------------
# Working an incident
# ---------------------------------------------------------------------------

@pytest.fixture
def case(world):
    event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
    return incident_of(event), event


class TestWorkingAnIncident:
    def test_the_case_file_shape(self, world, case):
        incident, event = case
        seed_config('IN')
        data = world['hyd_safety'][0].get(f'{INCIDENTS}{incident.id}/').json()['data']

        assert data['id'] == incident.id and data['sos']['active'] == 1
        assert data['sos']['unacknowledged_ids'] == [event.id]
        assert data['sos_events'][0]['user_phone'].endswith('0001') and '9876700001' not in data['sos_events'][0]['user_phone']
        assert data['anchor'] == {'source': 'trip', 'node': 'IN-TG-HYD',
                                  'node_name': incident.anchor_node.name}
        assert data['emergency'] == {'country': 'IN', 'configured': True, 'numbers': IN_NUMBERS}
        assert [p['id'] for p in data['people']['drivers']] == [world['hyd_driver'].id]
        assert data['people']['riders'][0]['in_scope'] is True
        assert data['people']['trips'][0]['primary'] is True
        assert data['actions']['suspend_driver'] and data['actions']['block_rider']
        assert data['actions']['close'] is False and data['actions']['close_blockers']
        assert data['timeline'][0]['kind'] == 'opened'

    def test_people_are_masked_and_the_only_reveal_is_the_logged_one(self, world, case):
        incident, event = case
        body = json.dumps(world['hyd_safety'][0].get(f'{INCIDENTS}{incident.id}/').json())

        assert '9876700001' not in body and '9876700002' not in body
        revealed = post(world['hyd_safety'][0], f'{SOS_ADMIN}{event.id}/reveal/')
        assert revealed.status_code == 200
        SOSEvent.objects.filter(pk=event.pk).update(status='resolved')
        assert post(world['hyd_safety'][0], f'{SOS_ADMIN}{event.id}/reveal/').status_code == 409

    def test_assign_only_to_a_responder_where_it_sits_and_is_idempotent(self, geo, world, case):
        incident, _ = case
        client, me = world['hyd_safety']
        _, aus_user = world['aus_safety']
        _, support = operator('support_agent', geo)

        for who in (aus_user, support):
            refused = post(client, f'{INCIDENTS}{incident.id}/assign/', {'assigned_to': who.id})
            assert refused.status_code == 400 and refused.json()['error']['code'] == 'ASSIGNEE_NOT_ELIGIBLE'
        before = audit_count('safety_incident_assigned')
        first = post(client, f'{INCIDENTS}{incident.id}/assign/', {'assigned_to': me.id})
        again = post(client, f'{INCIDENTS}{incident.id}/assign/', {'assigned_to': 'me'})

        assert first.json()['data']['changed'] and not again.json()['data']['changed']
        assert audit_count('safety_incident_assigned') == before + 1
        listing = client.get(f'{INCIDENTS}{incident.id}/assignees/').json()['data']['results']
        assert [p['id'] for p in listing if p['assigned']] == [me.id]
        assert aus_user.id not in [p['id'] for p in listing]
        assert ids_in(client.get(f'{INCIDENTS}?assigned=me')) == {incident.id}
        assert ids_in(client.get(f'{INCIDENTS}?assigned=none')) == set()

    def test_note_is_idempotent_with_a_key_and_audited(self, world, case):
        incident, _ = case
        client = world['hyd_safety'][0]
        before = audit_count('safety_incident_noted')

        a = post(client, f'{INCIDENTS}{incident.id}/note/', {'body': 'called the driver', 'idempotency_key': 'k1'})
        b = post(client, f'{INCIDENTS}{incident.id}/note/', {'body': 'called the driver', 'idempotency_key': 'k1'})

        assert (a.status_code, b.status_code) == (201, 200)
        assert incident.updates.filter(kind='note').count() == 1
        assert audit_count('safety_incident_noted') == before + 1
        assert post(client, f'{INCIDENTS}{incident.id}/note/', {}).status_code == 400

    def test_free_text_phone_numbers_are_masked(self, world, case):
        incident, _ = case
        client = world['hyd_safety'][0]
        post(client, f'{INCIDENTS}{incident.id}/note/', {'body': 'rang +919812345678 twice'})

        body = json.dumps(client.get(f'{INCIDENTS}{incident.id}/').json())
        assert '9812345678' not in body

    def test_status_changes_are_idempotent_audited_and_never_close(self, world, case):
        incident, _ = case
        client = world['hyd_safety'][0]
        before = audit_count('safety_incident_status_changed')

        a = post(client, f'{INCIDENTS}{incident.id}/status/', {'status': 'investigating'})
        b = post(client, f'{INCIDENTS}{incident.id}/status/', {'status': 'investigating'})

        assert a.json()['data']['changed'] and not b.json()['data']['changed']
        assert audit_count('safety_incident_status_changed') == before + 1
        assert post(client, f'{INCIDENTS}{incident.id}/status/', {'status': 'closed'}).status_code == 400
        assert post(client, f'{INCIDENTS}{incident.id}/status/', {'status': 'nope'}).status_code == 400

    def test_close_needs_a_summary_and_an_outcome(self, world, case):
        incident, event = case
        client = world['hyd_safety'][0]
        SOSEvent.objects.filter(pk=event.pk).update(status='resolved')
        url = f'{INCIDENTS}{incident.id}/close/'

        assert post(client, url, {'outcome': 'no_action'}).status_code == 400
        assert post(client, url, {'summary': 'ok'}).status_code == 400
        assert post(client, url, {'summary': 'ok', 'outcome': 'jail'}).status_code == 400
        before = audit_count('safety_incident_closed')
        done = post(client, url, {'summary': 'rider safe, driver warned', 'outcome': 'warning'})
        again = post(client, url, {'summary': 'rider safe, driver warned', 'outcome': 'warning'})

        assert done.status_code == 200 and done.json()['data']['changed']
        assert again.status_code == 200 and not again.json()['data']['changed']
        assert audit_count('safety_incident_closed') == before + 1
        incident.refresh_from_db()
        assert incident.status == 'closed' and incident.closed_at and incident.outcome == 'warning'
        assert post(client, f'{INCIDENTS}{incident.id}/note/', {'body': 'late'}).status_code == 409
        assert post(client, url, {'summary': 'different', 'outcome': 'other'}).status_code == 409

    def test_an_incident_with_a_live_sos_cannot_be_closed(self, world, case):
        incident, event = case
        client = world['hyd_safety'][0]

        refused = post(client, f'{INCIDENTS}{incident.id}/close/', {'summary': 's', 'outcome': 'no_action'})

        assert refused.status_code == 409 and refused.json()['error']['code'] == 'SOS_STILL_ACTIVE'
        post(client, f'{SOS_ADMIN}{event.id}/resolve/', {'note': 'safe'})
        assert post(client, f'{INCIDENTS}{incident.id}/close/',
                    {'summary': 's', 'outcome': 'no_action'}).status_code == 200

    def test_link_and_unlink_a_visible_target_only(self, world, case):
        incident, _ = case
        client = world['hyd_safety'][0]
        other = _driver('+919876700055', 'Other Driver', world['hyd_driver'].home_city)
        far = world['aus_driver']

        ok = post(client, f'{INCIDENTS}{incident.id}/link/', {'kind': 'driver', 'id': other.id})
        dup = post(client, f'{INCIDENTS}{incident.id}/link/', {'kind': 'driver', 'id': other.id})
        gone = post(client, f'{INCIDENTS}{incident.id}/link/', {'kind': 'driver', 'id': far.id})

        assert ok.json()['data']['changed'] and not dup.json()['data']['changed']
        assert gone.status_code == 400 and gone.json()['error']['code'] == 'TARGET_NOT_FOUND'
        assert post(client, f'{INCIDENTS}{incident.id}/unlink/',
                    {'kind': 'driver', 'id': other.id}).json()['data']['changed']
        assert post(client, f'{INCIDENTS}{incident.id}/unlink/',
                    {'kind': 'trip', 'id': incident.trip_id}).status_code == 409
        sos_id = incident.sos_events.get().id
        assert post(client, f'{INCIDENTS}{incident.id}/unlink/',
                    {'kind': 'sos', 'id': sos_id}).status_code == 409

    def test_manual_incident_and_joining_the_trips_open_one(self, world):
        client = world['hyd_safety'][0]
        body = {'category': 'dangerous_driving', 'severity': 'medium',
                'title': 'Driver speeding', 'trip_id': world['hyd_trip'].id}

        created = post(client, INCIDENTS, body)
        joined = post(client, INCIDENTS, body)

        assert created.status_code == 201 and joined.status_code == 200
        assert created.json()['data']['id'] == joined.json()['data']['id']
        assert created.json()['data']['source'] == 'manual'
        assert audit_count('safety_incident_opened') == 1
        assert post(client, INCIDENTS, {**body, 'category': 'nope'}).status_code == 400
        assert post(client, INCIDENTS, {**body, 'trip_id': world['aus_trip'].id,
                                        'title': 'x'}).status_code == 400   # out of scope

    def test_the_queue_sorts_sos_first_then_severity_then_age_and_filters(self, geo, world):
        client = world['super'][0]
        low = services.open_incident(source='manual', category='other', severity='low', title='low')[0]
        crit = services.open_incident(source='manual', category='medical', severity='critical',
                                      title='critical, no sos')[0]
        with_sos = incident_of(raise_sos(world['hyd_driver'].user_id))        # SOS: critical, live
        SafetyIncident.objects.filter(pk=low.pk).update(opened_at=timezone.now() - timedelta(hours=5))

        rows = client.get(INCIDENTS).json()['data']
        order = [r['id'] for r in rows['results']]

        assert order[0] == with_sos.id and order.index(crit.id) < order.index(low.id)
        assert rows['counts']['active_sos'] == 1 and rows['counts']['unassigned_critical'] == 2
        assert ids_in(client.get(f'{INCIDENTS}?has_open_sos=true')) == {with_sos.id}
        assert ids_in(client.get(f'{INCIDENTS}?severity=low')) == {low.id}
        assert ids_in(client.get(f'{INCIDENTS}?category=medical&status=open')) == {crit.id}
        assert ids_in(client.get(f'{INCIDENTS}?source=manual')) == {low.id, crit.id}
        assert ids_in(client.get(f'{INCIDENTS}?assigned=none')) == {low.id, crit.id, with_sos.id}
        assert ids_in(client.get(f'{INCIDENTS}?q={low.id}')) == {low.id}
        assert client.get(f'{INCIDENTS}?severity=bogus').status_code == 400
        today = timezone.now().date().isoformat()
        assert ids_in(client.get(f'{INCIDENTS}?date_from={today}&date_to={today}')) >= {crit.id}
        assert ids_in(client.get(f'{INCIDENTS}?date_to=2000-01-01')) == set()

    def test_every_write_is_audited(self, geo, world, case):
        incident, event = case
        client, me = world['hyd_safety']
        rider_user = world['hyd_rider']
        steps = [
            (f'{INCIDENTS}{incident.id}/assign/', {'assigned_to': 'me'}, 'safety_incident_assigned'),
            (f'{INCIDENTS}{incident.id}/note/', {'body': 'n'}, 'safety_incident_noted'),
            (f'{INCIDENTS}{incident.id}/status/', {'status': 'investigating'}, 'safety_incident_status_changed'),
            (f'{INCIDENTS}{incident.id}/link/', {'kind': 'driver', 'id': _driver(
                '+919876700044', 'Extra', world['hyd_driver'].home_city).id}, 'safety_incident_linked'),
            (f'{INCIDENTS}{incident.id}/refer-police/', {'reference': 'FIR-1'}, 'safety_incident_police_referral'),
            (f'{SOS_ADMIN}{event.id}/resolve/', {}, None),
            (f'{INCIDENTS}{incident.id}/close/', {'summary': 'done', 'outcome': 'police_referral'},
             'safety_incident_closed'),
        ]
        for path, body, action in steps:
            before = audit_count(action) if action else 0
            assert post(client, path, body).status_code in (200, 201), path
            if action:
                assert audit_count(action) == before + 1, action
        row = AdminAuditLog.objects.filter(action='safety_incident_closed').get()
        assert row.target_type == 'safety_incident' and row.before['status'] == 'action_taken'
        assert row.after['status'] == 'closed' and row.actor_id == me.id

    def test_a_safety_officer_cannot_make_a_non_responder_the_owner_by_id_guessing(self, geo, world, case):
        incident, _ = case
        _, marketing = operator('marketing', geo)
        refused = post(world['hyd_safety'][0], f'{INCIDENTS}{incident.id}/assign/',
                       {'assigned_to': marketing.id})
        assert refused.status_code == 400


class TestAccountActions:
    def test_suspending_a_linked_driver_reuses_the_audited_service(self, world, case):
        incident, _ = case
        client = world['hyd_safety'][0]
        driver = world['hyd_driver']
        Driver.objects.filter(pk=driver.pk).update(approved=True, status='off')
        body = {'driver_id': driver.id, 'reason_code': 'safety', 'note': 'Reported by the rider'}

        first = post(client, f'{INCIDENTS}{incident.id}/suspend-driver/', body)
        again = post(client, f'{INCIDENTS}{incident.id}/suspend-driver/', body)

        assert first.status_code == 201 and again.status_code == 200
        driver.refresh_from_db()
        assert driver.approved is False and driver.status == 'blocked'
        assert AdminAuditLog.objects.filter(action='driver_suspended').count() == 1
        assert AdminAuditLog.objects.filter(action='safety_incident_driver_suspended').count() == 1
        entry = incident.updates.get(kind='action')
        assert entry.data['action'] == 'driver_suspended' and entry.data['driver_id'] == driver.id
        incident.refresh_from_db()
        assert incident.status == 'action_taken'

    def test_blocking_a_linked_rider_reuses_the_audited_service(self, world, case):
        incident, _ = case
        client = world['hyd_safety'][0]
        rider = world['hyd_rider']

        done = post(client, f'{INCIDENTS}{incident.id}/block-rider/',
                    {'rider_id': rider.id, 'reason_code': 'safety', 'note': 'Threatened the driver'})

        assert done.status_code == 201
        assert RiderBlock.objects.filter(rider__user_id=rider, lifted_at__isnull=True).count() == 1
        assert AdminAuditLog.objects.filter(action='rider_blocked').count() == 1
        assert AdminAuditLog.objects.filter(action='safety_incident_rider_blocked').count() == 1
        assert incident.updates.filter(kind='action', data__action='rider_blocked').exists()
        # The block stops booking and nothing else: the next SOS still works and has a case.
        again = raise_sos(rider, trip_id=world['hyd_trip'].id, event_type='accident')
        assert incident_of(again).id == incident.id

    def test_the_reason_code_is_required_and_the_target_must_be_linked(self, geo, world, case):
        incident, _ = case
        client = world['hyd_safety'][0]
        stranger = _driver('+919876700033', 'Stranger', world['hyd_driver'].home_city)

        assert post(client, f'{INCIDENTS}{incident.id}/suspend-driver/',
                    {'driver_id': world['hyd_driver'].id}).status_code == 400
        assert post(client, f'{INCIDENTS}{incident.id}/suspend-driver/',
                    {'driver_id': stranger.id, 'reason_code': 'safety'}).status_code == 409
        assert post(client, f'{INCIDENTS}{incident.id}/block-rider/',
                    {'rider_id': world['aus_rider'].id, 'reason_code': 'safety'}).status_code == 409

    def test_an_action_needs_the_account_capability_at_the_drivers_city(self, geo, world, case):
        """A role with safety.respond but not driver.write is refused."""
        incident, _ = case
        role = AdminRole.objects.create(code='responder_only', name='Responder only',
                                        permissions=['safety.respond', 'trip.read'])
        user = User.objects.create_user(phone_number='+919800066666', username='+919800066666',
                                        role='admin', is_staff=True, email='ro@saaradhigo.test')
        AdminAssignment.objects.create(admin=user, role=role, scope=geo['IN-TG-HYD'])
        client = _client_for(user)

        refused = post(client, f'{INCIDENTS}{incident.id}/suspend-driver/',
                       {'driver_id': world['hyd_driver'].id, 'reason_code': 'safety'})

        assert refused.status_code in (403, 404)
        world['hyd_driver'].refresh_from_db()
        assert world['hyd_driver'].status != 'blocked'

    def test_no_money_moves(self, world, case):
        from servers.payments.models import Payment, TransactionHistory

        incident, _ = case
        client = world['hyd_safety'][0]
        before = (Payment.objects.count(), TransactionHistory.objects.count())
        post(client, f'{INCIDENTS}{incident.id}/suspend-driver/',
             {'driver_id': world['hyd_driver'].id, 'reason_code': 'safety'})
        post(client, f'{INCIDENTS}{incident.id}/block-rider/',
             {'rider_id': world['hyd_rider'].id, 'reason_code': 'safety'})
        assert (Payment.objects.count(), TransactionHistory.objects.count()) == before


class TestPoliceReferral:
    def test_it_records_the_referral_with_no_network_and_shows_the_countrys_numbers(
        self, world, case, monkeypatch,
    ):
        import socket

        def no_network(*a, **k):
            raise AssertionError('the null provider must not touch the network')

        monkeypatch.setattr(socket.socket, 'connect', no_network)
        incident, _ = case
        seed_config('IN')
        client = world['hyd_safety'][0]

        first = post(client, f'{INCIDENTS}{incident.id}/refer-police/', {'reference': 'FIR-42', 'note': 'called 112'})
        second = post(client, f'{INCIDENTS}{incident.id}/refer-police/', {})

        assert first.json()['data']['changed'] and not second.json()['data']['changed']
        assert first.json()['data']['emergency']['numbers'] == IN_NUMBERS
        incident.refresh_from_db()
        assert incident.police_referred_at and incident.police_reference == 'FIR-42'
        assert incident.updates.filter(kind='police_referral', data__provider='null').count() == 1
        assert AdminAuditLog.objects.filter(action='safety_incident_police_referral').count() == 1

    def test_a_country_with_no_config_says_not_configured(self, world):
        seed_config('IN')
        incident = incident_of(raise_sos(world['aus_rider'], trip_id=world['aus_trip'].id))
        data = world['aus_safety'][0].get(f'{INCIDENTS}{incident.id}/').json()['data']

        assert data['emergency'] == {'country': 'US', 'configured': False, 'numbers': []}
        assert '"112"' not in json.dumps(data)

    def test_the_numbers_come_from_the_registry_not_from_safety_code(self):
        from base.countries import COUNTRIES

        assert dict(COUNTRIES['IN'].emergency_numbers) == {'unified': '112'}
        assert dict(COUNTRIES['US'].emergency_numbers) == {'unified': '911'}
        import pathlib

        source = ''.join(p.read_text(encoding='utf-8')
                         for p in pathlib.Path('servers/safety').rglob('*.py'))
        assert "'112'" not in source and "'911'" not in source and '"112"' not in source


# ---------------------------------------------------------------------------
# Ticket escalation
# ---------------------------------------------------------------------------

class TestTicketEscalation:
    def ticket(self, user, trip=None, issue='safety'):
        return SupportTicket.objects.create(user_id=user, trip_id=trip, issue_type=issue,
                                            description='I felt unsafe')

    def test_a_support_agent_escalates_and_a_safety_officer_works_it(self, geo, world):
        ticket = self.ticket(world['hyd_rider'], world['hyd_trip'])
        agent, _ = operator('support_agent', geo)

        made = post(agent, f'/api/v1/safety/admin/tickets/{ticket.id}/escalate/', {'note': 'rider scared'})
        again = post(agent, f'/api/v1/safety/admin/tickets/{ticket.id}/escalate/')

        assert made.status_code == 201 and again.status_code == 200
        data = made.json()['data']
        assert data['created'] and data['can_open'] is False         # an agent does not work it
        assert not again.json()['data']['created']
        incident = SafetyIncident.objects.get(pk=data['incident_id'])
        assert incident.source == 'support_ticket' and incident.trip_id == world['hyd_trip'].id
        assert incident.links.filter(kind='ticket', target_id=ticket.id).exists()
        assert incident.id in ids_in(world['hyd_safety'][0].get(INCIDENTS))
        assert audit_count('support_ticket_escalated_to_safety') == 1
        assert audit_count('safety_incident_opened') == 1
        assert agent.get(f'{INCIDENTS}{incident.id}/').status_code == 403

    def test_only_a_safety_concern_can_be_escalated(self, geo, world):
        ticket = self.ticket(world['hyd_rider'], world['hyd_trip'], issue='payment')
        agent, _ = operator('support_agent', geo)

        assert post(agent, f'/api/v1/safety/admin/tickets/{ticket.id}/escalate/').status_code == 409

    def test_out_of_scope_is_not_found_and_without_support_reply_is_forbidden(self, geo, world):
        far = self.ticket(world['aus_rider'], world['aus_trip'])
        near = self.ticket(world['hyd_rider'], world['hyd_trip'])
        agent, _ = operator('support_agent', geo)
        analyst, _ = operator('analyst', geo)

        assert post(agent, f'/api/v1/safety/admin/tickets/{far.id}/escalate/').status_code == 404
        assert post(analyst, f'/api/v1/safety/admin/tickets/{near.id}/escalate/').status_code == 403
        # a safety officer does not hold support.reply: they work incidents, they do not escalate
        assert post(world['hyd_safety'][0],
                    f'/api/v1/safety/admin/tickets/{near.id}/escalate/').status_code == 403


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_read_by_a_responder_edit_by_super_admin_with_history(self, geo, world):
        seed_config('IN')
        officer, sup = world['hyd_safety'][0], world['super'][0]

        read = officer.get(CONFIG).json()['data']
        assert read['can_edit'] is False
        assert [r['country'] for r in read['results']] == ['IN']      # only their country
        assert read['results'][0]['active']['sos_ack_minutes'] == 2
        assert post(officer, f'{CONFIG}IN/', {'sos_ack_minutes': 1, 'reason': 'x'}).status_code == 403

        before = audit_count('safety_config_changed')
        done = post(sup, f'{CONFIG}IN/', {'sos_ack_minutes': 1, 'reason': 'tighten the timer'})
        same = post(sup, f'{CONFIG}IN/', {'sos_ack_minutes': 1, 'reason': 'again'})

        assert done.status_code == 200 and done.json()['data']['changed'] is True
        assert same.json()['data']['changed'] is False
        assert audit_count('safety_config_changed') == before + 1
        active = SafetyCountryConfig.objects.get(country='IN', is_active=True)
        assert active.version == 2 and active.sos_ack_minutes == 1
        assert active.long_stop_minutes == 8                           # untouched
        assert active.created_by_email.startswith('op')
        old = SafetyCountryConfig.objects.get(country='IN', version=1)
        assert old.is_active is False and old.valid_to is not None
        history = sup.get(CONFIG).json()['data']['results']
        us = [r for r in history if r['country'] == 'US'][0]
        assert us['configured'] is False and us['active'] is None
        assert [h['version'] for h in [r for r in history if r['country'] == 'IN'][0]['history']] == [2, 1]
        row = AdminAuditLog.objects.filter(action='safety_config_changed').latest('id')
        assert row.before['sos_ack_minutes'] == 2 and row.after['sos_ack_minutes'] == 1

    def test_validation(self, geo, world):
        sup = world['super'][0]
        seed_config('IN')
        assert post(sup, f'{CONFIG}IN/', {'sos_ack_minutes': 2}).status_code == 400          # reason
        assert post(sup, f'{CONFIG}IN/', {'sos_ack_minutes': 0, 'reason': 'r'}).status_code == 400
        assert post(sup, f'{CONFIG}IN/', {'sos_ack_minutes': 'abc', 'reason': 'r'}).status_code == 400
        assert post(sup, f'{CONFIG}IN/', {'emergency_numbers': [{'label': 'x', 'number': 'call me'}],
                                          'reason': 'r'}).status_code == 400
        assert post(sup, f'{CONFIG}IN/', {'long_stop_minutes': 30, 'long_stop_incident_minutes': 10,
                                          'reason': 'r'}).status_code == 400
        assert post(sup, f'{CONFIG}FR/', {'reason': 'r'}).status_code == 404

    def test_a_timer_can_be_switched_off_with_null_and_a_new_country_started(self, geo, world):
        sup = world['super'][0]
        seed_config('IN')
        post(sup, f'{CONFIG}IN/', {'sos_ack_minutes': None, 'reason': 'off'})
        assert SafetyCountryConfig.objects.get(country='IN', is_active=True).sos_ack_minutes is None
        created = post(sup, f'{CONFIG}US/', {'sos_ack_minutes': 3, 'reason': 'start Austin'})
        assert created.status_code == 200
        assert SafetyCountryConfig.objects.get(country='US', is_active=True).version == 1

    def test_one_active_row_per_country_is_enforced(self):
        from django.db import IntegrityError, transaction

        seed_config('IN')
        with pytest.raises(IntegrityError), transaction.atomic():
            SafetyCountryConfig.objects.create(country='IN', version=2, is_active=True)


class TestSeedCommand:
    def actor(self, geo):
        client, user = operator('super_admin', geo)
        return user

    def test_seeds_registry_countries_only_when_absent_and_never_overwrites(self, geo):
        actor = self.actor(geo)
        out = StringIO()

        call_command('seed_safety_config', actor_email=actor.email, stdout=out)

        rows = {c.country: c for c in SafetyCountryConfig.objects.all()}
        assert set(rows) == {'IN', 'US'}
        assert rows['IN'].emergency_numbers == [{'label': 'unified', 'number': '112'}]
        assert rows['US'].emergency_numbers == [{'label': 'unified', 'number': '911'}]
        for row in rows.values():
            assert (row.sos_ack_minutes, row.incident_assign_minutes) == (2, 5)
            assert (row.long_stop_minutes, row.long_stop_radius_m) == (8, 50)
            assert (row.eta_overrun_percent, row.eta_overrun_minutes) == (50, 15)
            assert (row.route_deviation_meters, row.route_deviation_minutes) == (300, 3)
            assert row.min_safety_officers == 1 and row.created_by_email == actor.email
        assert AdminAuditLog.objects.filter(action='safety_config_seeded').count() == 1

        SafetyCountryConfig.objects.filter(country='IN').update(sos_ack_minutes=9)
        audit_before = audit_count()
        call_command('seed_safety_config', actor_email=actor.email, stdout=StringIO())

        assert SafetyCountryConfig.objects.get(country='IN').sos_ack_minutes == 9
        assert SafetyCountryConfig.objects.count() == 2
        assert audit_count() == audit_before

    def test_an_edited_country_is_not_reseeded_even_if_its_active_row_is_gone(self, geo):
        actor = self.actor(geo)
        seed_config('IN')
        SafetyCountryConfig.objects.filter(country='IN').update(is_active=False)

        call_command('seed_safety_config', actor_email=actor.email, stdout=StringIO())

        assert SafetyCountryConfig.objects.filter(country='IN').count() == 1

    def test_it_refuses_without_an_actor(self, geo):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command('seed_safety_config', stdout=StringIO())


class TestNothingAssumesACountry:
    def test_no_hardcoded_country_in_the_safety_code(self):
        import pathlib
        import re

        offenders = []
        for path in pathlib.Path('servers/safety').rglob('*.py'):
            if 'migrations' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            for pattern in (r"'IN'", r'"IN"', r'Hyderabad', r'INR', r'DEFAULT_COUNTRY', r'get_country\('):
                for match in re.finditer(pattern, text):
                    offenders.append((str(path), pattern))
        assert not offenders, offenders


# ---------------------------------------------------------------------------
# PostgreSQL: the signal sweep's query shape uses its indexes
# ---------------------------------------------------------------------------

@pytest.mark.postgres
class TestQueryPlans:
    def test_the_candidate_trips_use_the_status_index_and_the_trail_index(self, geo, world):
        from django.db import connection
        from django.db.models import OuterRef, Subquery

        from servers.ride.models import TripStatus

        status_id = TripStatus.objects.get(status_code='in_progress').id
        last = TripLocationPoint.objects.filter(trip=OuterRef('pk')).order_by('-recorded_at')
        qs = (Trip.objects.filter(status_id=status_id, started_at__isnull=False,
                                  requested_at__gte=timezone.now() - timedelta(hours=24))
              .annotate(last_at=Subquery(last.values('recorded_at')[:1])).order_by('started_at')[:2000])
        sql, params = qs.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute('SET enable_seqscan = off')
            cursor.execute('EXPLAIN ' + sql, params)
            plan = chr(10).join(row[0] for row in cursor.fetchall())
        # Which status index the planner picks depends on table size (the FK
        # index on an empty table, the composite one on a full one); what
        # matters is that the trips are read through an index, never a scan.
        assert 'Seq Scan' not in plan, plan
        assert 'ride_trip_status' in plan or 'trip_status_recent_idx' in plan, plan
        assert 'triploc_trip_time_idx' in plan, plan
