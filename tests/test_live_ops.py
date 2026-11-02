"""Module 10: the Live Ops map. Scoped dots and trips, a demand and supply heatmap,
and the single-use ticket that replaced the JWT in the socket URL.

WHAT THESE PROVE
----------------
  * CAPABILITIES: `liveops.read` (Super Admin, City Ops, Safety) shows dots and
    trips; `liveops.aggregate` (Analyst, Marketing) shows cells only; Support, KYC
    and Finance hold neither. REST and WebSocket gates agree.
  * SCOPE: a driver's city is the zone they are IN, else their home city. A
    driver, trip or cell outside the caller's places is never sent, and never
    counted. Out of scope is 404, missing capability is 403. The per-frame path
    is covered too: an Austin driver's frame never reaches a Hyderabad City Ops
    socket.
  * THE SOCKET: `?ticket=` is single-use (GETDEL), hashed at rest, bound to the
    user, 30 s; `?token=` is refused on the admin socket; the scope is re-read
    and a revoked assignment closes the stream with 4003.
  * PII: dots, trips and alerts are built from whitelists; no name, phone, plate
    or rider data, ever; SOS pins only for `sees_live_sos`; finance alerts never.
  * HEATMAP: 0.01 degree cells, demand against supply; cells under 3 events are
    suppressed for aggregate-only callers, who get no coordinate finer than the grid.
  * AUDIT: one row per session open and close; none per frame.

The WebSocket tests are `postgres`-marked like every other consumer suite here
(threads and an in-memory SQLite database do not mix); the CI `postgres` job
runs them. Everything else runs in the default suite.

Fixture passwords are not used: tokens and tickets are minted directly.
"""

import asyncio
import json
import re
import time
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.utils import timezone

from conftest import seed_admin_access
from finance_world import client_for, make_driver, make_rider, operator, status_obj
from servers.admin_access.masking import RAW_PII_PATTERNS
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_access.roles import ROLE_TEMPLATES, validate_templates
from servers.admin_audit.models import AdminAuditLog
from servers.command_centre.models import (
    CRITICAL, SAFETY_SOS_ESCALATED, SETTLEMENT_NO_COMMISSION_RULE, OperatorAlert,
)
from servers.driver.models import VehicleType
from servers.live_ops import config as cfg
from servers.live_ops import heatmap as hm
from servers.live_ops.dots import ALERT_KEYS, DOT_KEYS, FORBIDDEN_KEYS, TRIP_KEYS
from servers.live_ops.geo import Geography
from servers.live_ops.models import SupplySample
from servers.live_ops.positions import encode
from servers.live_ops.scope import LiveScope
from servers.live_ops.snapshot import network_status
from servers.live_ops.stream import FrameFilter
from servers.live_ops.tickets import consume_ticket, issue_ticket
from servers.pricing.models import ServiceZone
from servers.redis_client import (
    ACTIVE_TRIP_PREFIX, HEARTBEAT_PREFIX, VEHICLE_TYPE_PREFIX, redis_client,
)
from servers.ride.models import Trip
from servers.sos.models import SOSEvent

User = get_user_model()

BASE = '/api/v1/live-ops/admin'
SNAPSHOT, HEATMAP, TICKET = f'{BASE}/snapshot/', f'{BASE}/heatmap/', f'{BASE}/ws-ticket/'
WS = '/ws/admin/live-locations/'

# Points inside the two test zones.
HYD = (17.43012, 78.40034)
HYD_B = (17.38501, 78.48601)
AUS = (30.26701, -97.74302)

DRIVER_NAME = 'Suresh Reddy'
DRIVER_PHONE_LAST = '0000042'
PLATE = 'TS09ZZ4242'
RIDER_NAME = 'Ravi Kumar'
ADDRESS = 'Plot 12, Road No 36, Jubilee Hills, Hyderabad'


