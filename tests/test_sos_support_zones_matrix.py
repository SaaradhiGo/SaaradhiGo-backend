"""The role x capability matrix for SOS, support tickets, zones and rate cards.

Every row of the matrix the owner ruled on (2026-10-28), as a test, for
each of the eight seeded roles, over real HTTP with a JWT. The matrix:

  endpoint                          cap              SA  CityOps Support Safety KYC Fin Mkt Analyst
  GET  /sos/admin/  (+ detail)      safety.respond    G    -       -       R     -   -   -    -
  POST /sos/admin/<id>/ack|res|fa/  safety.respond    G    -       -       W     -   -   -    -
  POST /sos/admin/<id>/reveal/      safety.respond    G    -       -       R*    -   -   -    -   (*active only)
  GET  /support/admin/tickets/      support.read      G    R       R       R     -   R   -    -
  POST .../tickets/<id>/reply|assign support.reply    G    -       W       -     -   -   -    -
  GET  /pricing/admin/zones/        zone.read         G    R       -       -     -   R   -    R
  GET  /pricing/admin/rate-cards/   zone.read         G    R       -       -     -   R   -    R
  writes on zones / rate cards      pricing.write     G    -       -       -     -   -   -    -

  G = read/write, global. R/W = within geo scope. - = 403.
  An object outside the operator's scope is 404, never 403.

Scope keys: SOS event -> its anchor (trip's zone, else GPS -> city, else a
driver's home city; an UNANCHORED event reaches every Safety Officer);
ticket -> the trip's zone, else the requester's home city if a driver;
zone -> its `geo_node`; rate card -> its zone's `geo_node`.

Every operator here is assigned at Hyderabad (`IN-TG-HYD`); Super Admin
at Global because the role is global-only. "Another city" is Austin.

Contact details on an SOS are masked for EVERYBODY; the reveal path is
the only way to the number and writes an `sos_contact_revealed` row.
"""

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.admin_access.models import GLOBAL, AdminAssignment, AdminRole, GeoNode
from servers.admin_access.roles import ROLE_TEMPLATES, SUPER_ADMIN
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import Driver, VehicleType
from servers.pricing.models import RateCard, ServiceZone
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider
from servers.sos.models import SOSEvent
from servers.sos.views import CONTACT_REVEALED_ACTION
from servers.support.models import SupportTicket

pytestmark = pytest.mark.django_db

User = get_user_model()

ROLES = [t['code'] for t in ROLE_TEMPLATES]
assert len(ROLES) == 8

# --- the matrix, as data ----------------------------------------------------

SOS_ROLES = {SUPER_ADMIN, 'safety_officer'}
TICKET_READ_ROLES = {SUPER_ADMIN, 'city_ops_manager', 'support_agent', 'safety_officer', 'finance'}
TICKET_WRITE_ROLES = {SUPER_ADMIN, 'support_agent'}
ZONE_READ_ROLES = {SUPER_ADMIN, 'city_ops_manager', 'finance', 'analyst'}
PRICING_WRITE_ROLES = {SUPER_ADMIN}

TARGET_STATUS = {'acknowledge': 'acknowledged', 'resolve': 'resolved', 'false-alarm': 'false_alarm'}

HYD_RIDER_PHONE = '+919876500001'
HYD_RIDER_NAME = 'Ravi Kumar'
HYD_RIDER_EMERGENCY = '+919876500099'
AUS_RIDER_PHONE = '+15125550001'

POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[78.30, 17.40], [78.50, 17.40], [78.50, 17.50],
                     [78.30, 17.50], [78.30, 17.40]]],
}

_n = iter(range(1, 9999))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


def _zone(code, node, **extra):
    zone, _ = ServiceZone.objects.get_or_create(
        code=code,
        defaults={
            'name': node.name, 'zone_type': 'city', 'city': node.name,
            'polygon_geojson': POLYGON, 'is_active': True, **extra,
        },
    )
    if zone.geo_node_id != node.id:
        zone.geo_node = node
        zone.save()
    return zone


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _rider(phone, name, **extra):
    user = User.objects.create_user(
        phone_number=phone, username=phone, full_name=name, role='rider', **extra,
    )
    Rider.objects.create(user_id=user)
    return user


def _driver(phone, name, city):
    user = User.objects.create_user(
        phone_number=phone, username=phone, full_name=name, role='driver',
    )
    return Driver.objects.create(user_id=user, home_city=city)


