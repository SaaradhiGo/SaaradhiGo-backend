"""Where an SOS is routed (owner's ruling, 2026-10-28).

  trip        -> the trip's zone node
  gps         -> the raiser's fix, resolved through the active zone polygons
  home_city   -> a driver's home city (riders have none)
  unanchored  -> EVERY Safety Officer, whatever their scope, plus Global,
                 and a Command Centre alert

Over real HTTP: the rider or driver app POSTs `/sos/`, then Safety Officers
in Hyderabad and in Austin, and a Super Admin at Global, read the queue.
"""

import json

import pytest
from rest_framework.test import APIClient

from servers.admin_audit.models import AdminAuditLog
from servers.command_centre.models import SOS_UNANCHORED, OperatorAlert
from servers.command_centre.services import raise_alert
from servers.sos.anchoring import resolve_anchor
from servers.sos.models import SOSEvent
from test_sos_support_zones_matrix import (
    _client_for, _driver, _rider, _trip, _zone, geo, ids_in, operator,  # noqa: F401 - fixtures
)

pytestmark = pytest.mark.django_db

SOS = '/api/v1/sos/'
QUEUE = '/api/v1/sos/admin/'
ALERTS = '/api/v1/command-centre/admin/alerts/'

# Inside the Hyderabad test polygon (78.30..78.50 E, 17.40..17.50 N).
IN_HYD = {'lat': '17.45', 'lng': '78.40'}
# Austin, drawn as its own polygon below.
AUSTIN_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-97.85, 30.15], [-97.60, 30.15], [-97.60, 30.40],
                     [-97.85, 30.40], [-97.85, 30.15]]],
}
IN_AUS = {'lat': '30.27', 'lng': '-97.74'}
# A real coordinate inside no drawn zone (mid-Atlantic).
NOWHERE = {'lat': '0.5', 'lng': '-30.0'}


@pytest.fixture
def world(geo):
    hyd_zone = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus_zone = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
                     polygon_geojson=AUSTIN_POLYGON)
    return {
        'hyd_zone': hyd_zone, 'aus_zone': aus_zone,
        'rider': _rider('+919876600001', 'Anchor Rider'),
        'rider2': _rider('+919876600002', 'Second Rider'),
        'aus_driver': _driver('+15125560002', 'Austin Driver', geo['US-TX-AUS']),
        'hyd_driver': _driver('+919876600003', 'Hyderabad Driver', geo['IN-TG-HYD']),
        'hyd_safety': operator('safety_officer', geo)[0],
        'aus_safety': operator('safety_officer', geo, scope_code='US-TX-AUS')[0],
        'super_admin': operator('super_admin', geo)[0],
    }


def _raise(user, **body):
    response = _client_for(user).post(SOS, {'event_type': 'panic', **body}, format='json')
    assert response.status_code == 201, response.content
    return SOSEvent.objects.get(id=response.json()['data']['sos_id'])


def _detail(client, event):
    return client.get(f'{QUEUE}{event.id}/')


# ---------------------------------------------------------------------------
# The three anchored paths
# ---------------------------------------------------------------------------

