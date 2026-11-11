"""Module 9: Zones and geofences. Versioned polygons through maker-checker.

WHAT THESE PROVE
----------------
  * EVERY CHANGE IS A REQUEST. Creating a zone, changing its polygon or settings,
    deactivating, reactivating and restoring a revision answer 202 and write nothing until
    a DIFFERENT admin approves; the viewset keeps its URLs.
  * EVERY APPLIED CHANGE IS A REVISION, with maker, checker, reason and a full snapshot;
    rollback is "propose restoring revision N" and is a normal request. A backfill revision
    v1 exists for every zone that predates versioning.
  * VALIDATION IS IN THE SERVER: a closed ring, >= 4 positions, no self-intersection, lon/lat
    range, area between the two constants; no overlapping active city zones in a country; a
    child zone inside its parent; country == the geo node's country; market fields match the
    country unless an override reason is given.
  * DEACTIVATION is refused under a live trip and when it would leave a live city with no
    zone; readiness has a `zone` item.
  * THE IMPACT PREVIEW counts the last 30 days of pickups that would move (old -> new), the
    drivers whose recent pickups would change city, and the fare cards newly applying; the
    POINT TESTER resolves with the same function quoting uses, for the live and the draft shape.
  * NO LEGACY POLYGON: with no active zone there is no service, with a clear code.
  * SCOPE: City Ops sees and drafts only its cities; out of scope is 404, in scope without
    zone.write is 403; a checker needs approval.check AND zone.write.

Fixture passwords are not used: tokens are minted directly.
"""

import copy
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from finance_world import (
    client_for, make_driver, make_rider, operator, status_obj,
)
from servers.admin_access.models import GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalPolicy, ApprovalRequest
from servers.driver.models import VehicleType
from servers.pricing import zone_geometry as geom
from servers.pricing.models import RateCard, ServiceZone, ServiceZoneRevision
from servers.pricing.services import ZoneIndex, find_zone_for_point
from servers.ride.models import Trip
from test_finance_actions import approve_request, body_of, checker, error_code, get, post

pytestmark = [pytest.mark.django_db]

BASE = '/api/v1/pricing/admin'
ZONES = f'{BASE}/zones'
LONG_AGO = timezone.now() - timedelta(days=400)


def ring(lon0, lat0, lon1, lat1):
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]


def poly(lon0, lat0, lon1, lat1):
    return {'type': 'Polygon', 'coordinates': [ring(lon0, lat0, lon1, lat1)]}


# Bengaluru, far from every seeded zone; ~ 20 km x 17 km.
BLR = poly(77.50, 12.90, 77.70, 13.05)
# An airport-sized box inside Hyderabad's seeded polygon.
AIRPORT = poly(78.40, 17.21, 78.46, 17.27)


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    nodes = {n.code: n for n in GeoNode.objects.all()}
    # A Bengaluru city node to hang a new zone from.
    ka = GeoNode.objects.create(kind='state', code='IN-KA', name='Karnataka', parent=nodes['IN'])
    blr = GeoNode.objects.create(kind='city', code='IN-KA-BLR', name='Bengaluru', parent=ka)
    nodes.update({'IN-KA': ka, 'IN-KA-BLR': blr})
    return nodes


@pytest.fixture
def world(geo):
    hyd = ServiceZone.objects.get(code='IN-TG-HYD')
    hyd.geo_node = geo['IN-TG-HYD']
    hyd.save()
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')
    return SimpleNamespace(
        geo=geo, hyd=hyd, sedan=sedan,
        super=operator('super_admin', geo),
        ops=operator('city_ops_manager', geo, 'IN-TG-HYD'),
        analyst=operator('analyst', geo, 'IN-TG-HYD'),
        finance=operator('finance', geo, 'IN-TG-HYD'))


def new_body(code='IN-KA-BLR', **over):
    body = {'code': code, 'name': 'Bengaluru', 'country': 'IN', 'state_code': 'KA',
            'city': 'Bengaluru', 'zone_type': 'city', 'priority': 10, 'geo_node': 'IN-KA-BLR',
            'polygon_geojson': BLR, 'reason': 'Launch Bengaluru'}
    body.update(over)
    return body


def approve(request_id):
    chk, _ = checker()
    r = approve_request(chk, request_id)
    assert r.status_code == 200, r.content[:400]
    return r


def request_id_of(response):
    return body_of(response)['approval_request']['id']


def create_zone(world, **over):
    """Propose and approve a zone; returns the ServiceZone."""
    maker, _ = world.super
    r = post(maker, f'{ZONES}/', new_body(**over), expect=202)
    approve(request_id_of(r))
    return ServiceZone.objects.get(code=over.get('code', 'IN-KA-BLR'))


def codes(response):
    return response.json()['error']['code']


# ===========================================================================
# GEOMETRY (server-side validation)
# ===========================================================================