def _trip(rider, zone, driver=None):
    return Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=_status('in_progress'), zone=zone,
        pickup_lat=Decimal('17.4'), pickup_long=Decimal('78.4'),
        destination_lat=Decimal('17.5'), destination_long=Decimal('78.5'),
    )


def _sos(user, trip=None, status='open'):
    return SOSEvent.objects.create(
        user=user, user_label=user.full_name or user.phone_number,
        initiated_by=user.role, trip=trip, event_type='panic', status=status,
    )


def _ticket(user, trip=None):
    return SupportTicket.objects.create(
        user_id=user, trip_id=trip, issue_type='other', description='help',
    )


def _card(zone, vt, **extra):
    return RateCard.objects.create(
        zone=zone, vehicle_type=vt,
        base_fare=Decimal('30.00'), per_km_fare=Decimal('12.00'),
        per_min_fare=Decimal('2.00'), min_fare=Decimal('50.00'),
        commission_percent=Decimal('18.00'), gst_percent=Decimal('5.00'),
        surge_cap_multiplier=Decimal('1.50'), **extra,
    )


@pytest.fixture
def world(geo):
    """One of everything in Hyderabad, one of everything in Austin.

    Plus the two edge shapes the scope keys have to handle: a DRIVER's SOS
    and ticket raised outside any trip (anchored by home city) and a
    RIDER's raised outside any trip (no anchor at all: the ticket is Global
    only; the SOS is unanchored and reaches every Safety Officer).
    """
    hyd_zone = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus_zone = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX')
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')

    hyd_rider = _rider(HYD_RIDER_PHONE, HYD_RIDER_NAME, emergency_contact=HYD_RIDER_EMERGENCY)
    aus_rider = _rider(AUS_RIDER_PHONE, 'Austin Rider')
    hyd_driver = _driver('+919876500002', 'Suresh Reddy', geo['IN-TG-HYD'])
    aus_driver = _driver('+15125550002', 'Austin Driver', geo['US-TX-AUS'])

    hyd_trip = _trip(hyd_rider, hyd_zone, hyd_driver)
    aus_trip = _trip(aus_rider, aus_zone, aus_driver)

    return {
        'hyd_zone': hyd_zone, 'aus_zone': aus_zone, 'sedan': sedan,
        'hyd_rider': hyd_rider, 'aus_rider': aus_rider,
        'hyd_driver': hyd_driver, 'aus_driver': aus_driver,
        'hyd_trip': hyd_trip, 'aus_trip': aus_trip,

        'hyd_event': _sos(hyd_rider, hyd_trip),
        'aus_event': _sos(aus_rider, aus_trip),
        'driver_event': _sos(hyd_driver.user_id),             # no trip: home city
        'rider_offtrip_event': _sos(hyd_rider),               # no trip, no anchor
        'resolved_event': _sos(hyd_rider, hyd_trip, status='resolved'),
        'acknowledged_event': _sos(hyd_rider, hyd_trip, status='acknowledged'),

        'hyd_ticket': _ticket(hyd_rider, hyd_trip),
        'aus_ticket': _ticket(aus_rider, aus_trip),
        'driver_ticket': _ticket(hyd_driver.user_id),
        'rider_offtrip_ticket': _ticket(hyd_rider),

        'hyd_card': _card(hyd_zone, sedan),
        'aus_card': _card(aus_zone, sedan),
    }


def _client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


def operator(role_code, geo, scope_code='IN-TG-HYD'):
    """An operator holding `role_code` at Hyderabad (Global for Super Admin)."""
    i = next(_n)
    phone = f'+9198000{i:05d}'
    user = User.objects.create_user(
        phone_number=phone, username=phone, email=f'op{i}@saaradhigo.test',
        full_name=f'Operator {role_code}', role='admin', is_staff=True,
    )
    role = AdminRole.objects.get(code=role_code)
    scope = geo['GLOBAL'] if role.is_global_only else geo[scope_code]
    AdminAssignment.objects.create(admin=user, role=role, scope=scope)
    return _client_for(user), user


def body_of(response):
    return json.dumps(response.json())


def ids_in(response):
    data = response.json().get('data', response.json())
    results = data.get('results', data)
    if isinstance(results, dict):
        results = results.get('results', [])
    return {row['id'] for row in results}


def _zone_payload(code):
    return {
        'code': code, 'name': code, 'zone_type': 'city', 'city': 'Hyderabad',
        'polygon_geojson': POLYGON, 'is_active': True, 'priority': 10,
    }