class TestAnchored:
    def test_a_trip_anchors_to_the_trips_zone(self, geo, world):
        trip = _trip(world['rider'], world['hyd_zone'], world['hyd_driver'])

        event = _raise(world['rider'], trip_id=trip.id, **IN_AUS)   # the fix says Austin; the trip wins

        assert (event.anchor_source, event.anchor_node) == ('trip', geo['IN-TG-HYD'])
        assert event.id in ids_in(world['hyd_safety'].get(QUEUE))
        assert event.id not in ids_in(world['aus_safety'].get(QUEUE))

    def test_a_riders_fix_inside_hyderabad_anchors_to_hyderabad(self, geo, world):
        event = _raise(world['rider'], **IN_HYD)

        assert (event.anchor_source, event.anchor_node) == ('gps', geo['IN-TG-HYD'])
        assert event.id in ids_in(world['hyd_safety'].get(QUEUE))
        assert event.id not in ids_in(world['aus_safety'].get(QUEUE))
        assert _detail(world['aus_safety'], event).status_code == 404
        assert _detail(world['hyd_safety'], event).json()['data']['anchor'] == {
            'source': 'gps', 'node': 'IN-TG-HYD', 'node_name': geo['IN-TG-HYD'].name,
        }
        assert not OperatorAlert.objects.exists()

    def test_a_drivers_fix_wins_over_their_home_city(self, geo, world):
        """A Hyderabad driver in trouble in Austin is Austin's incident."""
        event = _raise(world['hyd_driver'].user_id, **IN_AUS)

        assert (event.anchor_source, event.anchor_node) == ('gps', geo['US-TX-AUS'])
        assert event.id in ids_in(world['aus_safety'].get(QUEUE))
        assert event.id not in ids_in(world['hyd_safety'].get(QUEUE))

    def test_a_driver_with_no_usable_fix_falls_back_to_their_home_city(self, geo, world):
        no_fix = _raise(world['aus_driver'].user_id)
        outside = _raise(world['aus_driver'].user_id, event_type='medical', **NOWHERE)

        for event in (no_fix, outside):
            assert (event.anchor_source, event.anchor_node) == ('home_city', geo['US-TX-AUS'])
            assert event.id in ids_in(world['aus_safety'].get(QUEUE))
            assert event.id not in ids_in(world['hyd_safety'].get(QUEUE))

    def test_a_zone_without_a_node_does_not_swallow_the_fix(self, geo, world):
        """An airport sub-zone drawn before its node existed sits on top of
        the city; the city underneath still anchors the event."""
        _zone('HYD-AIRPORT-ORPHAN', geo['IN-TG-HYD'], priority=100)
        from servers.pricing.models import ServiceZone
        ServiceZone.objects.filter(code='HYD-AIRPORT-ORPHAN').update(geo_node=None)

        event = _raise(world['rider2'], **IN_HYD)

        assert (event.anchor_source, event.anchor_node) == ('gps', geo['IN-TG-HYD'])

    def test_resolve_anchor_is_the_order_the_ruling_gives(self, geo, world):
        event = SOSEvent(user=world['hyd_driver'].user_id, initiated_by='driver',
                         latitude='30.27', longitude='-97.74')
        assert resolve_anchor(event) == (geo['US-TX-AUS'], 'gps')
        event.latitude = event.longitude = None
        assert resolve_anchor(event) == (geo['IN-TG-HYD'], 'home_city')
        event.user = world['rider']
        assert resolve_anchor(event) == (None, 'unanchored')


# ---------------------------------------------------------------------------
# Unanchored: never invisible
# ---------------------------------------------------------------------------