def box(lon0, lat0, lon1, lat1):
    return {'type': 'Polygon',
            'coordinates': [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}


needs_redis = pytest.mark.skipif(redis_client is None, reason='the live map reads Redis')


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------

def build_world():
    """Hyderabad and Austin, a driver in each, one who is Hyderabad's by home city
    but is IN Austin, one nobody can place, an operator of every role."""
    seed_admin_access()
    geo = {n.code: n for n in GeoNode.objects.all()}
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')

    def zone(code, node, country, state, polygon):
        z, _ = ServiceZone.objects.update_or_create(code=code, defaults={
            'name': node.name, 'zone_type': 'city', 'city': node.name, 'country': country,
            'state_code': state, 'polygon_geojson': polygon, 'is_active': True,
            'priority': 60, 'geo_node': node})
        return z

    hyd_zone = zone('LO-HYD', geo['IN-TG-HYD'], 'IN', 'TG', box(78.30, 17.20, 78.60, 17.60))
    aus_zone = zone('LO-AUS', geo['US-TX-AUS'], 'US', 'TX', box(-97.95, 30.10, -97.55, 30.50))
    Geography.reset()

    drivers = SimpleNamespace(
        hyd=make_driver(geo, 'IN-TG-HYD', 'LO1', currency='INR', calling=91),
        aus=make_driver(geo, 'US-TX-AUS', 'LO2', currency='USD', calling=1),
        visitor=make_driver(geo, 'IN-TG-HYD', 'LO3', currency='INR', calling=91),
        nowhere=make_driver(geo, 'IN-TG-HYD', 'LO4', currency='INR', calling=91, city_node=False),
    )
    for d, name in ((drivers.hyd, DRIVER_NAME), (drivers.aus, 'Alex Austin'),
                    (drivers.visitor, 'Vera Visitor'), (drivers.nowhere, 'Nina Nowhere')):
        d.user_id.full_name = name
        d.user_id.save(update_fields=['full_name'])
    drivers.hyd.active_vehicle.vehicle_number = PLATE
    drivers.hyd.active_vehicle.save(update_fields=['vehicle_number'])

    ops = {code: operator(code, geo, 'IN-TG-HYD') for code in (
        'city_ops_manager', 'safety_officer', 'analyst', 'marketing',
        'support_agent', 'kyc_verifier', 'finance')}
    return SimpleNamespace(
        geo=geo, sedan=sedan, hyd_zone=hyd_zone, aus_zone=aus_zone, drivers=drivers,
        ops=ops, super=operator('super_admin', geo),
        aus_ops=operator('city_ops_manager', geo, 'US-TX-AUS'),
        rider=make_rider('LO', 91))


def put_driver(driver, point, *, trip_id=None, heading=None, alive=True, vclass='sedan'):
    """What the location consumer leaves in Redis for a driver who just pinged."""
    did = driver.id
    if alive:
        redis_client.setex(f'{HEARTBEAT_PREFIX}{did}', 45, '1')
    redis_client.setex(f'{cfg.LIVEPOS_PREFIX}{did}', 120,
                       encode(point[0], point[1], heading, round(time.time(), 1)))
    redis_client.setex(f'{VEHICLE_TYPE_PREFIX}{did}', 3600, vclass)
    if trip_id:
        redis_client.set(f'{ACTIVE_TRIP_PREFIX}{did}', str(trip_id))


def put_fleet(w):
    put_driver(w.drivers.hyd, HYD, heading=90)
    put_driver(w.drivers.aus, AUS)
    put_driver(w.drivers.visitor, AUS)            # Hyderabad's driver, working in Austin
    put_driver(w.drivers.nowhere, (12.0, 77.0))    # outside every zone, no home city


def make_trip(w, zone, *, state='requested', pickup=HYD, drop=(17.40, 78.45), driver=None,
              requested_at=None, stale=False):
    trip = Trip.objects.create(
        user_id=w.rider, driver_id=driver, zone=zone, status_id=status_obj(state),
        requested_vehicle_type=w.sedan, estimated_fare=Decimal('100'),
        pickup_lat=Decimal(str(pickup[0])), pickup_long=Decimal(str(pickup[1])),
        destination_lat=Decimal(str(drop[0])), destination_long=Decimal(str(drop[1])),
        pickup_address=ADDRESS, destination_address=ADDRESS)
    updates = {}
    if requested_at:
        updates['requested_at'] = requested_at
    if stale:
        updates['stale_flagged_at'] = timezone.now()
    if updates:
        Trip.objects.filter(pk=trip.pk).update(**updates)
        trip.refresh_from_db()
    return trip


@pytest.fixture(autouse=True)
def fresh_geography():
    Geography.reset()
    yield
    Geography.reset()


@pytest.fixture
def world(db):
    return build_world()


def body(response):
    return response.json()['data']


def snap(client, **params):
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    return client.get(f'{SNAPSHOT}?{qs}' if qs else SNAPSHOT)


def driver_ids(data):
    return sorted(d['driver_id'] for d in data.get('drivers', []))


#: The socket's `session` id is 12 random hex characters (`consumer.py`): about one run in 60 it
#: holds ten digits in a row and reads as a phone number to the digit-run pattern. It is ours, not a
#: person's, so it is taken out before the scan.
_SESSION_ID = re.compile(r'"session": ?"[0-9a-f]{12}"')


def assert_no_pii(text):
    text = _SESSION_ID.sub('"session": ""', text)
    for name, pattern in RAW_PII_PATTERNS.items():
        assert not pattern.search(text), (name, pattern.search(text).group(0))
    for secret in (DRIVER_NAME, PLATE, RIDER_NAME, 'Jubilee', DRIVER_PHONE_LAST,
                   'Vera Visitor', 'Alex Austin', 'Nina Nowhere'):
        assert secret not in text, secret


# ===========================================================================
# Capabilities
# ===========================================================================

READ_ROLES = ('super_admin', 'city_ops_manager', 'safety_officer')
AGGREGATE_ROLES = ('analyst', 'marketing')
NEITHER_ROLES = ('support_agent', 'support_lead', 'kyc_verifier', 'finance')


class TestCapabilities:
    def test_the_templates_are_valid_and_grant_exactly_what_the_brief_says(self):
        assert validate_templates() == []
        held = {t['code']: set(t['permissions']) for t in ROLE_TEMPLATES}
        for code in READ_ROLES:
            assert 'liveops.read' in held[code], code
        assert 'liveops.aggregate' in held['super_admin']
        for code in AGGREGATE_ROLES:
            assert 'liveops.aggregate' in held[code] and 'liveops.read' not in held[code], code
        for code in NEITHER_ROLES:
            assert not held[code] & {'liveops.read', 'liveops.aggregate'}, code

    @pytest.mark.parametrize('role', READ_ROLES + AGGREGATE_ROLES)
    def test_a_holder_gets_the_snapshot_the_heatmap_and_a_ticket(self, world, role):
        client, _ = world.super if role == 'super_admin' else world.ops[role]
        assert snap(client).status_code == 200
        assert client.get(HEATMAP).status_code == 200
        assert client.post(TICKET).status_code == 201

    @pytest.mark.parametrize('role', ('support_agent', 'kyc_verifier', 'finance'))
    def test_support_kyc_and_finance_get_403_everywhere(self, world, role):
        client, _ = world.ops[role]
        assert snap(client).status_code == 403
        assert client.get(HEATMAP).status_code == 403
        assert client.post(TICKET).status_code == 403

    def test_anonymous_callers_are_refused(self, world):
        from rest_framework.test import APIClient

        anon = APIClient()
        assert anon.get(SNAPSHOT).status_code in (401, 403)
        assert anon.post(TICKET).status_code in (401, 403)

    def test_an_aggregate_caller_gets_no_individual_keys_at_all(self, world):
        put_fleet(world)
        client, _ = world.ops['analyst']
        data = body(snap(client))
        assert data['mode'] == 'aggregate'
        assert not ({'drivers', 'trips', 'alerts'} & set(data))

    def test_the_scope_object_reflects_the_role(self, world):
        _, analyst = world.ops['analyst']
        _, cityops = world.ops['city_ops_manager']
        _, support = world.ops['support_agent']
        assert LiveScope.of(analyst).mode == 'aggregate'
        assert LiveScope.of(cityops).mode == 'full'
        assert not LiveScope.of(support)

    def test_a_role_holding_read_in_one_city_reads_nothing_in_another(self, world):
        """Places are the assignments whose ROLE carries the capability: Support in
        Austin plus City Ops in Hyderabad reads Hyderabad only."""
        client, user = operator('city_ops_manager', world.geo, 'IN-TG-HYD')
        AdminAssignment.objects.create(
            admin=user, role=AdminRole.objects.get(code='support_agent'), scope=world.geo['US-TX-AUS'])
        scope = LiveScope.of(user)
        geography = Geography.get()
        assert scope.reads(geography.by_code('IN-TG-HYD'))
        assert not scope.sees(geography.by_code('US-TX-AUS'))


# ===========================================================================
# Scope: snapshot
# ===========================================================================

@needs_redis
class TestSnapshotScope:
    def test_a_hyderabad_city_ops_sees_hyderabad_and_nothing_of_austin(self, world):
        put_fleet(world)
        client, _ = world.ops['city_ops_manager']
        data = body(snap(client))
        assert driver_ids(data) == [world.drivers.hyd.id]
        assert [c['code'] for c in data['cities']] == ['IN-TG-HYD']
        assert data['cities'][0]['online_drivers'] == 1
        text = json.dumps(data)
        assert 'US-TX-AUS' not in text and str(world.drivers.aus.id) not in {
            str(i) for i in driver_ids(data)}

    def test_a_driver_is_in_the_city_they_are_in_not_the_city_they_live_in(self, world):
        put_fleet(world)
        hyd, _ = world.ops['city_ops_manager']
        aus, _ = world.aus_ops
        assert world.drivers.visitor.id not in driver_ids(body(snap(hyd)))
        in_austin = driver_ids(body(snap(aus)))
        assert in_austin == sorted([world.drivers.aus.id, world.drivers.visitor.id])

    def test_a_city_is_the_zone_of_the_position_then_the_home_city(self, world):
        """Outside every zone, the driver falls back to home_city."""
        put_driver(world.drivers.hyd, (12.0, 77.0))            # outside every zone; home is Hyderabad
        client, _ = world.ops['city_ops_manager']
        assert driver_ids(body(snap(client))) == [world.drivers.hyd.id]

    def test_an_unplaceable_driver_is_the_unrestricted_callers_alone(self, world):
        put_fleet(world)
        city, _ = world.ops['city_ops_manager']
        root, _ = world.super
        assert world.drivers.nowhere.id not in driver_ids(body(snap(city)))
        everything = driver_ids(body(snap(root)))
        assert world.drivers.nowhere.id in everything and len(everything) == 4

    def test_out_of_scope_and_unknown_cities_answer_the_same_404(self, world):
        client, _ = world.ops['city_ops_manager']
        outside = snap(client, city='US-TX-AUS')
        missing = snap(client, city='NO-SUCH-CITY')
        assert outside.status_code == missing.status_code == 404
        assert outside.json()['error']['code'] == missing.json()['error']['code']
        assert outside.json()['error']['message'] == missing.json()['error']['message']

    def test_the_city_filter_narrows_for_a_wide_caller(self, world):
        put_fleet(world)
        root, _ = world.super
        only_austin = body(snap(root, city='US-TX-AUS'))
        assert driver_ids(only_austin) == sorted([world.drivers.aus.id, world.drivers.visitor.id])
        assert only_austin['city'] == 'US-TX-AUS'

    def test_heartbeat_gone_means_gone(self, world):
        put_driver(world.drivers.hyd, HYD, alive=False)
        client, _ = world.ops['city_ops_manager']
        assert driver_ids(body(snap(client))) == []

    def test_on_trip_and_online_status_and_vehicle_class(self, world):
        trip = make_trip(world, world.hyd_zone, state='in_progress', driver=world.drivers.hyd)
        put_driver(world.drivers.hyd, HYD, trip_id=trip.id, heading=181.25)
        put_driver(world.drivers.visitor, HYD_B)
        client, _ = world.ops['city_ops_manager']
        dots = {d['driver_id']: d for d in body(snap(client))['drivers']}
        assert dots[world.drivers.hyd.id]['status'] == 'on_trip'
        assert dots[world.drivers.hyd.id]['trip_id'] == trip.id
        assert dots[world.drivers.hyd.id]['heading'] == 181.2 or dots[world.drivers.hyd.id]['heading'] == 181.3
        assert dots[world.drivers.visitor.id]['status'] == 'online'
        assert dots[world.drivers.visitor.id]['vehicle_class'] == 'sedan'

    def test_the_snapshot_is_capped_with_a_zoom_in_message(self, world, monkeypatch):
        import servers.live_ops.snapshot as snapshot_module

        monkeypatch.setattr(snapshot_module, 'SNAPSHOT_DOT_CAP', 2)
        put_driver(world.drivers.hyd, HYD)
        put_driver(world.drivers.visitor, HYD_B)
        put_driver(world.drivers.nowhere, HYD)
        root, _ = world.super
        data = body(snap(root))
        assert len(data['drivers']) == 2
        assert data['truncated']['drivers'] is True and 'Zoom in' in data['truncated']['message']
        # The count on the status bar is the truth, not the capped list.
        hyd = next(c for c in data['cities'] if c['code'] == 'IN-TG-HYD')
        assert hyd['online_drivers'] == 3

    def test_redis_unreachable_is_reported_not_hidden(self, world, monkeypatch):
        import servers.live_ops.snapshot as snapshot_module

        monkeypatch.setattr(snapshot_module, 'read_live_positions', lambda: None)
        client, _ = world.ops['city_ops_manager']
        data = body(snap(client))
        assert data['presence_available'] is False and data['drivers'] == []


# ===========================================================================
# Scope: trips and alerts, and the city status bar
# ===========================================================================

@needs_redis
class TestTripsAlertsAndStatus:
    def test_trips_are_scoped_minimal_and_rounded(self, world):
        hyd_trip = make_trip(world, world.hyd_zone, pickup=HYD, drop=(17.40123, 78.45987))
        aus_trip = make_trip(world, world.aus_zone, pickup=AUS, drop=(30.3, -97.7))
        client, _ = world.ops['city_ops_manager']
        data = body(snap(client))
        assert [t['id'] for t in data['trips']] == [hyd_trip.id]
        trip = data['trips'][0]
        assert set(trip) <= TRIP_KEYS
        assert trip['pickup_lat'] == 17.43 and trip['drop_lat'] == 17.401 and trip['drop_lng'] == 78.46
        assert trip['state'] == 'requested' and trip['vehicle_class'] == 'sedan'
        assert aus_trip.id not in {t['id'] for t in data['trips']}

    def test_completed_and_cancelled_trips_are_not_on_the_map(self, world):
        make_trip(world, world.hyd_zone, state='completed')
        make_trip(world, world.hyd_zone, state='cancelled')
        live = make_trip(world, world.hyd_zone, state='accepted')
        client, _ = world.ops['city_ops_manager']
        assert [t['id'] for t in body(snap(client))['trips']] == [live.id]

    def test_city_status_counts_only_visible_cities(self, world):
        put_fleet(world)
        make_trip(world, world.hyd_zone, requested_at=timezone.now() - timedelta(minutes=7))
        make_trip(world, world.hyd_zone, state='in_progress', driver=world.drivers.hyd, stale=True)
        make_trip(world, world.aus_zone, pickup=AUS)
        client, _ = world.ops['city_ops_manager']
        rows = body(snap(client))['cities']
        assert len(rows) == 1
        row = rows[0]
        assert (row['unassigned_trips'], row['stale_trips']) == (1, 1)
        assert 400 <= row['oldest_unassigned_age_s'] <= 460
        assert row['network_status'] in {'NORMAL', 'HIGH DEMAND', 'CRITICAL', 'OFFLINE'}

    def test_network_status_keeps_the_ratios_of_the_django_screen(self):
        assert network_status(0, 0, 0) == 'OFFLINE'
        assert network_status(0, 0, 5) == 'OFFLINE'
        assert network_status(4, 0, 4) == 'NORMAL'            # equal is not "more"
        assert network_status(4, 0, 5) == 'HIGH DEMAND'       # more than 1x
        assert network_status(4, 0, 8) == 'HIGH DEMAND'       # exactly 2x is not beyond 2x
        assert network_status(4, 0, 9) == 'CRITICAL'          # beyond 2x
        assert network_status(0, 3, 0) == 'NORMAL'            # busy drivers are not an outage
        assert network_status(0, 3, 1) == 'CRITICAL'          # but waiting riders with none idle are

    def test_sos_pins_only_for_callers_who_see_live_sos(self, world):
        trip = make_trip(world, world.hyd_zone, state='in_progress', driver=world.drivers.hyd)
        event = SOSEvent.objects.create(
            user=world.rider, initiated_by='rider', trip=trip, latitude=Decimal('17.43012'),
            longitude=Decimal('78.40034'))
        OperatorAlert.objects.create(
            kind=SAFETY_SOS_ESCALATED, severity=CRITICAL, title=f'SOS #{event.id} not acknowledged',
            payload={'sos_id': event.id}, source_type='sos_event', source_id=str(event.id),
            city=world.geo['IN-TG-HYD'])
        OperatorAlert.objects.create(
            kind='metric.unfulfilled', severity='warning', title='Unfulfilled rate high',
            city=world.geo['IN-TG-HYD'])

        # A role that may read the map but is neither a responder nor City Ops.
        viewer_role = AdminRole.objects.create(
            code='map_viewer', name='Map viewer', permissions=['liveops.read'])
        viewer, vuser = operator('analyst', world.geo, 'IN-TG-HYD')
        AdminAssignment.objects.filter(admin=vuser).update(role=viewer_role)

        seen_by = {}
        for label, client in (('safety', world.ops['safety_officer'][0]),
                              ('cityops', world.ops['city_ops_manager'][0]),
                              ('super', world.super[0]), ('viewer', viewer)):
            alerts = body(snap(client)).get('alerts', [])
            seen_by[label] = {a['kind'] for a in alerts}
            for alert in alerts:
                assert set(alert) <= ALERT_KEYS
        assert SAFETY_SOS_ESCALATED in seen_by['safety']
        assert SAFETY_SOS_ESCALATED in seen_by['cityops']
        assert SAFETY_SOS_ESCALATED in seen_by['super']
        assert SAFETY_SOS_ESCALATED not in seen_by['viewer'], 'sees_live_sos gates the SOS overlay'
        assert 'metric.unfulfilled' in seen_by['viewer']

        pins = [a for a in body(snap(world.ops['safety_officer'][0]))['alerts'] if a['is_sos']]
        assert pins[0]['lat'] == 17.43 or pins[0]['lat'] == 17.430   # 3 decimals, masked by role

    def test_finance_alert_kinds_never_appear_on_the_map(self, world):
        OperatorAlert.objects.create(
            kind=SETTLEMENT_NO_COMMISSION_RULE, severity=CRITICAL, title='Trip 9 unsettled',
            city=world.geo['IN-TG-HYD'])
        for client, _ in (world.super, world.ops['city_ops_manager']):
            kinds = {a['kind'] for a in body(snap(client))['alerts']}
            assert SETTLEMENT_NO_COMMISSION_RULE not in kinds

    def test_an_alert_for_another_city_is_not_shown(self, world):
        OperatorAlert.objects.create(
            kind='metric.unfulfilled', severity='warning', title='Austin', city=world.geo['US-TX-AUS'])
        client, _ = world.ops['city_ops_manager']
        assert body(snap(client))['alerts'] == []


# ===========================================================================
# Payload minimisation (the PII sweep for this module)
# ===========================================================================

@needs_redis
class TestNoPersonalData:
    def test_no_payload_carries_a_name_phone_plate_or_rider_in_any_role(self, world):
        put_fleet(world)
        make_trip(world, world.hyd_zone, driver=world.drivers.hyd, state='in_progress')
        make_trip(world, world.aus_zone, pickup=AUS)
        every = [world.super, world.aus_ops, *world.ops.values()]
        for client, _ in every:
            for response in (snap(client), client.get(HEATMAP), client.get(f'{HEATMAP}?window=1440')):
                if response.status_code != 200:
                    continue
                assert_no_pii(response.content.decode())

    def test_dots_are_built_from_the_whitelist_only(self, world):
        put_fleet(world)
        root, _ = world.super
        for dot in body(snap(root))['drivers']:
            assert set(dot) == DOT_KEYS, set(dot) ^ DOT_KEYS
            assert not set(dot) & FORBIDDEN_KEYS
        assert not DOT_KEYS & FORBIDDEN_KEYS and not TRIP_KEYS & FORBIDDEN_KEYS
        assert not ALERT_KEYS & FORBIDDEN_KEYS

    def test_dots_carry_five_decimals_to_pii_view_holders_and_two_to_the_rest(self, world):
        put_driver(world.drivers.hyd, HYD)
        trusted, _ = world.ops['city_ops_manager']
        assert body(snap(trusted))['drivers'][0]['lat'] == 17.43012
        # A role that reads the map without pii.view gets the masking layer's two decimals.
        role = AdminRole.objects.create(code='blind_map', name='Blind map', permissions=['liveops.read'])
        blind, user = operator('analyst', world.geo, 'IN-TG-HYD')
        AdminAssignment.objects.filter(admin=user).update(role=role)
        assert body(snap(blind))['drivers'][0]['lat'] == 17.43


# ===========================================================================
# The per-frame path
# ===========================================================================

def frame(driver, point, **extra):
    """What `DriverLocationConsumer` publishes: PII included, by design of that consumer."""
    return {'type': 'driver_location_update', 'driver_id': driver.id,
            'lat': point[0], 'lng': point[1], 'status': 'online',
            'driver_name': DRIVER_NAME, 'phone_number': '+919800000042',
            'vehicle_number': PLATE, 'vehicle_model': 'Dzire', 'ratings': '4.80', **extra}


def scope_of(user):
    return LiveScope.of(user)


class TestPerFrameFilter:
    def test_an_austin_drivers_frame_never_reaches_a_hyderabad_city_ops_socket(self, world):
        _, cityops = world.ops['city_ops_manager']
        flt = FrameFilter(scope_of(cityops), exact=True)
        flt.offer_location(frame(world.drivers.aus, AUS))
        flt.offer_location(frame(world.drivers.visitor, AUS))   # Hyderabad's, but in Austin
        assert flt.flush() == []
        assert flt.visible == {}
        flt.offer_location(frame(world.drivers.hyd, HYD))
        out = flt.flush()
        assert [f['driver_id'] for f in out] == [world.drivers.hyd.id]

    def test_the_same_frame_reaches_austin_ops_and_a_global_super_admin(self, world):
        _, aus = world.aus_ops
        _, root = world.super
        for user in (aus, root):
            flt = FrameFilter(scope_of(user), exact=True)
            flt.offer_location(frame(world.drivers.aus, AUS))
            assert [f['driver_id'] for f in flt.flush()] == [world.drivers.aus.id]

    def test_frames_are_rebuilt_never_forwarded(self, world):
        _, root = world.super
        flt = FrameFilter(scope_of(root), exact=True)
        flt.offer_location(frame(world.drivers.hyd, HYD, heading=45, active_trip_id=17, status='busy'))
        out = flt.flush()[0]
        assert set(out) == DOT_KEYS | {'type'}
        assert out['status'] == 'on_trip' and out['trip_id'] == 17 and out['heading'] == 45.0
        assert_no_pii(json.dumps(out))

    def test_a_burst_from_one_driver_is_coalesced_to_the_latest_position(self, world):
        _, root = world.super
        flt = FrameFilter(scope_of(root), exact=True)
        for i in range(50):
            flt.offer_location(frame(world.drivers.hyd, (HYD[0] + i * 0.0001, HYD[1])))
        out = flt.flush()
        assert len(out) == 1 and out[0]['lat'] == round(HYD[0] + 49 * 0.0001, 5)
        assert flt.flush() == [], 'nothing is re-sent until the driver pings again'

    def test_a_driver_who_goes_offline_is_removed_only_where_the_socket_knew_them(self, world):
        _, cityops = world.ops['city_ops_manager']
        flt = FrameFilter(scope_of(cityops), exact=True)
        flt.offer_location(frame(world.drivers.hyd, HYD))
        flt.flush()
        flt.offer_status({'driver_id': world.drivers.hyd.id, 'status': 'offline'})
        flt.offer_status({'driver_id': world.drivers.aus.id, 'status': 'offline'})
        out = flt.flush()
        assert out == [{'type': 'driver_status_update', 'driver_id': world.drivers.hyd.id,
                        'status': 'offline'}]

    def test_a_driver_who_drives_out_of_scope_is_removed(self, world):
        _, cityops = world.ops['city_ops_manager']
        flt = FrameFilter(scope_of(cityops), exact=True)
        flt.offer_location(frame(world.drivers.visitor, HYD))
        assert len(flt.flush()) == 1
        flt.offer_location(frame(world.drivers.visitor, AUS))
        assert flt.flush() == [{'type': 'driver_status_update',
                                'driver_id': world.drivers.visitor.id, 'status': 'offline'}]

    def test_narrowing_the_scope_removes_what_fell_outside(self, world):
        _, root = world.super
        flt = FrameFilter(scope_of(root), exact=True)
        flt.offer_location(frame(world.drivers.hyd, HYD))
        flt.offer_location(frame(world.drivers.aus, AUS))
        assert len(flt.flush()) == 2
        _, cityops = world.ops['city_ops_manager']
        removed = flt.rescope(scope_of(cityops))
        assert [f['driver_id'] for f in removed] == [world.drivers.aus.id]

    def test_malformed_frames_are_ignored(self, world):
        _, root = world.super
        flt = FrameFilter(scope_of(root), exact=True)
        flt.offer_location({'driver_id': 'x', 'lat': 1, 'lng': 2})
        flt.offer_location({'driver_id': 1})
        flt.offer_location({'driver_id': world.drivers.hyd.id, 'lat': 'north', 'lng': 2})
        assert flt.flush() == []


# ===========================================================================
# The single-use ticket
# ===========================================================================

@needs_redis
class TestTicket:
    def test_a_ticket_works_once(self, world):
        ticket = issue_ticket(7)
        assert consume_ticket(ticket) == 7
        assert consume_ticket(ticket) is None

    def test_it_is_stored_hashed_with_a_30_second_ttl_and_the_user_id(self, world):
        ticket = issue_ticket(7)
        keys = list(redis_client.scan_iter(match=f'{cfg.TICKET_KEY_PREFIX}*'))
        assert len(keys) == 1 and ticket not in keys[0]
        assert redis_client.get(keys[0]) == '7'
        assert 0 < redis_client.ttl(keys[0]) <= cfg.TICKET_TTL_SECONDS == 30

    def test_an_expired_ticket_is_refused(self, world):
        ticket = issue_ticket(7)
        key = next(redis_client.scan_iter(match=f'{cfg.TICKET_KEY_PREFIX}*'))
        redis_client.delete(key)             # what the TTL does at 30 s
        assert consume_ticket(ticket) is None

    def test_unknown_and_absurd_tickets_are_refused(self, world):
        assert consume_ticket('not-a-ticket') is None
        assert consume_ticket('') is None
        assert consume_ticket('x' * 5000) is None

    def test_tickets_are_distinct(self, world):
        assert len({issue_ticket(1) for _ in range(20)}) == 20

    def test_the_endpoint_returns_a_usable_ticket_and_audits_it(self, world):
        client, user = world.ops['city_ops_manager']
        data = body(client.post(TICKET))
        assert data['expires_in'] == 30 and data['path'] == WS and data['mode'] == 'full'
        assert consume_ticket(data['ticket']) == user.id
        row = AdminAuditLog.objects.filter(action=cfg.AUDIT_TICKET_ISSUED).latest('id')
        assert row.actor_id == user.id and row.after['capability'] == 'liveops.read'

    def test_the_endpoint_never_logs_the_ticket(self, world, caplog):
        import logging

        client, _ = world.ops['city_ops_manager']
        with caplog.at_level(logging.DEBUG):
            data = body(client.post(TICKET))
        assert data['ticket'] not in caplog.text
        assert data['ticket'] not in json.dumps(
            list(AdminAuditLog.objects.values_list('after', flat=True)), default=str)

    def test_a_ticket_store_outage_refuses_rather_than_falling_back(self, world, monkeypatch):
        import servers.live_ops.tickets as tickets_module

        monkeypatch.setattr(tickets_module, '_client', lambda: (_ for _ in ()).throw(
            tickets_module.TicketStoreUnavailable('down')))
        client, _ = world.ops['city_ops_manager']
        assert client.post(TICKET).status_code == 503


# ===========================================================================
# The heatmap
# ===========================================================================

class TestHeatmapMath:
    def test_cells_are_a_hundredth_of_a_degree(self):
        assert hm.cell_of(17.4301, 78.4003) == (1743, 7840)
        assert hm.cell_of(17.4399, 78.4099) == (1743, 7840)
        assert hm.cell_of(17.4400, 78.4100) == (1744, 7841)
        assert hm.corner_of(1743, 7840) == (17.43, 78.4)
        assert hm.cell_of(-33.8688, -151.2093) == (-3387, -15121)   # floor, not truncation

    def test_demand_above_supply_is_short(self):
        # 6 requests in 60 minutes = 1.5 per quarter hour against 1 idle driver.
        state, per = hm.shade(6, 1.0, 60)
        assert state == 'short' and per == 1.5
        assert hm.shade(4, 1.0, 60)[0] == 'balanced'        # exactly 1 per quarter hour
        assert hm.shade(1, 0.0, 15)[0] == 'short'           # a request and nobody
        assert hm.shade(0, 0.0, 15)[0] == 'balanced'

    def test_merge_combines_both_sides_and_averages_supply_over_samples(self):
        cells = hm.merge_cells({(1, 1): 5}, {(1, 1): (30, 4), (2, 2): (3, 1)}, samples=15, window_minutes=15)
        by = {(c['row'], c['col']): c for c in cells}
        assert by[(1, 1)]['demand'] == 5 and by[(1, 1)]['supply'] == 2.0
        assert by[(1, 1)]['events'] == 5 + 4 and by[(1, 1)]['shade'] == 'short'
        assert by[(2, 2)]['demand'] == 0 and by[(2, 2)]['supply'] == 0.2

    def test_k_anonymity_drops_cells_under_three_events(self):
        cells = [{'events': n} for n in (0, 1, 2, 3, 4)]
        kept, dropped = hm.suppress_small(cells)
        assert [c['events'] for c in kept] == [3, 4] and dropped == 3

    def test_windows_are_validated(self):
        assert hm.parse_window(None) == 15
        assert hm.parse_window('60') == 60 and hm.parse_window('1440') == 1440
        for bad in ('5', 'x', '-15', '0'):
            with pytest.raises(hm.BadWindow):
                hm.parse_window(bad)


def sample(world, node, point, drivers, minutes_ago=1):
    row, col = hm.cell_of(*point)
    when = (timezone.now() - timedelta(minutes=minutes_ago)).replace(second=0, microsecond=0)
    SupplySample.objects.create(city=node, cell_row=row, cell_col=col, drivers=drivers, sampled_at=when)


class TestHeatmapEndpoint:
    def test_demand_and_supply_per_cell_for_a_reader(self, world):
        for _ in range(4):
            make_trip(world, world.hyd_zone, pickup=HYD)
        make_trip(world, world.hyd_zone, pickup=HYD_B)
        sample(world, world.geo['IN-TG-HYD'], HYD, 2)
        client, _ = world.ops['city_ops_manager']
        data = body(client.get(HEATMAP))
        assert data['title'] == 'Demand and supply' and data['window_minutes'] == 15
        assert data['cell_degrees'] == 0.01 and data['k_anonymity'] is None
        cell = next(c for c in data['cells'] if (c['row'], c['col']) == hm.cell_of(*HYD))
        assert cell['demand'] == 4 and cell['supply'] == 2.0 and cell['shade'] == 'short'
        assert any((c['row'], c['col']) == hm.cell_of(*HYD_B) and c['demand'] == 1 for c in data['cells'])

    def test_a_reader_sees_small_cells_an_aggregate_caller_does_not(self, world):
        make_trip(world, world.hyd_zone, pickup=HYD_B)                  # 1 request: a lone rider
        for _ in range(3):
            make_trip(world, world.hyd_zone, pickup=HYD)                # 3: a crowd
        reader, _ = world.ops['city_ops_manager']
        analyst, _ = world.ops['analyst']
        marketing, _ = world.ops['marketing']
        assert len(body(reader.get(HEATMAP))['cells']) == 2
        for client in (analyst, marketing):
            data = body(client.get(HEATMAP))
            assert [(c['row'], c['col']) for c in data['cells']] == [hm.cell_of(*HYD)]
            assert data['k_anonymity'] == 3 and data['suppressed_cells'] == 1

    def test_aggregate_cells_carry_no_coordinate_finer_than_the_grid(self, world):
        for _ in range(3):
            make_trip(world, world.hyd_zone, pickup=HYD)
        client, _ = world.ops['analyst']
        text = client.get(HEATMAP).content.decode()
        for fragment in ('17.43012', '78.40034', 'pickup', '"lat"', '"lng"'):
            assert fragment not in text
        cell = body(client.get(HEATMAP))['cells'][0]
        assert cell['cell_south'] == 17.43 and cell['cell_west'] == 78.4

    def test_a_cell_is_scoped_to_the_callers_cities(self, world):
        for _ in range(3):
            make_trip(world, world.aus_zone, pickup=AUS)
        for _ in range(3):
            make_trip(world, world.hyd_zone, pickup=HYD)
        hyd, _ = world.ops['city_ops_manager']
        data = body(hyd.get(HEATMAP))
        assert all(c['row'] == hm.cell_of(*HYD)[0] for c in data['cells'])
        assert hyd.get(f'{HEATMAP}?city=US-TX-AUS').status_code == 404
        aus, _ = world.aus_ops
        assert body(aus.get(HEATMAP))['cells'][0]['row'] == hm.cell_of(*AUS)[0]

    def test_the_window_bounds_demand(self, world):
        make_trip(world, world.hyd_zone, pickup=HYD, requested_at=timezone.now() - timedelta(minutes=50))
        make_trip(world, world.hyd_zone, pickup=HYD, requested_at=timezone.now() - timedelta(hours=5))
        client, _ = world.ops['city_ops_manager']
        assert body(client.get(f'{HEATMAP}?window=15'))['cells'] == []
        assert body(client.get(f'{HEATMAP}?window=60'))['cells'][0]['demand'] == 1
        assert body(client.get(f'{HEATMAP}?window=1440'))['cells'][0]['demand'] == 2

    def test_a_bad_window_is_a_400(self, world):
        client, _ = world.ops['city_ops_manager']
        assert client.get(f'{HEATMAP}?window=7').status_code == 400

    def test_a_reader_in_one_city_is_still_guarded_in_another(self, world):
        """Reading Hyderabad individually does not unmask a city held as aggregate only."""
        client, user = operator('city_ops_manager', world.geo, 'IN-TG-HYD')
        AdminAssignment.objects.create(
            admin=user, role=AdminRole.objects.get(code='analyst'), scope=world.geo['US-TX-AUS'])
        make_trip(world, world.aus_zone, pickup=AUS)                    # 1 event
        make_trip(world, world.hyd_zone, pickup=HYD)                    # 1 event, but readable
        data = body(client.get(HEATMAP))
        assert [c['row'] for c in data['cells']] == [hm.cell_of(*HYD)[0]]
        assert data['suppressed_cells'] == 1


class TestSupplySampler:
    def test_it_counts_idle_drivers_per_cell_and_never_stores_an_id(self, world):
        from servers.live_ops.tasks import prune, sample_now
        from servers.live_ops.positions import LivePosition

        positions = [
            LivePosition(world.drivers.hyd.id, *HYD, None, None),
            LivePosition(world.drivers.visitor.id, HYD[0] + 0.0001, HYD[1], None, None),
            LivePosition(world.drivers.aus.id, *AUS, None, None),
            LivePosition(world.drivers.nowhere.id, 12.0, 77.0, None, None),                    # no city
            LivePosition(world.drivers.hyd.id + 900, *HYD_B, None, None, status='on_trip'),    # busy
        ]
        assert sample_now(positions=positions) == 2
        rows = {(s.city.code, s.cell_row, s.cell_col): s.drivers for s in SupplySample.objects.all()}
        assert rows == {('IN-TG-HYD', *hm.cell_of(*HYD)): 2, ('US-TX-AUS', *hm.cell_of(*AUS)): 1}
        assert {f.name for f in SupplySample._meta.get_fields()} == {
            'id', 'city', 'cell_row', 'cell_col', 'drivers', 'sampled_at'}
        # A doubled run (acks_late redelivery) writes the same rows.
        sample_now(positions=positions)
        assert SupplySample.objects.count() == 2
        SupplySample.objects.update(sampled_at=timezone.now() - timedelta(hours=30))
        assert prune() == 2


# ===========================================================================
# The Django screen it replaces
# ===========================================================================

class TestParity:
    def test_the_network_status_constants_are_named_and_match_the_old_ratios(self):
        assert cfg.CRITICAL_REQUEST_TO_DRIVER_RATIO == 2
        assert cfg.HIGH_DEMAND_REQUEST_TO_DRIVER_RATIO == 1
        assert (cfg.STATUS_OFFLINE, cfg.STATUS_CRITICAL, cfg.STATUS_HIGH_DEMAND, cfg.STATUS_NORMAL) == (
            'OFFLINE', 'CRITICAL', 'HIGH DEMAND', 'NORMAL')


# ===========================================================================
# The socket
# ===========================================================================

ws = [pytest.mark.postgres, pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def open_socket(user_id, *, path=WS, query=None):
    from base.asgi import application

    ticket = await asyncio.to_thread(issue_ticket, user_id)
    comm = WebsocketCommunicator(application, f'{path}?{query if query is not None else "ticket=" + ticket}')
    connected, _ = await comm.connect(timeout=10)
    return comm, connected, ticket


async def closed_with(comm, timeout=5):
    out = await comm.receive_output(timeout=timeout)
    assert out['type'] == 'websocket.close', out
    return out['code']


async def drain(comm, wait=0.5):
    """Everything the socket sends in `wait` seconds. Reads the output queue
    directly: `receive_output(timeout=)` CANCELS the application on timeout."""
    frames = []
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        while True:
            try:
                out = comm.output_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if out['type'] == 'websocket.send':
                frames.append(json.loads(out['text']))
            else:
                frames.append(out)
    return frames


def types_of(frames):
    return [f.get('type') for f in frames]


@pytest.fixture
def fast_socket(monkeypatch):
    monkeypatch.setattr(cfg, 'STREAM_FRAME_INTERVAL_SECONDS', 0.1)


@needs_redis
class TestSocketGates:
    pytestmark = ws

    async def test_no_ticket_is_refused_with_4001(self):
        world = await asyncio.to_thread(build_world)
        _, user = world.ops['city_ops_manager']
        comm, connected, _ = await open_socket(user.id, query='')
        assert connected and await closed_with(comm) == 4001
        await comm.disconnect()

    async def test_the_jwt_in_the_url_is_refused_on_the_admin_socket(self):
        from rest_framework_simplejwt.tokens import AccessToken

        world = await asyncio.to_thread(build_world)
        _, root = world.super
        token = str(await asyncio.to_thread(AccessToken.for_user, root))
        for path in (WS, '/ws/admin/dashboard/'):
            comm, connected, _ = await open_socket(root.id, path=path, query=f'token={token}')
            assert connected and await closed_with(comm) == 4001, path
            await comm.disconnect()

    async def test_a_ticket_is_single_use(self, fast_socket):
        world = await asyncio.to_thread(build_world)
        _, user = world.ops['city_ops_manager']
        comm, connected, ticket = await open_socket(user.id)
        assert connected
        first = await drain(comm, 0.3)
        assert 'hello' in types_of(first)
        again = WebsocketCommunicator(__import__('base.asgi', fromlist=['application']).application,
                                      f'{WS}?ticket={ticket}')
        ok, _ = await again.connect(timeout=10)
        assert ok and await closed_with(again) == 4001
        await again.disconnect()
        await comm.disconnect()

    async def test_a_ticket_for_somebody_else_is_still_only_the_issuers(self):
        """The ticket is bound to the user id it was minted for: the connecting
        session is that user's, with that user's scope, whoever holds the string."""
        world = await asyncio.to_thread(build_world)
        _, analyst = world.ops['analyst']
        comm, connected, _ = await open_socket(analyst.id)
        assert connected
        frames = await drain(comm, 0.3)
        hello = next(f for f in frames if f.get('type') == 'hello')
        assert hello['mode'] == 'aggregate'
        await comm.disconnect()

    @pytest.mark.parametrize('role', ('support_agent', 'kyc_verifier', 'finance'))
    async def test_no_capability_is_refused_with_4003(self, role):
        world = await asyncio.to_thread(build_world)
        _, user = world.ops[role]
        comm, connected, _ = await open_socket(user.id)
        assert connected and await closed_with(comm) == 4003
        await comm.disconnect()

    async def test_an_inactive_account_is_refused(self):
        world = await asyncio.to_thread(build_world)
        _, user = world.ops['city_ops_manager']
        await asyncio.to_thread(lambda: User.objects.filter(pk=user.pk).update(is_active=False))
        comm, connected, _ = await open_socket(user.id)
        assert connected and await closed_with(comm) == 4001
        await comm.disconnect()

    async def test_a_ticket_store_outage_closes_with_1013(self, monkeypatch):
        import servers.live_ops.consumer as consumer_module
        from servers.live_ops.tickets import TicketStoreUnavailable

        def down(_ticket):
            raise TicketStoreUnavailable('down')

        monkeypatch.setattr(consumer_module, 'consume_ticket', down)
        world = await asyncio.to_thread(build_world)
        _, user = world.ops['city_ops_manager']
        comm, connected, _ = await open_socket(user.id, query='ticket=anything')
        assert connected and await closed_with(comm) == 1013
        await comm.disconnect()


@needs_redis
class TestSocketStream:
    pytestmark = ws

    async def test_a_reader_gets_hello_and_only_their_scopes_initial_dots(self, fast_socket):
        world = await asyncio.to_thread(build_world)
        await asyncio.to_thread(put_fleet, world)
        _, user = world.ops['city_ops_manager']
        comm, connected, _ = await open_socket(user.id)
        frames = await drain(comm, 0.4)
        hello = next(f for f in frames if f['type'] == 'hello')
        initial = next(f for f in frames if f['type'] == 'initial_drivers')
        assert hello['mode'] == 'full' and hello['cities'] == ['IN-TG-HYD']
        assert [d['driver_id'] for d in initial['drivers']] == [world.drivers.hyd.id]
        assert_no_pii(json.dumps(frames))
        await comm.disconnect()

    async def test_an_austin_frame_never_reaches_a_hyderabad_socket(self, fast_socket):
        world = await asyncio.to_thread(build_world)
        _, hyd_user = world.ops['city_ops_manager']
        _, aus_user = world.aus_ops
        _, root = world.super
        hyd, _, _ = await open_socket(hyd_user.id)
        aus, _, _ = await open_socket(aus_user.id)
        top, _, _ = await open_socket(root.id)
        for comm in (hyd, aus, top):
            await drain(comm, 0.3)

        layer = get_channel_layer()
        await layer.group_send('admin_dashboard', frame(world.drivers.aus, AUS))
        await layer.group_send('admin_dashboard', frame(world.drivers.hyd, HYD))
        seen = {name: [f for f in await drain(comm, 0.6) if f.get('type') == 'driver_location_update']
                for name, comm in (('hyd', hyd), ('aus', aus), ('top', top))}

        assert [f['driver_id'] for f in seen['hyd']] == [world.drivers.hyd.id]
        assert [f['driver_id'] for f in seen['aus']] == [world.drivers.aus.id]
        assert sorted(f['driver_id'] for f in seen['top']) == sorted(
            [world.drivers.aus.id, world.drivers.hyd.id])
        for frames in seen.values():
            for f in frames:
                assert set(f) == DOT_KEYS | {'type'}
            assert_no_pii(json.dumps(frames))
        for comm in (hyd, aus, top):
            await comm.disconnect()

    async def test_a_burst_is_throttled_to_one_frame_per_driver_per_tick(self, fast_socket):
        world = await asyncio.to_thread(build_world)
        _, root = world.super
        comm, _, _ = await open_socket(root.id)
        await drain(comm, 0.3)
        layer = get_channel_layer()
        for i in range(40):
            await layer.group_send('admin_dashboard', frame(world.drivers.hyd, (HYD[0] + i * 0.0001, HYD[1])))
        frames = [f for f in await drain(comm, 0.8) if f.get('type') == 'driver_location_update']
        assert 1 <= len(frames) <= 3, len(frames)
        assert frames[-1]['lat'] == round(HYD[0] + 39 * 0.0001, 5), 'the latest position wins'
        await comm.disconnect()

    async def test_an_aggregate_socket_never_receives_a_driver_frame(self, fast_socket):
        world = await asyncio.to_thread(build_world)
        await asyncio.to_thread(put_fleet, world)
        _, analyst = world.ops['analyst']
        comm, _, _ = await open_socket(analyst.id)
        layer = get_channel_layer()
        await layer.group_send('admin_dashboard', frame(world.drivers.hyd, HYD))
        await layer.group_send('admin_dashboard', {'type': 'driver_status_update',
                                                   'driver_id': world.drivers.hyd.id, 'status': 'offline'})
        frames = await drain(comm, 0.8)
        assert {'hello', 'city_status'} <= set(types_of(frames))
        assert not {'driver_location_update', 'initial_drivers', 'driver_status_update'} & set(types_of(frames))
        assert_no_pii(json.dumps(frames))
        await comm.disconnect()

    async def test_the_session_is_audited_on_open_and_close_and_never_per_frame(self, fast_socket):
        world = await asyncio.to_thread(build_world)
        _, user = world.ops['city_ops_manager']
        before = await asyncio.to_thread(
            lambda: AdminAuditLog.objects.filter(action__startswith='live_ops_session').count())
        comm, _, _ = await open_socket(user.id)
        layer = get_channel_layer()
        for i in range(15):
            await layer.group_send('admin_dashboard', frame(world.drivers.hyd, (HYD[0] + i * 0.001, HYD[1])))
            await asyncio.sleep(0.12)
        await comm.disconnect()
        await asyncio.sleep(0.3)

        def rows():
            return list(AdminAuditLog.objects.filter(action__startswith='live_ops_session').order_by('id'))

        got = (await asyncio.to_thread(rows))[before:]
        assert [r.action for r in got] == [cfg.AUDIT_SESSION_OPENED, cfg.AUDIT_SESSION_CLOSED]
        opened, closed = got
        assert opened.actor_id == user.id and opened.after['capability'] == 'liveops.read'
        assert opened.after['scope']['read'] and 'IN-TG-HYD' in opened.after['cities']
        assert closed.after['reason'] == 'closed' and closed.after['frames_sent'] >= 15
        assert opened.target_id == closed.target_id
        assert '17.43' not in json.dumps([opened.after, closed.after])

    async def test_a_revoked_assignment_closes_the_stream_with_4003(self, fast_socket, monkeypatch):
        monkeypatch.setattr(cfg, 'SCOPE_RECHECK_SECONDS', 0.3)
        world = await asyncio.to_thread(build_world)
        _, user = world.ops['city_ops_manager']
        comm, _, _ = await open_socket(user.id)
        await drain(comm, 0.2)

        await asyncio.to_thread(
            lambda: AdminAssignment.objects.filter(admin=user).update(is_active=False))
        frames = await drain(comm, 2.0)
        closes = [f for f in frames if f.get('type') == 'websocket.close']
        assert [c['code'] for c in closes] == [4003]
        await comm.disconnect()
        await asyncio.sleep(0.2)
        closed = await asyncio.to_thread(
            lambda: AdminAuditLog.objects.filter(action=cfg.AUDIT_SESSION_CLOSED).latest('id'))
        assert closed.after['reason'] == 'revoked' and closed.actor_id == user.id

    async def test_a_narrowed_scope_removes_the_dots_that_fell_outside(self, fast_socket, monkeypatch):
        monkeypatch.setattr(cfg, 'SCOPE_RECHECK_SECONDS', 0.3)
        world = await asyncio.to_thread(build_world)
        await asyncio.to_thread(put_fleet, world)
        _, root = world.super
        # A second assignment keeps the account alive while the Global one goes.
        await asyncio.to_thread(lambda: AdminAssignment.objects.create(
            admin=root, role=AdminRole.objects.get(code='city_ops_manager'), scope=world.geo['IN-TG-HYD']))
        comm, _, _ = await open_socket(root.id)
        frames = await drain(comm, 0.3)
        initial = next(f for f in frames if f['type'] == 'initial_drivers')
        assert len(initial['drivers']) == 4

        await asyncio.to_thread(lambda: AdminAssignment.objects.filter(
            admin=root, role__code='super_admin').update(is_active=False))
        frames = await drain(comm, 1.2)
        removed = {f['driver_id'] for f in frames if f.get('type') == 'driver_status_update'}
        assert removed == {world.drivers.aus.id, world.drivers.visitor.id, world.drivers.nowhere.id}
        await comm.disconnect()