# ===========================================================================
# The role templates carry what the matrix says they carry
# ===========================================================================

class TestTheTemplates:
    def test_zone_read_is_held_by_exactly_the_matrix_roles(self):
        holders = {t['code'] for t in ROLE_TEMPLATES if 'zone.read' in t['permissions']}

        assert holders == ZONE_READ_ROLES

    def test_safety_respond_is_held_by_exactly_the_matrix_roles(self):
        holders = {t['code'] for t in ROLE_TEMPLATES if 'safety.respond' in t['permissions']}

        assert holders == SOS_ROLES

    def test_pricing_write_is_super_admin_only(self):
        """Until maker-checker exists. No other template may grow it quietly."""
        holders = {t['code'] for t in ROLE_TEMPLATES if 'pricing.write' in t['permissions']}

        assert holders == PRICING_WRITE_ROLES

    def test_support_read_is_held_by_exactly_the_matrix_roles(self):
        holders = {t['code'] for t in ROLE_TEMPLATES if 'support.read' in t['permissions']}

        assert holders == TICKET_READ_ROLES

    def test_the_seed_grants_zone_read(self, geo):
        """`seed_admin_access` upserts the templates, so QA gets it on the next run."""
        assert AdminRole.objects.get(code='analyst').grants('zone.read')
        assert not AdminRole.objects.get(code='support_agent').grants('zone.read')


# ===========================================================================
# SOS
# ===========================================================================

class TestSOSMatrix:
    @pytest.mark.parametrize('role', ROLES)
    def test_list(self, geo, world, role):
        client, _ = operator(role, geo)
        response = client.get('/api/v1/sos/admin/')

        assert response.status_code == (200 if role in SOS_ROLES else 403), (role, response.content[:200])

    @pytest.mark.parametrize('role', ROLES)
    def test_detail(self, geo, world, role):
        client, _ = operator(role, geo)
        response = client.get(f'/api/v1/sos/admin/{world["hyd_event"].id}/')

        assert response.status_code == (200 if role in SOS_ROLES else 403), role

    @pytest.mark.parametrize('action', ['acknowledge', 'resolve', 'false-alarm'])
    @pytest.mark.parametrize('role', ROLES)
    def test_transitions(self, geo, world, role, action):
        client, _ = operator(role, geo)
        response = client.post(
            f'/api/v1/sos/admin/{world["hyd_event"].id}/{action}/',
            {'note': 'handled'}, format='json',
        )

        assert response.status_code == (200 if role in SOS_ROLES else 403), (role, action)
        world['hyd_event'].refresh_from_db()
        if role in SOS_ROLES:
            assert world['hyd_event'].status == TARGET_STATUS[action]
        else:
            assert world['hyd_event'].status == 'open', 'a refused transition changed the row'

    @pytest.mark.parametrize('action', ['acknowledge', 'resolve', 'false-alarm'])
    def test_the_console_alias_path_carries_the_same_gate(self, geo, world, action):
        """`/sos/<id>/<action>/` is what the deployed console calls."""
        safety, _ = operator('safety_officer', geo)
        support, _ = operator('support_agent', geo)
        url = f'/api/v1/sos/{world["hyd_event"].id}/{action}/'

        assert support.post(url, {'note': 'x'}, format='json').status_code == 403
        assert safety.post(url, {'note': 'x'}, format='json').status_code == 200

    @pytest.mark.parametrize('role', ROLES)
    def test_reveal(self, geo, world, role):
        client, _ = operator(role, geo)
        response = client.post(
            f'/api/v1/sos/admin/{world["hyd_event"].id}/reveal/',
            {'reason': 'calling the rider'}, format='json',
        )

        assert response.status_code == (200 if role in SOS_ROLES else 403), role
        logged = AdminAuditLog.objects.filter(
            action=CONTACT_REVEALED_ACTION, target_id=str(world['hyd_event'].id),
        ).exists()
        assert logged is (role in SOS_ROLES), 'a reveal and its audit row must agree'