class TestGeometry:
    def test_a_valid_polygon_and_a_multipolygon_pass(self):
        geojson, g, area = geom.validate(BLR)
        assert geojson['type'] == 'Polygon' and 200 < area < 600
        multi = {'type': 'MultiPolygon', 'coordinates': [BLR['coordinates'],
                                                         poly(77.9, 12.9, 78.0, 13.0)['coordinates']]}
        assert geom.validate(multi)[0]['type'] == 'MultiPolygon'

    def test_a_bare_ring_is_normalised_to_a_polygon(self):
        assert geom.validate(ring(77.5, 12.9, 77.7, 13.05))[0]['type'] == 'Polygon'

    @pytest.mark.parametrize('bad, code', [
        ({'type': 'Polygon', 'coordinates': [[[77.5, 12.9], [77.7, 12.9], [77.5, 12.9]]]},
         'RING_TOO_SHORT'),
        ({'type': 'Polygon', 'coordinates': [[[77.5, 12.9], [77.7, 12.9], [77.7, 13.0],
                                              [77.5, 13.0]]]}, 'RING_NOT_CLOSED'),
        ({'type': 'Polygon', 'coordinates': [[[77.5, 12.9], [77.7, 13.0], [77.7, 12.9],
                                              [77.5, 13.0], [77.5, 12.9]]]}, 'SELF_INTERSECTION'),
        (poly(77.5, 12.9, 200.0, 13.0), 'COORDINATE_OUT_OF_RANGE'),
        (poly(77.5, 12.9, 77.7, 95.0), 'COORDINATE_OUT_OF_RANGE'),
        ({'type': 'Point', 'coordinates': [77.5, 12.9]}, 'POLYGON_INVALID'),
        ('nonsense', 'POLYGON_INVALID'),
    ])
    def test_invalid_shapes_are_refused_with_a_stable_code(self, bad, code):
        with pytest.raises(geom.GeometryError) as exc:
            geom.validate(bad)
        assert exc.value.code == code

    def test_the_area_bounds_are_constants_and_enforced(self):
        assert (geom.MIN_AREA_KM2, geom.MAX_AREA_KM2) == (0.01, 20_000)
        with pytest.raises(geom.GeometryError) as small:
            geom.validate(poly(77.5, 12.9, 77.5001, 12.9001))        # ~ 0.0001 km2
        assert small.value.code == 'AREA_TOO_SMALL'
        with pytest.raises(geom.GeometryError) as big:
            geom.validate(poly(70.0, 10.0, 80.0, 20.0))               # ~ 1.2 million km2
        assert big.value.code == 'AREA_TOO_LARGE'

    def test_containment_and_overlap_tolerate_a_shared_border(self):
        a = geom.to_geometry(poly(77.5, 12.9, 77.6, 13.0))
        touching = geom.to_geometry(poly(77.6, 12.9, 77.7, 13.0))
        inside = geom.to_geometry(poly(77.52, 12.92, 77.58, 12.98))
        assert not geom.overlaps(a, touching)
        assert geom.overlaps(a, inside)
        assert geom.contains(a, inside) and not geom.contains(inside, a)
        assert geom.contains(a, a)

    def test_the_api_refuses_a_bad_polygon_before_any_request_exists(self, world):
        maker, _ = world.super
        before = ApprovalRequest.objects.count()
        r = post(maker, f'{ZONES}/', new_body(polygon_geojson={
            'type': 'Polygon', 'coordinates': [[[77.5, 12.9], [77.7, 13.0], [77.7, 12.9],
                                                [77.5, 13.0], [77.5, 12.9]]]}), expect=400)
        assert error_code(r) == 'SELF_INTERSECTION'
        assert ApprovalRequest.objects.count() == before


# ===========================================================================
# CREATE THROUGH MAKER-CHECKER, REVISIONS, AUDIT
# ===========================================================================

class TestCreate:
    def test_create_is_a_202_and_writes_nothing_until_a_different_admin_approves(self, world):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/', new_body(), expect=202)
        assert body_of(r)['state'] == 'pending_approval'
        assert body_of(r)['approval_request']['action_key'] == 'zone.change'
        assert not ServiceZone.objects.filter(code='IN-KA-BLR').exists()
        approve(request_id_of(r))
        zone = ServiceZone.objects.get(code='IN-KA-BLR')
        assert (zone.is_active, zone.country, zone.currency, zone.geo_node.code) == (
            True, 'IN', 'INR', 'IN-KA-BLR')

    def test_the_maker_cannot_approve_their_own_zone(self, world):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/', new_body(), expect=202)
        denied = approve_request(maker, request_id_of(r))
        assert denied.status_code in (403, 409)
        assert not ServiceZone.objects.filter(code='IN-KA-BLR').exists()

    def test_applying_writes_revision_one_with_maker_checker_reason_and_audit(self, world):
        maker, maker_user = world.super
        r = post(maker, f'{ZONES}/', new_body(reason='Launch Bengaluru in Q1'), expect=202)
        chk, chk_user = checker()
        assert approve_request(chk, request_id_of(r)).status_code == 200
        zone = ServiceZone.objects.get(code='IN-KA-BLR')
        rev = zone.revisions.get()
        assert (rev.version, rev.kind) == (1, 'create')
        assert rev.maker_id == maker_user.id and rev.checker_id == chk_user.id
        assert rev.reason == 'Launch Bengaluru in Q1' and rev.applied_at is not None
        assert rev.snapshot['polygon_geojson']['type'] == 'Polygon'
        assert rev.snapshot['code'] == 'IN-KA-BLR' and rev.geo_node_code == 'IN-KA-BLR'
        row = AdminAuditLog.objects.filter(action='service_zone_created').latest('id')
        assert row.actor_id == chk_user.id and row.target_id == str(zone.id)

    def test_a_reason_is_required(self, world):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/', new_body(reason=''), expect=400)
        assert error_code(r) == 'REASON_REQUIRED'

    def test_a_duplicate_code_is_refused(self, world):
        create_zone(world)
        maker, _ = world.super
        r = post(maker, f'{ZONES}/', new_body(polygon_geojson=poly(78.9, 12.9, 79.0, 13.0)),
                 expect=409)
        assert error_code(r) == 'ZONE_EXISTS'

    def test_a_revision_cannot_be_edited_or_deleted(self, world):
        rev = create_zone(world).revisions.get()
        rev.reason = 'rewritten'
        with pytest.raises(PermissionError):
            rev.save()
        with pytest.raises(PermissionError):
            rev.delete()

    def test_the_new_zone_serves_pickups_once_applied(self, world):
        assert find_zone_for_point(12.97, 77.59) is None
        create_zone(world)
        assert find_zone_for_point(12.97, 77.59).code == 'IN-KA-BLR'