class TestUnanchored:
    @pytest.fixture
    def event(self, world):
        return _raise(world['rider'])            # no trip, no fix, no home city

    def test_it_is_unanchored(self, event):
        assert (event.anchor_source, event.anchor_node) == ('unanchored', None)

    def test_a_fix_outside_every_zone_is_also_unanchored(self, world):
        event = _raise(world['rider2'], **NOWHERE)
        assert event.anchor_source == 'unanchored'

    def test_every_safety_officer_sees_it_whatever_their_city(self, world, event):
        for who in ('hyd_safety', 'aus_safety', 'super_admin'):
            response = world[who].get(QUEUE)
            assert event.id in ids_in(response), who
            assert response.json()['data']['open_count'] >= 1, who
            detail = _detail(world[who], event)
            assert detail.status_code == 200, who
            assert detail.json()['data']['anchor']['source'] == 'unanchored'

    def test_it_stays_in_a_narrowed_view(self, world, event):
        """Narrowing to a city is "show me Austin"; an unanchored SOS is
        nowhere, not elsewhere, so it is not hidden by the switcher."""
        assert event.id in ids_in(world['super_admin'].get(f'{QUEUE}?scope=US-TX-AUS'))
        assert event.id in ids_in(world['hyd_safety'].get(f'{QUEUE}?scope=IN-TG-HYD'))

    def test_a_safety_officer_in_another_city_can_act_on_it(self, world, event):
        acked = world['aus_safety'].post(f'{QUEUE}{event.id}/acknowledge/', {'note': 'on it'}, format='json')
        assert acked.status_code == 200
        event.refresh_from_db()
        assert event.status == 'acknowledged'

        revealed = world['aus_safety'].post(f'{QUEUE}{event.id}/reveal/', {}, format='json')
        assert revealed.status_code == 200
        assert revealed.json()['data']['contact']['phone_number'] == '+919876600001'

    def test_nobody_without_safety_respond_sees_it(self, geo, world, event):
        for role in ('city_ops_manager', 'support_agent', 'analyst'):
            client, _ = operator(role, geo)
            assert client.get(QUEUE).status_code == 403, role

    def test_a_command_centre_alert_is_raised_with_the_event(self, world, event):
        alert = OperatorAlert.objects.get(source_type='sos_event', source_id=str(event.id))

        assert alert.kind == SOS_UNANCHORED and alert.severity == 'critical'
        assert alert.is_open and alert.payload['sos_id'] == event.id
        assert alert.payload['initiated_by'] == 'rider'
        # Nothing about the person on the alert: the list is read widely.
        body = json.dumps(alert.payload) + alert.title
        assert '9876600001' not in body and 'Anchor Rider' not in body

    def test_anchored_events_raise_no_alert(self, world):
        _raise(world['rider2'], **IN_HYD)
        _raise(world['aus_driver'].user_id)
        assert not OperatorAlert.objects.exists()

    def test_the_alert_is_not_raised_twice_while_open(self, world, event):
        again = raise_alert(SOS_UNANCHORED, title='dup', source_type='sos_event', source_id=event.id)
        assert OperatorAlert.objects.filter(source_id=str(event.id)).count() == 1
        assert again.title != 'dup'

    def test_the_dashboard_counts_the_open_alert(self, world, event):
        payload = world['super_admin'].get('/api/v1/ride/admin/dashboard/').json()['data']
        assert payload['alerts']['open'] == 1

    def test_the_alert_list_is_read_by_those_who_may_respond(self, geo, world, event):
        # Owner ruling 2026-10-30: live SOS goes to responders and City Ops.
        ops, _ = operator('city_ops_manager', geo)
        response = ops.get(ALERTS)

        assert response.status_code == 200
        rows = response.json()['data']['results']
        assert [r['payload']['sos_id'] for r in rows] == [event.id]
        assert response.json()['data']['open_count'] == 1

        analyst, _ = operator('analyst', geo)                # report.read, cannot respond
        quiet = analyst.get(ALERTS).json()['data']
        assert quiet['results'] == [] and quiet['open_count'] == 0

        support, _ = operator('support_agent', geo)        # no report.read
        assert support.get(ALERTS).status_code == 403
        assert APIClient().get(ALERTS).status_code in (401, 403)

    def test_acknowledging_needs_the_kinds_own_capability_and_is_audited(self, geo, world, event):
        alert = OperatorAlert.objects.get(source_id=str(event.id))
        analyst, _ = operator('analyst', geo)                # may neither see nor take
        refused = analyst.post(f'{ALERTS}{alert.id}/acknowledge/', {}, format='json')
        assert refused.status_code == 403

        # The Safety Officer who took the incident -- in ANOTHER city -- takes
        # the alert. `safety.respond` is the gate; `report.read` is not needed.
        aus_safety_client, aus_safety = operator('safety_officer', geo, scope_code='US-TX-AUS')
        taken = aus_safety_client.post(f'{ALERTS}{alert.id}/acknowledge/',
                                       {'note': 'Austin took it'}, format='json')
        assert taken.status_code == 200, taken.content
        alert.refresh_from_db()
        assert not alert.is_open and alert.acknowledged_note == 'Austin took it'
        assert alert.acknowledged_by_id == aus_safety.id
        row = AdminAuditLog.objects.filter(action='operator_alert_acknowledged').first()
        assert row is not None and row.target_id == str(alert.id) and row.actor_id == aus_safety.id

        # Idempotent, and the dashboard count drops.
        assert world['super_admin'].post(f'{ALERTS}{alert.id}/acknowledge/', {}, format='json').status_code == 200
        assert AdminAuditLog.objects.filter(action='operator_alert_acknowledged').count() == 1
        assert world['super_admin'].get('/api/v1/ride/admin/dashboard/').json()['data']['alerts']['open'] == 0
        assert world['super_admin'].get(f'{ALERTS}?status=acknowledged').json()['data']['count'] == 1