class TestSOSScope:
    """Everything a Hyderabad Safety Officer can and cannot reach."""

    @pytest.fixture
    def safety(self, geo):
        return operator('safety_officer', geo)[0]

    @pytest.fixture
    def super_admin(self, geo):
        return operator(SUPER_ADMIN, geo)[0]

    def test_the_list_is_the_callers_city(self, safety, world):
        ids = ids_in(safety.get('/api/v1/sos/admin/'))

        assert world['hyd_event'].id in ids
        # Anchored through the DRIVER's home city, no trip.
        assert world['driver_event'].id in ids
        assert world['aus_event'].id not in ids

    def test_a_riders_off_trip_sos_is_unanchored_and_reaches_every_safety_officer(
        self, safety, super_admin, geo, world,
    ):
        """Riders have no city, so an SOS a rider raises outside any trip and
        without a GPS fix has no anchor. Owner's ruling (2026-10-28): such an
        event must never be invisible to the people who respond to it -- it
        is in EVERY Safety Officer's queue, whatever their scope, and at
        Global, and a Command Centre alert is raised. The full matrix of
        paths is in `tests/test_sos_anchoring.py`."""
        event = world['rider_offtrip_event']
        assert event.anchor_source == 'unanchored' and event.anchor_node is None

        austin_safety, _ = operator('safety_officer', geo, scope_code='US-TX-AUS')
        assert event.id in ids_in(safety.get('/api/v1/sos/admin/'))
        assert event.id in ids_in(austin_safety.get('/api/v1/sos/admin/'))
        assert event.id in ids_in(super_admin.get('/api/v1/sos/admin/'))

    def test_super_admin_sees_both_countries(self, super_admin, world):
        ids = ids_in(super_admin.get('/api/v1/sos/admin/'))

        assert {world['hyd_event'].id, world['aus_event'].id} <= ids

    def test_the_open_count_is_scoped(self, safety, world):
        _sos(world['aus_rider'], world['aus_trip'])  # another open one, elsewhere
        payload = safety.get('/api/v1/sos/admin/').json()['data']

        assert payload['open_count'] == SOSEvent.objects.filter(
            status='open', trip__zone=world['hyd_zone'],
        ).count() + 2  # + the driver's off-trip event + the unanchored rider SOS

    def test_scope_param_narrows_but_never_widens(self, safety, super_admin, world):
        narrowed = super_admin.get('/api/v1/sos/admin/?scope=US-TX-AUS')
        assert narrowed.status_code == 200
        # Austin's event, plus the one that is nowhere: an unanchored SOS
        # stays in a narrowed view too (`scoping.UNSCOPED_UNION`).
        assert ids_in(narrowed) == {world['aus_event'].id, world['rider_offtrip_event'].id}

        refused = safety.get('/api/v1/sos/admin/?scope=US-TX-AUS')
        assert refused.status_code == 403
        assert refused.json()['error']['code'] == 'SCOPE_NOT_HELD'

    def test_detail_out_of_scope_is_not_found_not_forbidden(self, safety, world):
        response = safety.get(f'/api/v1/sos/admin/{world["aus_event"].id}/')

        assert response.status_code == 404
        missing = safety.get('/api/v1/sos/admin/999999/')
        assert missing.status_code == 404
        assert response.json() == missing.json() or (
            response.json()['error']['code'] == missing.json()['error']['code'] == 'NOT_FOUND'
        )

    @pytest.mark.parametrize('action', ['acknowledge', 'resolve', 'false-alarm', 'reveal'])
    def test_acting_out_of_scope_is_not_found(self, safety, world, action):
        response = safety.post(
            f'/api/v1/sos/admin/{world["aus_event"].id}/{action}/', {}, format='json',
        )

        assert response.status_code == 404, action
        world['aus_event'].refresh_from_db()
        assert world['aus_event'].status == 'open'
        assert not AdminAuditLog.objects.filter(action=CONTACT_REVEALED_ACTION).exists()


