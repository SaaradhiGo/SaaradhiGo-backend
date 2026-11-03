"""Console Tier 0 (owner, 2026-11-03): the functional fixes behind the console.

  * one human-readable ID per record (`base.refs`);
  * locality-level addresses in lists (`base.locality`), real names or "Driver #18";
  * an SOS answered in one step from the list or the case: Acknowledge, Assign
    to me, Resolve, False alarm (reason required);
  * one alert per event, raised in place, closed when the cause ends, and no
    operational alert for a city that is not live;
  * stuck trips by the owner's rule, per country.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from base import refs
from base.locality import locality
from servers.command_centre.models import OperatorAlert
from servers.command_centre.services import clear_alerts, raise_alert
from servers.ride import stuck
from servers.ride.models import Trip
from servers.sos.models import SOSEvent
from test_safety_module import incident_of, post, raise_sos, seed_config, world  # noqa: F401
from test_sos_support_zones_matrix import _status, geo  # noqa: F401

pytestmark = pytest.mark.django_db

INCIDENTS = '/api/v1/safety/admin/incidents/'


# ---------------------------------------------------------------------------
# IDs and localities
# ---------------------------------------------------------------------------

class TestRefs:
    @pytest.mark.parametrize('text, expected', [
        ('T-881230', ('trip', 881230)), ('sos-1042', ('incident', 1042)),
        ('SOS 1042', ('incident', 1042)), ('INC#7', ('incident', 7)),
        ('D-2231', ('driver', 2231)), ('R-55120', ('rider', 55120)),
        ('PO-77', ('payout', 77)), ('TKT-310', ('ticket', 310)), ('APR-5', ('approval', 5)),
        ('Hyderabad', None), ('T-', None), ('', None),
    ])
    def test_parse(self, text, expected):
        assert refs.parse(text) == expected

    def test_format(self):
        assert refs.ref(refs.TRIP, 12) == 'T-12'
        assert refs.ref(refs.DRIVER, None) is None


class TestLocality:
    @pytest.mark.parametrize('address, city, expected', [
        ('Road No. 3, Banjara Hills, Hyderabad, Telangana 500034, India', 'Hyderabad',
         'Banjara Hills'),
        ('HITEC City, Hyderabad', 'Hyderabad', 'HITEC City'),
        ('Inorbit Mall, Madhapur', 'Hyderabad', 'Madhapur'),
        ('12, 4th Cross, Hyderabad', 'Hyderabad', None),
        ('Plot 7, Gachibowli, Hyderabad 500032', 'Hyderabad', 'Gachibowli'),
        ('', 'Hyderabad', None),
        ('500034, India', None, None),
    ])
    def test_locality(self, address, city, expected):
        assert locality(address, city) == expected


# ---------------------------------------------------------------------------
# SOS in one step
# ---------------------------------------------------------------------------

@pytest.fixture
def case(world):
    seed_config('IN')
    event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
    return incident_of(event), event


class TestOneStepSos:
    def test_acknowledge_then_assign_to_me(self, world, case):
        incident, event = case
        client, me = world['hyd_safety']
        listed = client.get(INCIDENTS).json()['data']['results']
        row = next(r for r in listed if r['id'] == incident.id)
        assert row['ref'] == f'SOS-{incident.id}' and row['trip_ref'] == f'T-{world["hyd_trip"].id}'

        r = post(client, f'{INCIDENTS}{incident.id}/acknowledge/')
        assert r.status_code == 200, r.content
        assert r.json()['data']['acknowledged_sos'] == 1
        event.refresh_from_db()
        assert event.status == 'acknowledged'
        assert post(client, f'{INCIDENTS}{incident.id}/assign/',
                    {'assigned_to': 'me'}).status_code == 200
        incident.refresh_from_db()
        assert incident.status == 'investigating' and incident.assigned_to_id == me.id

    def test_false_alarm_needs_a_reason_and_closes_everything(self, world, case):
        incident, event = case
        client, _ = world['hyd_safety']
        missing = post(client, f'{INCIDENTS}{incident.id}/false-alarm/', {})
        assert missing.status_code == 400
        r = post(client, f'{INCIDENTS}{incident.id}/false-alarm/', {'reason': 'Rider confirmed safe'})
        assert r.status_code == 200, r.content
        event.refresh_from_db()
        incident.refresh_from_db()
        assert event.status == 'false_alarm'
        assert incident.status == 'closed' and incident.outcome == 'no_action'
        assert 'Rider confirmed safe' in incident.closing_summary

    def test_resolve_needs_an_outcome(self, world, case):
        incident, event = case
        client, _ = world['hyd_safety']
        assert post(client, f'{INCIDENTS}{incident.id}/resolve/', {'summary': 'ok'}).status_code == 400
        r = post(client, f'{INCIDENTS}{incident.id}/resolve/',
                 {'summary': 'Spoke to both', 'outcome': 'warning'})
        assert r.status_code == 200, r.content
        event.refresh_from_db()
        assert event.status == 'resolved'

    def test_the_sos_false_alarm_endpoint_needs_a_reason(self, world, case):
        _incident, event = case
        client, _ = world['hyd_safety']
        assert post(client, f'/api/v1/sos/admin/{event.id}/false-alarm/', {}).status_code == 400

    def test_alerts_for_the_case_close_with_it(self, world, case):
        incident, event = case
        client, _ = world['hyd_safety']
        alert = raise_alert('safety_sos_escalated', title='x', severity='critical',
                            source_type='safety_incident', source_id=incident.id,
                            city=incident.anchor_node)
        post(client, f'{INCIDENTS}{incident.id}/false-alarm/', {'reason': 'drill'})
        alert.refresh_from_db()
        assert alert.acknowledged_at is not None


# ---------------------------------------------------------------------------
# Alerts: one per event, raised in place, live cities only
# ---------------------------------------------------------------------------

class TestAlerts:
    def test_a_worse_occurrence_raises_the_same_alert(self, geo):
        city = geo['IN-TG-HYD']
        first = raise_alert('threshold_unfulfilled_rate', title='warn', severity='warning',
                            source_type='alert_threshold', source_id='IN-TG-HYD:u', city=city)
        again = raise_alert('threshold_unfulfilled_rate', title='crit', severity='critical',
                            source_type='alert_threshold', source_id='IN-TG-HYD:u', city=city)
        assert again.pk == first.pk
        first.refresh_from_db()
        assert first.severity == 'critical' and first.title == 'crit'
        lower = raise_alert('threshold_unfulfilled_rate', title='warn', severity='warning',
                            source_type='alert_threshold', source_id='IN-TG-HYD:u', city=city)
        lower.refresh_from_db()
        assert lower.severity == 'critical', 'never lowered in place'
        assert OperatorAlert.objects.filter(source_id='IN-TG-HYD:u').count() == 1

    def test_cleared_when_the_cause_ends(self, geo):
        a = raise_alert('safety_coverage_gap', title='x', severity='critical',
                        source_type='safety_coverage', source_id='IN-TG-HYD', city=geo['IN-TG-HYD'])
        assert clear_alerts('safety_coverage', 'IN-TG-HYD', note='fixed') == 1
        a.refresh_from_db()
        assert a.acknowledged_at is not None

    def test_no_operational_alert_for_a_city_that_is_not_live(self, geo):
        paused = geo['US-TX-AUS']
        paused.status = 'paused'
        paused.save()
        assert raise_alert('safety_coverage_gap', title='x', source_type='safety_coverage',
                           source_id='US-TX-AUS', city=paused) is None
        sos = raise_alert('safety_sos_escalated', title='y', severity='critical',
                          source_type='safety_incident', source_id='99', city=paused)
        assert sos is not None, 'an SOS alerts wherever it happens'


# ---------------------------------------------------------------------------
# Stuck trips
# ---------------------------------------------------------------------------

class TestStuck:
    def _config(self):
        return seed_config('IN', stuck_overrun_percent=200, stuck_no_location_minutes=10,
                           stuck_not_started_minutes=30)

    def _trip(self, world, code, **fields):
        trip = world['hyd_trip']
        Trip.objects.filter(pk=trip.pk).update(status_id=_status(code), **fields)
        trip.refresh_from_db()
        return trip

    def test_overrun(self, world):
        config, now = self._config(), timezone.now()
        trip = self._trip(world, 'in_progress', started_at=now - timedelta(minutes=45),
                          estimated_duration_min=Decimal('20'), last_driver_activity_at=now)
        assert stuck.stuck_reason(trip, config, now)[0] == stuck.OVERRUN

    def test_no_location(self, world):
        config, now = self._config(), timezone.now()
        trip = self._trip(world, 'in_progress', started_at=now - timedelta(minutes=15),
                          estimated_duration_min=Decimal('40'),
                          last_driver_activity_at=now - timedelta(minutes=11))
        assert stuck.stuck_reason(trip, config, now) == (stuck.NO_LOCATION, 11)

    def test_not_started(self, world):
        config, now = self._config(), timezone.now()
        trip = self._trip(world, 'accepted', accepted_at=now - timedelta(minutes=31),
                          started_at=None, last_driver_activity_at=now)
        assert stuck.stuck_reason(trip, config, now)[0] == stuck.NOT_STARTED

    def test_healthy_and_off(self, world):
        config, now = self._config(), timezone.now()
        trip = self._trip(world, 'in_progress', started_at=now - timedelta(minutes=10),
                          estimated_duration_min=Decimal('20'), last_driver_activity_at=now)
        assert stuck.stuck_reason(trip, config, now) == (None, None)
        assert stuck.stuck_reason(trip, None, now) == (None, None), 'no config: off'


def test_the_mistake_flag_is_on_the_case(world):
    seed_config('IN')
    event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
    SOSEvent.objects.filter(pk=event.pk).update(raiser_reported_mistake_at=timezone.now())
    incident = incident_of(event)
    client, _ = world['hyd_safety']
    data = client.get(f'{INCIDENTS}{incident.id}/').json()['data']
    assert data['rider_reports_false_alarm'] is True
    assert data['sos_events'][0]['raiser_reported_mistake_at']