# ===========================================================================
# RULES THAT LOOK AT OTHER ZONES
# ===========================================================================

class TestRules:
    def test_the_country_is_required_and_must_match_the_geo_node(self, world):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/', new_body(country=''), expect=400)
        assert error_code(r) == 'FIELD_REQUIRED'
        r = post(maker, f'{ZONES}/', new_body(country='US', currency='USD',
                                              timezone_name='America/Chicago',
                                              distance_unit='mi',
                                              market_override_reason='n/a'), expect=400)
        assert error_code(r) == 'COUNTRY_MISMATCH'

    def test_a_zone_must_hang_from_a_city_node(self, world):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/', new_body(geo_node='IN-KA'), expect=400)
        assert error_code(r) == 'GEO_NODE_NOT_CITY'

    def test_market_fields_must_match_the_country_unless_overridden_with_a_reason(self, world):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/', new_body(currency='USD'), expect=400)
        assert error_code(r) == 'MARKET_MISMATCH'
        r = post(maker, f'{ZONES}/', new_body(distance_unit='mi'), expect=400)
        assert error_code(r) == 'MARKET_MISMATCH'
        r = post(maker, f'{ZONES}/', new_body(timezone_name='Asia/Tokyo'), expect=400)
        assert error_code(r) == 'MARKET_MISMATCH'
        r = post(maker, f'{ZONES}/', new_body(timezone_name='Mars/Olympus',
                                              market_override_reason='x'), expect=400)
        assert error_code(r) == 'INVALID_TIMEZONE'
        post(maker, f'{ZONES}/', new_body(timezone_name='Asia/Tokyo',
                                          market_override_reason='Test of an override'),
             expect=202)

    def test_blank_market_fields_take_the_country_defaults(self, world):
        zone = create_zone(world)
        assert (zone.currency, zone.timezone_name, zone.distance_unit) == (
            'INR', 'Asia/Kolkata', 'km')

    def test_two_active_city_zones_in_a_country_cannot_overlap(self, world):
        create_zone(world)
        maker, _ = world.super
        # An 'IN-KA-BLR2' overlapping Bengaluru, hung from the same node.
        r = post(maker, f'{ZONES}/', new_body(code='IN-KA-BLR2',
                                              polygon_geojson=poly(77.60, 12.95, 77.80, 13.10)),
                 expect=400)
        assert error_code(r) == 'CITY_ZONES_OVERLAP'
        # Sharing a border is fine.
        post(maker, f'{ZONES}/', new_body(code='IN-KA-BLR3',
                                          polygon_geojson=poly(77.70, 12.90, 77.90, 13.05)),
             expect=202)

    def test_a_child_zone_must_lie_inside_its_parent(self, world):
        maker, _ = world.super
        child = new_body(code='IN-TG-HYD-AIRPORT', geo_node='IN-TG-HYD', zone_type='subzone',
                         parent='IN-TG-HYD', priority=100, state_code='TG', city='Hyderabad',
                         polygon_geojson=AIRPORT)
        post(maker, f'{ZONES}/', child, expect=202)
        child['polygon_geojson'] = poly(79.4, 17.2, 79.5, 17.3)               # outside Hyderabad
        r = post(maker, f'{ZONES}/', child, expect=400)
        assert error_code(r) == 'CHILD_OUTSIDE_PARENT'

    def test_shrinking_a_parent_may_not_leave_a_child_outside(self, world):
        maker, _ = world.super
        child = new_body(code='IN-TG-HYD-AIRPORT', geo_node='IN-TG-HYD', zone_type='subzone',
                         parent='IN-TG-HYD', priority=100, state_code='TG', city='Hyderabad',
                         polygon_geojson=AIRPORT)
        approve(request_id_of(post(maker, f'{ZONES}/', child, expect=202)))
        r = post_patch(maker, world.hyd.id, {'polygon_geojson': poly(78.5, 17.4, 78.6, 17.5),
                                             'reason': 'Shrink'}, expect=400)
        assert error_code(r) == 'CHILD_OUTSIDE_PARENT'

    def test_code_and_geo_node_cannot_change(self, world):
        maker, _ = world.super
        r = post_patch(maker, world.hyd.id, {'code': 'IN-TG-HYD2', 'reason': 'x'}, expect=400)
        assert error_code(r) == 'CODE_IMMUTABLE'
        r = post_patch(maker, world.hyd.id, {'geo_node': 'IN-TG-WGL', 'reason': 'x'}, expect=400)
        assert error_code(r) == 'GEO_NODE_IMMUTABLE'

    def test_a_per_trip_fee_needs_its_label(self, world):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/', new_body(per_trip_fee='2.50'), expect=400)
        assert error_code(r) == 'FIELD_REQUIRED'


def post_patch(client, zone_id, body, expect=None):
    response = client.patch(f'{ZONES}/{zone_id}/', body, format='json')
    if expect is not None:
        assert response.status_code == expect, (response.status_code, response.content[:500])
    return response


# ===========================================================================
# UPDATE, HISTORY, RESTORE
# ===========================================================================