class TestSOSContactMasking:
    @pytest.fixture(params=sorted(SOS_ROLES))
    def viewer(self, request, geo):
        """Both roles that can see the queue. Masking is for everybody."""
        return operator(request.param, geo)

    def test_the_number_is_not_in_the_list(self, viewer, world):
        client, _ = viewer
        body = body_of(client.get('/api/v1/sos/admin/'))

        assert HYD_RIDER_PHONE not in body
        assert '9876500001' not in body
        assert '9876500002' not in body          # the driver's, via driver_label
        assert '0001' in body                    # last four survive: confirmable

    def test_the_surname_is_not_in_the_list(self, viewer, world):
        client, _ = viewer
        body = body_of(client.get('/api/v1/sos/admin/'))

        assert 'Kumar' not in body
        assert 'Ravi' in body
        assert 'Reddy' not in body

    def test_the_number_is_not_in_the_detail(self, viewer, world):
        client, _ = viewer
        response = client.get(f'/api/v1/sos/admin/{world["hyd_event"].id}/')

        assert response.status_code == 200
        assert HYD_RIDER_PHONE not in body_of(response)
        assert response.json()['data']['contact_masked'] is True
        assert response.json()['data']['user_phone'].endswith('0001')

    def test_reveal_returns_the_contact_once_and_logs_it(self, viewer, world):
        client, user = viewer
        before = AdminAuditLog.objects.count()

        response = client.post(
            f'/api/v1/sos/admin/{world["hyd_event"].id}/reveal/',
            {'reason': 'rider not answering in app'}, format='json',
        )

        assert response.status_code == 200, response.content
        contact = response.json()['data']['contact']
        assert contact['phone_number'] == HYD_RIDER_PHONE
        assert contact['full_name'] == HYD_RIDER_NAME
        assert contact['emergency_contact'] == HYD_RIDER_EMERGENCY

        assert AdminAuditLog.objects.count() == before + 1
        row = AdminAuditLog.objects.order_by('-id').first()
        assert row.action == CONTACT_REVEALED_ACTION
        assert row.target_type == 'sos_event'
        assert row.target_id == str(world['hyd_event'].id)
        assert row.actor_id == user.id
        assert row.reason == 'rider not answering in app'
        assert set(row.after['fields']) == {'emergency_contact', 'full_name', 'phone_number', 'user_label'}
        # The row says WHAT was revealed, never the value.
        assert HYD_RIDER_PHONE not in json.dumps(row.after)
        assert HYD_RIDER_PHONE not in json.dumps(row.before)
        assert response.json()['data']['audit_id'] == row.id

    def test_reveal_works_while_acknowledged(self, viewer, world):
        client, _ = viewer
        response = client.post(
            f'/api/v1/sos/admin/{world["acknowledged_event"].id}/reveal/', {}, format='json',
        )

        assert response.status_code == 200

    @pytest.mark.parametrize('closed', ['resolved', 'false_alarm'])
    def test_reveal_is_refused_once_the_incident_is_closed(self, viewer, world, closed):
        """Nobody left to reach. 409 with a code, and no audit row either."""
        client, _ = viewer
        event = world['resolved_event']
        if closed != 'resolved':
            event.status = closed
            event.save(update_fields=['status'])

        response = client.post(f'/api/v1/sos/admin/{event.id}/reveal/', {}, format='json')

        assert response.status_code == 409
        assert response.json()['error']['code'] == 'SOS_NOT_ACTIVE'
        assert HYD_RIDER_PHONE not in body_of(response)
        assert not AdminAuditLog.objects.filter(action=CONTACT_REVEALED_ACTION).exists()

    def test_the_queue_stays_masked_after_a_reveal(self, viewer, world):
        """A reveal is a one-time hand-over, not a state change on the row."""
        client, _ = viewer
        client.post(f'/api/v1/sos/admin/{world["hyd_event"].id}/reveal/', {}, format='json')

        assert HYD_RIDER_PHONE not in body_of(client.get('/api/v1/sos/admin/'))


# ===========================================================================
# Support tickets
# ===========================================================================

class TestTicketMatrix:
    @pytest.mark.parametrize('role', ROLES)
    def test_list(self, geo, world, role):
        client, _ = operator(role, geo)
        response = client.get('/api/v1/support/admin/tickets/')

        assert response.status_code == (200 if role in TICKET_READ_ROLES else 403), role

    @pytest.mark.parametrize('role', ROLES)
    def test_detail(self, geo, world, role):
        client, _ = operator(role, geo)
        response = client.get(f'/api/v1/support/admin/tickets/{world["hyd_ticket"].id}/')

        assert response.status_code == (200 if role in TICKET_READ_ROLES else 403), role

    @pytest.mark.parametrize('role', ROLES)
    def test_reply(self, geo, world, role):
        client, _ = operator(role, geo)
        response = client.post(
            f'/api/v1/support/admin/tickets/{world["hyd_ticket"].id}/reply/',
            {'body': 'Looking into it.'}, format='json',
        )

        assert response.status_code == (201 if role in TICKET_WRITE_ROLES else 403), role

    @pytest.mark.parametrize('role', ROLES)
    def test_assign(self, geo, world, role):
        client, user = operator(role, geo)
        response = client.post(
            f'/api/v1/support/admin/tickets/{world["hyd_ticket"].id}/assign/',
            {'assigned_to': user.id}, format='json',
        )

        assert response.status_code == (200 if role in TICKET_WRITE_ROLES else 403), role