class TestUpdateHistoryRestore:
    def test_a_polygon_change_is_a_request_then_a_new_revision(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        bigger = poly(77.45, 12.88, 77.72, 13.07)
        r = post_patch(maker, zone.id, {'polygon_geojson': bigger, 'priority': 20,
                                        'reason': 'Take in the new tech park'}, expect=202)
        zone.refresh_from_db()
        assert zone.priority == 10 and zone.revisions.count() == 1          # nothing yet
        approve(request_id_of(r))
        zone.refresh_from_db()
        assert zone.priority == 20 and zone.polygon_geojson['coordinates'][0][0] == [77.45, 12.88]
        assert list(zone.revisions.order_by('version').values_list('version', 'kind')) == [
            (1, 'create'), (2, 'update')]

    def test_a_change_with_nothing_different_is_refused(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        r = post_patch(maker, zone.id, {'name': zone.name, 'reason': 'noop'}, expect=400)
        assert error_code(r) == 'NO_CHANGE'

    def test_history_lists_revisions_and_pending_requests(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        post_patch(maker, zone.id, {'name': 'Bengaluru Metro', 'reason': 'Rename'}, expect=202)
        data = get(maker, f'{ZONES}/{zone.id}/history/')
        assert data['version'] == 1 and len(data['revisions']) == 1
        assert data['revisions'][0]['kind'] == 'create' and 'polygon_geojson' not in \
            data['revisions'][0]['snapshot']
        assert len(data['pending_requests']) == 1
        with_polygons = get(maker, f'{ZONES}/{zone.id}/history/?polygons=1')
        assert 'polygon_geojson' in with_polygons['revisions'][0]['snapshot']
        listing = get(maker, f'{ZONES}/pending/')
        assert any(row['target_id'] == str(zone.id) for row in listing['results'])

    def test_restore_is_a_normal_request_that_puts_the_revision_back(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        original = copy.deepcopy(zone.polygon_geojson)
        approve(request_id_of(post_patch(
            maker, zone.id, {'polygon_geojson': poly(77.45, 12.88, 77.72, 13.07),
                             'reason': 'Bigger'}, expect=202)))
        zone.refresh_from_db()
        assert zone.polygon_geojson != original
        r = post(maker, f'{ZONES}/{zone.id}/restore/', {'version': 1, 'reason': 'Back out'},
                 expect=202)
        zone.refresh_from_db()
        assert zone.polygon_geojson != original                               # still waiting
        approve(request_id_of(r))
        zone.refresh_from_db()
        assert zone.polygon_geojson == original
        last = zone.revisions.order_by('-version').first()
        assert (last.version, last.kind, last.restored_from_version) == (3, 'restore', 1)

    def test_restoring_a_missing_revision_or_the_current_state_is_refused(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        r = post(maker, f'{ZONES}/{zone.id}/restore/', {'version': 9, 'reason': 'x'}, expect=400)
        assert error_code(r) == 'REVISION_NOT_FOUND'
        r = post(maker, f'{ZONES}/{zone.id}/restore/', {'version': 1, 'reason': 'x'}, expect=400)
        assert error_code(r) == 'NO_CHANGE'

    def test_a_stale_request_is_refused_at_approval_not_applied(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        a = post_patch(maker, zone.id, {'name': 'Name A', 'reason': 'A'}, expect=202)
        b = post_patch(maker, zone.id, {'name': 'Name B', 'reason': 'B'}, expect=202)
        approve(request_id_of(a))
        chk, _ = checker()
        refused = approve_request(chk, request_id_of(b))
        zone.refresh_from_db()
        assert zone.name == 'Name A'
        assert refused.status_code == 200, refused.content[:300]     # the decision is recorded
        row = ApprovalRequest.objects.get(pk=request_id_of(b))
        assert row.status == 'failed' and row.error_code == 'ZONE_CHANGED'

    def test_a_zone_with_no_revision_gets_a_baseline_before_its_first_change(self, world):
        zone = ServiceZone.objects.create(
            code='IN-KA-BLR', name='Bengaluru', country='IN', zone_type='city',
            polygon_geojson=BLR, geo_node=world.geo['IN-KA-BLR'], is_active=True)
        assert not zone.revisions.exists()
        maker, _ = world.super
        approve(request_id_of(post_patch(maker, zone.id, {'name': 'Metro', 'reason': 'r'},
                                         expect=202)))
        assert list(zone.revisions.order_by('version').values_list('version', 'kind')) == [
            (1, 'backfill'), (2, 'update')]


# ===========================================================================
# DEACTIVATION AND READINESS
# ===========================================================================

def trip_at(zone, lat, lon, status='in_progress', vt=None, driver=None, days_ago=1):
    rider = make_rider(f'r{Trip.objects.count()}', '91')
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, zone=zone, status_id=status_obj(status),
        pickup_lat=Decimal(str(lat)), pickup_long=Decimal(str(lon)),
        destination_lat=Decimal('17.24'), destination_long=Decimal('78.42'),
        requested_vehicle_type=vt, estimated_fare=Decimal('120.00'), final_fare=Decimal('120.00'),
        payment_method='online', payment_status='completed')
    Trip.objects.filter(pk=trip.pk).update(requested_at=timezone.now() - timedelta(days=days_ago))
    return trip


class TestDeactivation:
    def test_deactivation_is_refused_while_a_trip_is_in_progress(self, world):
        zone = create_zone(world)
        # a second zone so the city is not left with none
        trip_at(zone, 12.97, 77.59, status='requested')
        maker, _ = world.super
        r = post(maker, f'{ZONES}/{zone.id}/deactivate/', {'reason': 'Retire'}, expect=409)
        assert error_code(r) == 'TRIPS_IN_PROGRESS'

    @pytest.mark.parametrize('state', ['requested', 'accepted', 'reached', 'in_progress'])
    def test_every_live_state_blocks_it(self, world, state):
        zone = create_zone(world)
        trip_at(zone, 12.97, 77.59, status=state)
        maker, _ = world.super
        post(maker, f'{ZONES}/{zone.id}/deactivate/', {'reason': 'Retire'}, expect=409)

    def test_a_finished_trip_does_not_block_and_the_request_deactivates(self, world):
        zone = create_zone(world)
        trip_at(zone, 12.97, 77.59, status='completed')
        maker, _ = world.super
        r = post(maker, f'{ZONES}/{zone.id}/deactivate/', {'reason': 'Retire'}, expect=202)
        approve(request_id_of(r))
        zone.refresh_from_db()
        assert zone.is_active is False
        assert zone.revisions.order_by('-version').first().kind == 'deactivate'
        assert find_zone_for_point(12.97, 77.59) is None

    def test_a_trip_that_started_between_request_and_approval_blocks_the_approval(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        r = post(maker, f'{ZONES}/{zone.id}/deactivate/', {'reason': 'Retire'}, expect=202)
        trip_at(zone, 12.97, 77.59, status='accepted')
        chk, _ = checker()
        approve_request(chk, request_id_of(r))
        zone.refresh_from_db()
        assert zone.is_active is True
        assert ApprovalRequest.objects.get(pk=request_id_of(r)).error_code == 'TRIPS_IN_PROGRESS'

    def test_delete_is_a_deactivation_request_and_the_zone_is_never_deleted(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        r = maker.delete(f'{ZONES}/{zone.id}/', {'reason': 'Retire'}, format='json')
        assert r.status_code == 202
        approve(request_id_of(r))
        assert ServiceZone.objects.filter(pk=zone.pk, is_active=False).exists()

    def test_patching_is_active_false_is_a_deactivation_request(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        r = post_patch(maker, zone.id, {'is_active': False, 'reason': 'Retire'}, expect=202)
        assert body_of(r)['approval_request']['payload']['kind'] == 'deactivate'

    def test_reactivation_is_a_request_and_checks_overlap_again(self, world):
        zone = create_zone(world)
        maker, _ = world.super
        approve(request_id_of(post(maker, f'{ZONES}/{zone.id}/deactivate/',
                                   {'reason': 'Retire'}, expect=202)))
        # Meanwhile another active city zone takes the same ground.
        ServiceZone.objects.create(
            code='IN-KA-OTHER', name='Other', country='IN', zone_type='city',
            polygon_geojson=BLR, geo_node=world.geo['IN-KA-BLR'], is_active=True)
        r = post(maker, f'{ZONES}/{zone.id}/reactivate/', {'reason': 'Back'}, expect=400)
        assert error_code(r) == 'CITY_ZONES_OVERLAP'

    def test_a_live_city_keeps_at_least_one_active_zone(self, world):
        maker, _ = world.super
        node = world.geo['IN-TG-HYD']
        assert node.status == 'active'
        r = post(maker, f'{ZONES}/{world.hyd.id}/deactivate/', {'reason': 'Retire'}, expect=409)
        assert error_code(r) == 'CITY_WOULD_HAVE_NO_ZONE'

    def test_readiness_has_a_zone_item(self, world):
        from servers.admin_access.readiness import city_readiness

        node = world.geo['IN-KA-BLR']
        item = {i['key']: i for i in city_readiness(node)}['zone']
        assert item['ok'] is False and 'no active service zone' in item['detail']
        create_zone(world)
        item = {i['key']: i for i in city_readiness(node)}['zone']
        assert item['ok'] is True
        ServiceZone.objects.filter(code='IN-KA-BLR').update(is_active=False)
        assert {i['key']: i for i in city_readiness(node)}['zone']['ok'] is False


# ===========================================================================
# NO LEGACY POLYGON
# ===========================================================================

class TestNoLegacyFallback:
    def test_with_no_active_zone_there_is_no_service(self, world):
        from base.service_area import (
            NO_ACTIVE_ZONE, OUT_OF_SERVICE_AREA, is_inside_service_area,
            validate_service_area_detail,
        )

        assert is_inside_service_area(17.385, 78.486) is True
        ServiceZone.objects.update(is_active=False)
        assert is_inside_service_area(17.385, 78.486) is False
        ok, code, message = validate_service_area_detail(17.385, 78.486, 17.40, 78.50)
        assert (ok, code) == (False, NO_ACTIVE_ZONE) and 'no service zone' in message
        ServiceZone.objects.filter(code='IN-TG-HYD').update(is_active=True)
        ok, code, _ = validate_service_area_detail(17.385, 78.486, 12.97, 77.59)
        assert (ok, code) == (False, OUT_OF_SERVICE_AREA)

    def test_the_source_has_no_polygon_literal_and_no_override_setting(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / 'base' / 'service_area.py').read_text()
        assert '_HYDERABAD_POLYGON' not in text and 'SERVICE_AREA_OVERRIDE' not in text
        assert '78.205' not in text

    def test_the_rest_endpoint_answers_with_the_clear_code(self, world):
        rider, _ = world.super  # any authenticated operator can call estimate for the check
        ServiceZone.objects.update(is_active=False)
        r = rider.post('/api/v1/ride/estimate-fare/', {
            'pickup_lat': 17.385, 'pickup_long': 78.486, 'destination_lat': 17.41,
            'destination_long': 78.43, 'distance_km': 5, 'duration_min': 15,
            'vehicle_type': 'sedan'}, format='json')
        assert r.status_code == 400 and codes(r) == 'NO_ACTIVE_ZONE'

    def test_the_four_qa_zones_are_active_in_the_seed_migrations(self, db):
        active = set(ServiceZone.objects.filter(is_active=True, country='IN')
                     .values_list('code', flat=True))
        assert {'IN-TG-HYD', 'IN-AP-VJA', 'IN-TG-WGL', 'IN-AP-VTZ'} <= active


# ===========================================================================
# SCOPE AND CAPABILITIES
# ===========================================================================

class TestScope:
    @pytest.fixture
    def austin(self, world):
        return ServiceZone.objects.create(
            code='US-TX-AUS', name='Austin', country='US', state_code='TX', city='Austin',
            zone_type='city', polygon_geojson=poly(-97.95, 30.15, -97.55, 30.50),
            geo_node=world.geo['US-TX-AUS'], currency='USD', timezone_name='America/Chicago',
            distance_unit='mi', is_active=True)

    def test_city_ops_sees_only_its_own_zones(self, world, austin):
        client, _ = world.ops
        listing = client.get(f'{ZONES}/').json()
        rows = listing['results'] if isinstance(listing, dict) and 'results' in listing else listing
        seen = {row['code'] for row in rows}
        assert 'IN-TG-HYD' in seen and 'US-TX-AUS' not in seen

    def test_the_cities_list_is_scoped_to_the_caller(self, world):
        ops = get(world.ops[0], f'{ZONES}/cities/')['results']
        assert {c['code'] for c in ops} == {'IN-TG-HYD'}
        assert ops[0] == {'code': 'IN-TG-HYD', 'name': 'Hyderabad', 'status': 'active',
                          'country': 'IN'}
        everything = {c['code'] for c in get(world.super[0], f'{ZONES}/cities/')['results']}
        assert {'IN-TG-HYD', 'US-TX-AUS', 'IN-KA-BLR'} <= everything

    def test_a_zone_outside_scope_is_404_for_every_verb(self, world, austin):
        client, _ = world.ops
        assert client.get(f'{ZONES}/{austin.id}/').status_code == 404
        assert client.get(f'{ZONES}/{austin.id}/history/').status_code == 404
        assert client.patch(f'{ZONES}/{austin.id}/', {'name': 'x', 'reason': 'r'},
                            format='json').status_code == 404
        assert client.delete(f'{ZONES}/{austin.id}/').status_code == 404
        assert client.post(f'{ZONES}/{austin.id}/restore/', {'version': 1, 'reason': 'r'},
                           format='json').status_code == 404
        assert client.post(f'{ZONES}/preview/', {'zone': austin.id, 'values': {}},
                           format='json').status_code == 404

    def test_a_zone_in_scope_without_zone_write_is_403(self, world):
        for who in (world.analyst, world.finance):
            client, _ = who
            assert client.get(f'{ZONES}/{world.hyd.id}/').status_code == 200
            assert client.patch(f'{ZONES}/{world.hyd.id}/', {'name': 'x', 'reason': 'r'},
                                format='json').status_code == 403
            assert client.post(f'{ZONES}/', new_body(), format='json').status_code == 403

    def test_creating_needs_a_city_node_in_scope(self, world):
        client, _ = world.ops
        r = post(client, f'{ZONES}/', new_body(), expect=404)       # Bengaluru is not City Ops'
        assert error_code(r) == 'NOT_FOUND'
        child = new_body(code='IN-TG-HYD-AIRPORT', geo_node='IN-TG-HYD', zone_type='subzone',
                         parent='IN-TG-HYD', priority=100, state_code='TG',
                         city='Hyderabad', polygon_geojson=AIRPORT)
        post(client, f'{ZONES}/', child, expect=202)

    def test_city_ops_drafts_in_its_city_and_cannot_approve_its_own_draft(self, world):
        client, _ = world.ops
        r = post_patch(client, world.hyd.id, {'priority': 11, 'reason': 'Tune'}, expect=202)
        denied = approve_request(client, request_id_of(r))
        assert denied.status_code in (403, 404)
        world.hyd.refresh_from_db()
        assert world.hyd.priority != 11

    def test_a_checker_needs_approval_check_and_zone_write(self, world):
        # Finance holds neither approval.check nor zone.write.
        maker, _ = world.ops
        r = post_patch(maker, world.hyd.id, {'priority': 12, 'reason': 'Tune'}, expect=202)
        fin, _ = world.finance
        assert approve_request(fin, request_id_of(r)).status_code in (403, 404)
        world.hyd.refresh_from_db()
        assert world.hyd.priority != 12
        approve(request_id_of(r))
        world.hyd.refresh_from_db()
        assert world.hyd.priority == 12

    def test_seed_policies_make_zone_change_always_a_checker_and_never_overwrite(self, world):
        for country in ('IN', 'US'):
            policy = ApprovalPolicy.objects.get(country=country, action_key='zone.change',
                                                is_active=True)
            assert policy.limit_kind == 'always'
        policy = ApprovalPolicy.objects.get(country='IN', action_key='zone.change', is_active=True)
        before = (policy.id, policy.version)
        call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
        policy.refresh_from_db()
        assert (policy.id, policy.version) == before
        assert ApprovalPolicy.objects.filter(country='IN', action_key='zone.change').count() == 1


# ===========================================================================
# IMPACT PREVIEW AND POINT TESTER
# ===========================================================================

class TestPreviewAndTester:
    @pytest.fixture
    def airport(self, world):
        """Hyderabad plus a priority-100 airport sub-zone with its own fare card."""
        maker, _ = world.super
        child = new_body(code='IN-TG-HYD-AIRPORT', geo_node='IN-TG-HYD', zone_type='subzone',
                         parent='IN-TG-HYD', priority=100, state_code='TG', city='Hyderabad',
                         polygon_geojson=AIRPORT)
        approve(request_id_of(post(maker, f'{ZONES}/', child, expect=202)))
        zone = ServiceZone.objects.get(code='IN-TG-HYD-AIRPORT')
        RateCard.objects.filter(zone=world.hyd).delete()
        values = dict(base_fare=Decimal('30'), per_km_fare=Decimal('12'),
                      per_min_fare=Decimal('1.5'), min_fare=Decimal('40'),
                      night_surge_multiplier=Decimal('1.25'),
                      surge_cap_multiplier=Decimal('1.50'))
        RateCard.objects.create(zone=world.hyd, vehicle_type=world.sedan,
                                effective_from=LONG_AGO, **values)
        RateCard.objects.create(zone=zone, vehicle_type=world.sedan, effective_from=LONG_AGO,
                                **{**values, 'base_fare': Decimal('60')})
        return zone

    def test_an_invalid_draft_answers_200_with_valid_false_and_the_code(self, world):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/preview/', {'values': new_body(polygon_geojson={
            'type': 'Polygon', 'coordinates': [[[77.5, 12.9], [77.7, 13.0], [77.7, 12.9],
                                                [77.5, 13.0], [77.5, 12.9]]]})}, expect=200)
        assert body_of(r)['valid'] is False and body_of(r)['error']['code'] == 'SELF_INTERSECTION'

    def test_a_new_zone_preview_counts_nothing_moving_when_no_trips_exist(self, world):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/preview/', {'values': new_body()}, expect=200)
        data = body_of(r)
        assert data['valid'] and data['is_new'] and data['trips_moved'] == 0
        assert data['summary'][0].startswith('Creates zone IN-KA-BLR')

    def test_growing_the_airport_moves_pickups_and_counts_them_by_zone(self, world, airport):
        driver = make_driver(world.geo, 'IN-TG-HYD', 'a', currency='INR', calling='91')
        # Two pickups just outside the airport box (currently Hyderabad), one inside, one old.
        trip_at(world.hyd, 17.30, 78.43, status='completed', vt=world.sedan, driver=driver)
        trip_at(world.hyd, 17.31, 78.44, status='completed', vt=world.sedan, driver=driver)
        trip_at(airport, 17.24, 78.43, status='completed', vt=world.sedan, driver=driver)
        trip_at(world.hyd, 17.30, 78.43, status='completed', vt=world.sedan, days_ago=45)
        maker, _ = world.super
        bigger = poly(78.40, 17.21, 78.46, 17.35)
        r = post(maker, f'{ZONES}/preview/', {
            'zone': airport.id, 'values': {'polygon_geojson': bigger}}, expect=200)
        data = body_of(r)
        assert data['valid'] is True
        assert data['trips_moved'] == 2 and data['moved_into_zone'] == 2
        assert data['moved_out_of_zone'] == 0 and data['trips_unchanged'] == 1
        assert data['transitions'] == [{'from': 'IN-TG-HYD', 'to': 'IN-TG-HYD-AIRPORT',
                                        'trips': 2}]
        assert data['window_days'] == 30 and data['area_km2'] > data['old_area_km2']
        # the airport card (base 60) would newly apply to those two pickups
        assert [(c['zone'], c['trips']) for c in data['cards_newly_applying']] == [
            ('IN-TG-HYD-AIRPORT', 2)]
        assert any('would change zone' in line for line in data['summary'])

    def test_drivers_whose_recent_pickups_would_change_city_are_counted_only(self, world, airport):
        driver = make_driver(world.geo, 'IN-TG-HYD', 'b', currency='INR', calling='91')
        other = make_driver(world.geo, 'IN-TG-HYD', 'c', currency='INR', calling='91')
        for _ in range(3):
            trip_at(world.hyd, 17.30, 78.43, status='completed', vt=world.sedan, driver=driver)
        trip_at(world.hyd, 17.45, 78.55, status='completed', vt=world.sedan, driver=other)
        maker, _ = world.super
        # A sub-zone hung from ANOTHER city's node, drawn over the first driver's pickups.
        draft = new_body(code='IN-KA-BLR-X', zone_type='subzone', priority=100,
                         polygon_geojson=poly(78.40, 17.25, 78.46, 17.35))
        data = body_of(post(maker, f'{ZONES}/preview/', {'values': draft}, expect=200))
        assert data['valid'] and data['trips_moved'] == 3
        assert data['drivers_whose_city_would_change'] == 1          # a count, never ids
        assert 'drivers' not in data and 'driver_ids' not in data
        assert any('different city' in line for line in data['summary'])
        # Within one city (the airport growing) no driver changes city.
        data = body_of(post(maker, f'{ZONES}/preview/', {
            'zone': airport.id, 'values': {'polygon_geojson': poly(78.40, 17.21, 78.46, 17.35)}},
            expect=200))
        assert data['drivers_whose_city_would_change'] == 0

    def test_a_moved_pickup_with_no_card_in_the_new_zone_is_called_out(self, world, airport):
        trip_at(world.hyd, 17.30, 78.43, status='completed', vt=world.sedan)
        trip_at(world.hyd, 17.31, 78.43, status='completed', vt=world.sedan)
        maker, _ = world.super
        draft = new_body(code='IN-KA-BLR-X', zone_type='subzone', priority=100,
                         polygon_geojson=poly(78.40, 17.25, 78.46, 17.35))
        data = body_of(post(maker, f'{ZONES}/preview/', {'values': draft}, expect=200))
        assert data['trips_moved'] == 2
        assert data['pickups_without_a_card'] == [
            {'zone': 'IN-KA-BLR-X', 'vehicle_type': world.sedan.id,
             'vehicle_type_name': 'sedan', 'trips': 2}]
        assert any(line.startswith('WARNING') for line in data['summary'])

    def test_taking_a_zone_out_of_service_moves_its_pickups_to_the_next_zone(self, world, airport):
        trip_at(airport, 17.24, 78.43, status='completed', vt=world.sedan)
        maker, _ = world.super
        data = body_of(post(maker, f'{ZONES}/preview/', {
            'zone': airport.id, 'is_active': False, 'values': {}}, expect=200))
        assert data['transitions'] == [{'from': 'IN-TG-HYD-AIRPORT', 'to': 'IN-TG-HYD',
                                        'trips': 1}]
        assert data['summary'][0].startswith('Takes IN-TG-HYD-AIRPORT out of service')

    def test_the_point_tester_uses_the_function_quoting_uses(self, world, airport):
        maker, _ = world.super
        for lat, lon in ((17.24, 78.43), (17.40, 78.48), (12.97, 77.59)):
            data = body_of(post(maker, f'{ZONES}/point-test/', {'lat': lat, 'lng': lon},
                                expect=200))
            expected = find_zone_for_point(lat, lon)
            assert (data['zone']['code'] if data['zone'] else None) == (
                expected.code if expected else None)
            assert data['in_service_area'] is (expected is not None)
        inside = body_of(post(maker, f'{ZONES}/point-test/', {'lat': 17.24, 'lng': 78.43},
                              expect=200))
        card = next(c for c in inside['rate_cards'] if c['vehicle_type_name'] == 'sedan')['card']
        assert card['zone'] == 'IN-TG-HYD-AIRPORT' and card['base_fare'] == '60.00'

    def test_the_point_tester_resolves_the_draft_polygon_with_the_same_function(self, world, airport):
        maker, _ = world.super
        r = post(maker, f'{ZONES}/point-test/', {
            'lat': 17.30, 'lng': 78.43, 'zone': airport.id,
            'values': {'polygon_geojson': poly(78.40, 17.21, 78.46, 17.35)}}, expect=200)
        data = body_of(r)
        assert data['zone']['code'] == 'IN-TG-HYD'
        assert data['draft']['zone']['code'] == 'IN-TG-HYD-AIRPORT'
        assert data['draft']['changes_resolution'] is True
        # and it is exactly ZoneIndex with the overlay: nothing special-cased
        from servers.pricing import zone_impact
        from servers.pricing import zone_workflow as wf

        values, g, _a, _n = wf.clean_values(
            {'polygon_geojson': poly(78.40, 17.21, 78.46, 17.35), 'market_override_reason': 'p'},
            wf.zone_values(airport))
        overlay = zone_impact.draft_overlay(airport, values, g)
        assert ZoneIndex.load(overlay).resolve(17.30, 78.43).code == 'IN-TG-HYD-AIRPORT'
        assert ZoneIndex.load().resolve(17.30, 78.43).code == 'IN-TG-HYD'

    def test_a_point_outside_every_zone_says_so_and_a_bad_point_is_400(self, world):
        maker, _ = world.super
        data = body_of(post(maker, f'{ZONES}/point-test/', {'lat': 12.97, 'lng': 77.59},
                            expect=200))
        assert data['in_service_area'] is False and data['zone'] is None
        post(maker, f'{ZONES}/point-test/', {'lat': 'x', 'lng': 1}, expect=400)
        post(maker, f'{ZONES}/point-test/', {'lat': 95, 'lng': 1}, expect=400)

    def test_resolution_is_deterministic_for_equal_priority(self, world):
        a = ServiceZone.objects.create(code='AAA-TIE', name='A', country='IN', zone_type='city',
                                       polygon_geojson=BLR, priority=10, is_active=True)
        ServiceZone.objects.create(code='ZZZ-TIE', name='Z', country='IN', zone_type='city',
                                   polygon_geojson=BLR, priority=10, is_active=True)
        assert find_zone_for_point(12.97, 77.59).code == a.code


# ===========================================================================
# BACKFILL
# ===========================================================================

class TestBackfill:
    def test_every_zone_gets_version_one_from_system_migration(self, world):
        import importlib

        from django.apps import apps

        ServiceZoneRevision.objects.all().delete() if False else None
        # The migration already ran when the test DB was built. Simulate a zone that
        # predates it, then run the migration's own function.
        zone = ServiceZone.objects.create(
            code='IN-OLD', name='Old', country='IN', zone_type='city',
            polygon_geojson=BLR, is_active=True)
        mig = importlib.import_module('servers.pricing.migrations.0017_backfill_zone_revisions')
        mig.backfill(apps, None)
        mig.backfill(apps, None)                                  # idempotent
        rev = zone.revisions.get()
        assert (rev.version, rev.kind, rev.maker_label) == (1, 'backfill', 'system:migration')
        assert rev.snapshot['polygon_geojson'] == BLR
        assert not ServiceZone.objects.filter(revisions__isnull=True).exists()