class TestTicketScope:
    @pytest.mark.parametrize('role', sorted(TICKET_READ_ROLES - {SUPER_ADMIN}))
    def test_the_list_is_the_callers_city(self, geo, world, role):
        client, _ = operator(role, geo)
        ids = ids_in(client.get('/api/v1/support/admin/tickets/'))

        assert world['hyd_ticket'].id in ids
        assert world['driver_ticket'].id in ids        # requester's home city
        assert world['aus_ticket'].id not in ids
        # A rider's trip-less ticket follows the rider (owner ruling: "a new
        # rider who can't book is a core support case"): this +91 rider is
        # visible in India, so Hyderabad sees the ticket. It used to be
        # Global only. See servers/support/scope.py.
        assert world['rider_offtrip_ticket'].id in ids

    def test_super_admin_sees_everything(self, geo, world):
        client, _ = operator(SUPER_ADMIN, geo)
        ids = ids_in(client.get('/api/v1/support/admin/tickets/'))

        assert {world['hyd_ticket'].id, world['aus_ticket'].id,
                world['driver_ticket'].id, world['rider_offtrip_ticket'].id} <= ids

    @pytest.mark.parametrize('role', sorted(TICKET_READ_ROLES - {SUPER_ADMIN}))
    def test_detail_out_of_scope_is_not_found(self, geo, world, role):
        client, _ = operator(role, geo)

        assert client.get(f'/api/v1/support/admin/tickets/{world["aus_ticket"].id}/').status_code == 404
        assert client.get('/api/v1/support/admin/tickets/999999/').status_code == 404

    def test_reply_and_assign_out_of_scope_are_not_found(self, geo, world):
        client, user = operator('support_agent', geo)
        aus = world['aus_ticket'].id

        assert client.post(f'/api/v1/support/admin/tickets/{aus}/reply/',
                           {'body': 'hi'}, format='json').status_code == 404
        assert client.post(f'/api/v1/support/admin/tickets/{aus}/assign/',
                           {'assigned_to': user.id}, format='json').status_code == 404
        assert world['aus_ticket'].messages.filter(author_role='support').count() == 0

    def test_the_user_side_detail_no_longer_has_a_staff_bypass(self, geo, world):
        """`/support/tickets/<id>/` let ANY staff account read ANY ticket.
        An operator now needs `support.read` where the ticket sits."""
        support, _ = operator('support_agent', geo)
        analyst, _ = operator('analyst', geo)
        hyd, aus = world['hyd_ticket'].id, world['aus_ticket'].id

        assert support.get(f'/api/v1/support/tickets/{hyd}/').status_code == 200
        assert support.get(f'/api/v1/support/tickets/{aus}/').status_code == 404
        assert analyst.get(f'/api/v1/support/tickets/{hyd}/').status_code == 403

    def test_the_requester_still_reads_their_own_ticket(self, world):
        client = _client_for(world['aus_rider'])

        assert client.get(f'/api/v1/support/tickets/{world["aus_ticket"].id}/').status_code == 200
        assert client.get(f'/api/v1/support/tickets/{world["hyd_ticket"].id}/').status_code == 403


# ===========================================================================
# Zones and rate cards
# ===========================================================================

class TestZoneMatrix:
    @pytest.mark.parametrize('role', ROLES)
    def test_zone_list_and_retrieve(self, geo, world, role):
        client, _ = operator(role, geo)
        expected = 200 if role in ZONE_READ_ROLES else 403

        assert client.get('/api/v1/pricing/admin/zones/').status_code == expected, role
        assert client.get(f'/api/v1/pricing/admin/zones/{world["hyd_zone"].pk}/').status_code == expected, role

    @pytest.mark.parametrize('role', ROLES)
    def test_rate_card_list_retrieve_and_effective(self, geo, world, role):
        client, _ = operator(role, geo)
        expected = 200 if role in ZONE_READ_ROLES else 403

        assert client.get('/api/v1/pricing/admin/rate-cards/').status_code == expected, role
        assert client.get(f'/api/v1/pricing/admin/rate-cards/{world["hyd_card"].pk}/').status_code == expected, role
        effective = client.get(
            f'/api/v1/pricing/admin/rate-cards/effective/?zone={world["hyd_zone"].pk}'
            f'&vehicle_type={world["sedan"].pk}'
        )
        assert effective.status_code == expected, role

    @pytest.mark.parametrize('role', ROLES)
    def test_zone_writes(self, geo, world, role):
        client, user = operator(role, geo)
        before = AdminAuditLog.objects.filter(target_type='service_zone').count()

        created = client.post('/api/v1/pricing/admin/zones/',
                              _zone_payload(f'MATRIX-{role.upper()[:8]}'), format='json')
        patched = client.patch(f'/api/v1/pricing/admin/zones/{world["hyd_zone"].pk}/',
                               {'name': f'renamed by {role}'}, format='json')
        deleted = client.delete(f'/api/v1/pricing/admin/zones/{world["aus_zone"].pk}/')

        if role in PRICING_WRITE_ROLES:
            assert created.status_code == 201, created.content[:300]
            assert patched.status_code == 200, patched.content[:300]
            # Austin has a rate card and a trip (PROTECT) -- the write is
            # authorised; the database refuses the delete. Either is fine
            # here; what matters is that it was not a 403/404.
            assert deleted.status_code not in (401, 403, 404), deleted.status_code
            rows = AdminAuditLog.objects.filter(target_type='service_zone').order_by('id')[before:]
            actions = [r.action for r in rows]
            assert 'service_zone_created' in actions and 'service_zone_updated' in actions
            assert all(r.actor_id == user.id for r in rows)
        else:
            assert (created.status_code, patched.status_code, deleted.status_code) == (403, 403, 403), role
            assert not ServiceZone.objects.filter(code__startswith='MATRIX-').exists()
            assert AdminAuditLog.objects.filter(target_type='service_zone').count() == before

    @pytest.mark.parametrize('role', ROLES)
    def test_rate_card_writes(self, geo, world, role):
        client, user = operator(role, geo)
        auto, _ = VehicleType.objects.get_or_create(type='auto')
        payload = {
            'zone': world['hyd_zone'].pk, 'vehicle_type': auto.pk,
            'base_fare': '25.00', 'per_km_fare': '10.00', 'per_min_fare': '1.50',
            'min_fare': '40.00', 'commission_percent': '18.00', 'gst_percent': '5.00',
            'surge_cap_multiplier': '1.50',
        }

        cards_before = RateCard.objects.count()
        created = client.post('/api/v1/pricing/admin/rate-cards/', payload, format='json')
        retired = client.patch(f'/api/v1/pricing/admin/rate-cards/{world["hyd_card"].pk}/',
                               {'notes': f'reviewed by {role}'}, format='json')

        if role in PRICING_WRITE_ROLES:
            assert created.status_code == 201, created.content[:300]
            assert retired.status_code == 200, retired.content[:300]
            assert AdminAuditLog.objects.filter(action='rate_card_created', actor_id=user.id).exists()
            assert AdminAuditLog.objects.filter(action='rate_card_updated', actor_id=user.id).exists()
        else:
            assert (created.status_code, retired.status_code) == (403, 403), role
            assert RateCard.objects.count() == cards_before
            world['hyd_card'].refresh_from_db()
            assert world['hyd_card'].notes == ''


class TestZoneScope:
    @pytest.mark.parametrize('role', sorted(ZONE_READ_ROLES - {SUPER_ADMIN}))
    def test_zones_are_the_callers_city(self, geo, world, role):
        client, _ = operator(role, geo)
        ids = ids_in(client.get('/api/v1/pricing/admin/zones/'))

        assert world['hyd_zone'].pk in ids
        assert world['aus_zone'].pk not in ids
        assert client.get(f'/api/v1/pricing/admin/zones/{world["aus_zone"].pk}/').status_code == 404

    @pytest.mark.parametrize('role', sorted(ZONE_READ_ROLES - {SUPER_ADMIN}))
    def test_rate_cards_follow_their_zone(self, geo, world, role):
        client, _ = operator(role, geo)
        ids = ids_in(client.get('/api/v1/pricing/admin/rate-cards/'))

        assert world['hyd_card'].pk in ids
        assert world['aus_card'].pk not in ids
        assert client.get(f'/api/v1/pricing/admin/rate-cards/{world["aus_card"].pk}/').status_code == 404
        effective = client.get(
            f'/api/v1/pricing/admin/rate-cards/effective/?zone={world["aus_zone"].pk}'
            f'&vehicle_type={world["sedan"].pk}'
        )
        assert effective.status_code == 404

    def test_super_admin_sees_both_countries(self, geo, world):
        client, _ = operator(SUPER_ADMIN, geo)

        assert {world['hyd_zone'].pk, world['aus_zone'].pk} <= ids_in(client.get('/api/v1/pricing/admin/zones/'))
        assert {world['hyd_card'].pk, world['aus_card'].pk} <= ids_in(client.get('/api/v1/pricing/admin/rate-cards/'))

    def test_a_zone_with_no_node_is_visible_at_global_only(self, geo, world):
        """Drawn before its node existed. A Global assignment (not only a
        superuser) must still see it, or nobody can link it."""
        orphan = ServiceZone.objects.create(
            code='ORPHAN', name='Orphan', zone_type='city', city='Nowhere',
            polygon_geojson=POLYGON,
        )
        city_ops, _ = operator('city_ops_manager', geo)
        super_admin, _ = operator(SUPER_ADMIN, geo)

        assert orphan.pk not in ids_in(city_ops.get('/api/v1/pricing/admin/zones/'))
        assert orphan.pk in ids_in(super_admin.get('/api/v1/pricing/admin/zones/'))

    def test_the_public_zone_list_is_untouched(self, world):
        """Rider-facing; no token, no scope."""
        response = APIClient().get('/api/v1/pricing/zones/')

        assert response.status_code == 200
        assert {'IN-TG-HYD', 'US-TX-AUS'} <= {z['code'] for z in response.json()['data']['zones']}


# ===========================================================================
# The migration bridge: unmigrated accounts keep today's access, strict closes it
# ===========================================================================

class TestTheBridge:
    @pytest.fixture
    def unmigrated(self, geo):
        phone = '+919800099001'
        user = User.objects.create_user(
            phone_number=phone, username=phone, role='admin', is_staff=True,
        )
        return _client_for(user)

    @pytest.mark.parametrize('url', [
        '/api/v1/sos/admin/',
        '/api/v1/support/admin/tickets/',
        '/api/v1/pricing/admin/zones/',
        '/api/v1/pricing/admin/rate-cards/',
    ])
    def test_an_unmigrated_operator_keeps_todays_access(self, unmigrated, world, settings, url):
        """These were open to every operator; an account nobody has assigned
        a role yet keeps them until `ADMIN_ACCESS_STRICT`, as the bridge
        promises for every other endpoint. Migrating the person is what
        tightens it."""
        settings.ADMIN_ACCESS_STRICT = False

        assert unmigrated.get(url).status_code == 200, url

    @pytest.mark.parametrize('url', [
        '/api/v1/sos/admin/',
        '/api/v1/support/admin/tickets/',
        '/api/v1/pricing/admin/zones/',
        '/api/v1/pricing/admin/rate-cards/',
    ])
    def test_strict_mode_closes_it(self, unmigrated, world, settings, url):
        settings.ADMIN_ACCESS_STRICT = True

        assert unmigrated.get(url).status_code == 403, url

    def test_an_unmigrated_operator_still_sees_masked_sos_contacts(self, unmigrated, world, settings):
        """Masking is not a migration question: nobody gets the number for free."""
        settings.ADMIN_ACCESS_STRICT = False

        assert HYD_RIDER_PHONE not in body_of(unmigrated.get('/api/v1/sos/admin/'))


# ===========================================================================
# Non-operators are refused everywhere, including the new routes
# ===========================================================================

@pytest.mark.parametrize('verb, url', [
    ('get', '/api/v1/sos/admin/'),
    ('get', '/api/v1/sos/admin/1/'),
    ('post', '/api/v1/sos/admin/1/reveal/'),
    ('post', '/api/v1/sos/admin/1/acknowledge/'),
    ('get', '/api/v1/support/admin/tickets/'),
    ('get', '/api/v1/support/admin/tickets/1/'),
    ('get', '/api/v1/pricing/admin/zones/'),
    ('get', '/api/v1/pricing/admin/rate-cards/'),
])
def test_a_rider_and_an_anonymous_caller_are_refused(world, verb, url):
    rider = _client_for(world['hyd_rider'])

    assert getattr(rider, verb)(url, {}, format='json').status_code in (401, 403), url
    assert getattr(APIClient(), verb)(url, {}, format='json').status_code in (401, 403), url
