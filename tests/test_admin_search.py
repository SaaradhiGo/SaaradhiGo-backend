"""Console search: `/api/v1/search/admin/{trips,drivers,riders,vehicles}/`.

WHAT IS PROVED HERE
-------------------
  * every term shape finds what it should, through every route;
  * phone numbers in any human form resolve to the same account;
  * plates in any spelling resolve to the same vehicle;
  * scope: a Hyderabad operator finds Hyderabad rows and NOT Austin's, a
    Super Admin finds both, and an out-of-scope row is simply absent --
    never "forbidden", which would confirm it exists;
  * a KYC-only operator sees drivers still in KYC and nothing else;
  * what leaves the server: a masked phone for EVERY caller, no email,
    names per `pii.view`;
  * each route refuses an operator without its capability with 403;
  * the per-operator rate limit, per route;
  * the two expression indexes exist and (on PostgreSQL) are used.

Every request goes through the URL conf with a JWT, exactly as the console
does. Fixtures give every operator an explicit assignment so scope applies
(an unmigrated account is served unscoped by design during the migration;
that path is covered in `test_admin_api_cross_country.py`).
"""

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.pricing.models import ServiceZone
from servers.ride.models import Trip
from servers.rider.models import Rider
from servers.search.terms import TermError, parse_term

pytestmark = pytest.mark.django_db

User = get_user_model()

TRIPS = '/api/v1/search/admin/trips/'
DRIVERS = '/api/v1/search/admin/drivers/'
RIDERS = '/api/v1/search/admin/riders/'
VEHICLES = '/api/v1/search/admin/vehicles/'
ROUTES = (TRIPS, DRIVERS, RIDERS, VEHICLES)

HYD_DRIVER_PHONE = '+919876543210'
HYD_DRIVER_EMAIL = 'ravi.kumar@example.test'
HYD_DRIVER_NAME = 'Ravi Kumar'
HYD_PLATE_AS_TYPED = 'TS 09 AB 1234'          # stored raw, with spaces
HYD_KYC_PHONE = '+919812340000'
HYC_KYC_NAME = 'Ravi Pending'
HYD_KYC_PLATE = 'TS07CD5678'
AUS_DRIVER_PHONE = '+15125550101'
AUS_DRIVER_NAME = 'Austin Driver'
AUS_PLATE = 'QAUS0001'
RIDER_PHONE = '+919000011111'
RIDER_EMAIL = 'priya@example.test'
RIDER_NAME = 'Priya Sharma'

SQUARE = {
    'type': 'Polygon',
    'coordinates': [[[78.3, 17.3], [78.6, 17.3], [78.6, 17.6], [78.3, 17.6], [78.3, 17.3]]],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_throttle_buckets():
    """LocMem is process-wide; a bucket must not leak between tests."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


def _zone(code, node, country, state, city):
    """The seeded zone for `code` if a data migration created one, else a new one.

    Either way it is anchored to `node`, which is what scopes a trip.
    """
    zone, _ = ServiceZone.objects.get_or_create(code=code, defaults={
        'name': city, 'country': country, 'state_code': state, 'city': city,
        'zone_type': 'city', 'polygon_geojson': SQUARE, 'priority': 50,
        'is_active': True,
    })
    if zone.geo_node_id != node.id:
        zone.geo_node = node
        zone.save(update_fields=['geo_node'])
    return zone


def _driver(phone, name, city, email=None, approved=True, doc_status='approved'):
    user = User.objects.create_user(
        phone_number=phone, username=phone, email=email, full_name=name, role='driver',
    )
    return Driver.objects.create(
        user_id=user, home_city=city, approved=approved, doc_status=doc_status,
    )


def _vehicle(driver, plate, vehicle_type):
    return Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vehicle_type, vehicle_number=plate,
        brand='Maruti', model='Dzire',
    )


def _trip(rider_user, driver, zone):
    return Trip.objects.create(
        user_id=rider_user, driver_id=driver, zone=zone,
        pickup_lat=Decimal('17.385'), pickup_long=Decimal('78.486'),
        destination_lat=Decimal('17.44'), destination_long=Decimal('78.38'),
        pickup_address='Banjara Hills', destination_address='Hitech City',
    )


@pytest.fixture
def world(geo):
    """Two cities, three drivers, three vehicles, one rider, two trips."""
    hyd_zone = _zone('IN-TG-HYD', geo['IN-TG-HYD'], 'IN', 'TG', 'Hyderabad')
    aus_zone = _zone('US-TX-AUS', geo['US-TX-AUS'], 'US', 'TX', 'Austin')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')

    hyd = _driver(HYD_DRIVER_PHONE, HYD_DRIVER_NAME, geo['IN-TG-HYD'], email=HYD_DRIVER_EMAIL)
    hyd_kyc = _driver(HYD_KYC_PHONE, HYC_KYC_NAME, geo['IN-TG-HYD'],
                      approved=False, doc_status='pending')
    aus = _driver(AUS_DRIVER_PHONE, AUS_DRIVER_NAME, geo['US-TX-AUS'])

    rider_user = User.objects.create_user(
        phone_number=RIDER_PHONE, username=RIDER_PHONE, email=RIDER_EMAIL,
        full_name=RIDER_NAME, role='rider',
    )
    rider = Rider.objects.create(user_id=rider_user)

    return {
        'hyd': hyd, 'hyd_kyc': hyd_kyc, 'aus': aus, 'rider': rider,
        'hyd_vehicle': _vehicle(hyd, HYD_PLATE_AS_TYPED, vt),
        'kyc_vehicle': _vehicle(hyd_kyc, HYD_KYC_PLATE, vt),
        'aus_vehicle': _vehicle(aus, AUS_PLATE, vt),
        'hyd_trip': _trip(rider_user, hyd, hyd_zone),
        'aus_trip': _trip(rider_user, aus, aus_zone),
    }


_counter = iter(range(1000, 9999))


def client_for(role_code, scope):
    n = next(_counter)
    phone = f'+9188000{n:05d}'
    user = User.objects.create_user(
        phone_number=phone, username=phone, email=f'op{n}@saaradhigo.test',
        role='admin', is_staff=True,
    )
    AdminAssignment.objects.create(
        admin=user, role=AdminRole.objects.get(code=role_code), scope=scope,
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


@pytest.fixture
def super_admin(geo):
    return client_for('super_admin', geo['GLOBAL'])


@pytest.fixture
def hyd_ops(geo):
    return client_for('city_ops_manager', geo['IN-TG-HYD'])


@pytest.fixture
def aus_ops(geo):
    return client_for('city_ops_manager', geo['US-TX-AUS'])


@pytest.fixture
def kyc_only(geo):
    """KYC Verifier: `driver.kyc` + `driver.read`, no `driver.write`, no `trip.read`."""
    return client_for('kyc_verifier', geo['IN-TG-HYD'])


@pytest.fixture
def analyst(geo):
    """Reads trips, drivers and riders; explicitly NO `pii.view`."""
    return client_for('analyst', geo['IN'])


@pytest.fixture
def marketing(geo):
    """None of the four read capabilities."""
    return client_for('marketing', geo['IN'])


def results(response):
    assert response.status_code == 200, response.content
    return response.json()['data']['results']


def ids(response):
    return {row['id'] for row in results(response)}


def kind(response):
    return response.json()['data']['kind']


def body_of(response):
    return json.dumps(response.json())


# ---------------------------------------------------------------------------
# Term parsing, with no database
# ---------------------------------------------------------------------------

class TestTermParsing:
    @pytest.mark.parametrize('raw', ['+91 98765 43210', '98765-43210', '9876543210', '+919876543210'])
    def test_an_indian_number_in_any_form_normalises_to_one_e164(self, raw):
        term = parse_term(raw)
        assert term.phone_e164 == '+919876543210'
        assert term.phone_tail == '9876543210'

    @pytest.mark.parametrize('raw', ['+1 (512) 555-0101', '+15125550101', '+1-512-555-0101'])
    def test_a_us_number_with_its_country_code_normalises_to_one_e164(self, raw):
        term = parse_term(raw)
        assert term.phone_e164 == '+15125550101'
        assert term.phone_tail == '5125550101'

    @pytest.mark.parametrize('raw', ['(512) 555-0101', '512-555-0101', '5125550101'])
    def test_a_bare_national_number_keeps_its_ten_digit_tail(self, raw):
        """Without a country code `normalize_phone` guesses the default
        market, which may be the wrong one; the TAIL is what still finds a
        US account typed without its +1, and is why the tail is indexed."""
        term = parse_term(raw)
        assert term.phone_tail == '5125550101'
        assert term.phone_e164 is not None

    def test_digits_are_a_trip_id_and_a_phone_prefix_and_a_plate_prefix(self):
        term = parse_term('98765')
        assert term.kind == 'number'
        assert term.trip_id == 98765
        assert term.phone_prefix == '98765'
        assert term.phone_e164 is None
        assert term.plate == '98765'

    def test_ten_digits_are_a_whole_phone_number_and_still_a_trip_id(self):
        term = parse_term('9876543210')
        assert term.trip_id == 9876543210
        assert term.phone_e164 == '+919876543210'
        assert term.phone_prefix is None

    def test_three_digits_are_not_a_phone_partial(self):
        term = parse_term('123')
        assert term.trip_id == 123
        assert not term.searches_phones

    def test_a_single_digit_is_a_trip_id(self):
        """The first trips in any database have one-digit ids."""
        term = parse_term('7')
        assert term.trip_id == 7
        assert not term.searches_phones

    @pytest.mark.parametrize('raw, plate', [
        ('TS09AB1234', 'TS09AB1234'), ('ts-09-ab-1234', 'TS09AB1234'),
        ('TS 09 AB 1234', 'TS09AB1234'), ('QAUS0001', 'QAUS0001'), ('ts09', 'TS09'),
    ])
    def test_a_plate_in_any_spelling_normalises_to_one_string(self, raw, plate):
        term = parse_term(raw)
        assert term.kind == 'plate'
        assert term.plate == plate
        assert term.name is None

    @pytest.mark.parametrize('raw', ['ravi', 'Ravi Kumar', "O'Brien", 'Jean-Luc', 'J. Doe'])
    def test_letters_are_a_name(self, raw):
        term = parse_term(raw)
        assert term.kind == 'name'
        assert term.name == raw
        assert term.plate is None and term.trip_id is None

    @pytest.mark.parametrize('raw', ['x@y.com', 'select *', '%%%', '../../etc'])
    def test_anything_else_is_unsupported_and_searches_nothing(self, raw):
        term = parse_term(raw)
        assert term.kind == 'unsupported'
        assert term.name is None and term.plate is None
        assert term.trip_id is None and not term.searches_phones

    @pytest.mark.parametrize('raw, code', [
        ('a', 'SEARCH_TERM_TOO_SHORT'), ('', 'SEARCH_TERM_TOO_SHORT'),
        ('x' * 65, 'SEARCH_TERM_TOO_LONG'),
    ])
    def test_too_short_or_too_long_is_refused(self, raw, code):
        with pytest.raises(TermError) as exc:
            parse_term(raw)
        assert exc.value.code == code


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------

class TestTrips:
    def test_a_trip_id_finds_the_trip_with_its_people(self, super_admin, world):
        trip = world['hyd_trip']
        rows = results(super_admin.get(TRIPS, {'q': str(trip.id)}))

        assert [r['id'] for r in rows] == [trip.id]
        row = rows[0]
        assert row['type'] == 'trip'
        assert row['city_code'] == 'IN-TG-HYD'
        assert row['status'] == 'requested'
        assert row['rider']['id'] == world['rider'].user_id.id
        assert row['driver']['id'] == world['hyd'].user_id.id

    def test_a_hyderabad_operator_finds_hyderabad_and_not_austin(self, hyd_ops, world):
        assert ids(hyd_ops.get(TRIPS, {'q': str(world['hyd_trip'].id)})) == {world['hyd_trip'].id}
        response = hyd_ops.get(TRIPS, {'q': str(world['aus_trip'].id)})
        # Absent, not forbidden: a 403 would confirm the id exists.
        assert response.status_code == 200
        assert ids(response) == set()

    def test_an_austin_operator_sees_the_mirror_image(self, aus_ops, world):
        assert ids(aus_ops.get(TRIPS, {'q': str(world['aus_trip'].id)})) == {world['aus_trip'].id}
        assert ids(aus_ops.get(TRIPS, {'q': str(world['hyd_trip'].id)})) == set()

    def test_a_super_admin_finds_both(self, super_admin, world):
        for key in ('hyd_trip', 'aus_trip'):
            assert ids(super_admin.get(TRIPS, {'q': str(world[key].id)})) == {world[key].id}

    @pytest.mark.parametrize('q', ['+91 98765 43210', 'TS09AB1234', 'ravi'])
    def test_other_term_kinds_find_no_trip_and_say_what_they_were(self, super_admin, world, q):
        response = super_admin.get(TRIPS, {'q': q})
        assert results(response) == []
        assert kind(response) in {'phone', 'plate', 'name'}

    def test_a_scope_the_caller_does_not_hold_is_refused(self, hyd_ops, world):
        response = hyd_ops.get(TRIPS, {'q': str(world['hyd_trip'].id), 'scope': 'US-TX-AUS'})
        assert response.status_code == 403
        assert response.json()['error']['code'] == 'SCOPE_NOT_HELD'


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

class TestDrivers:
    @pytest.mark.parametrize('q', [
        '+919876543210', '+91 98765 43210', '98765-43210', '98765 43210',
        '9876543210', '919876543210',
    ])
    def test_a_phone_in_any_form_finds_the_driver(self, super_admin, world, q):
        assert ids(super_admin.get(DRIVERS, {'q': q})) == {world['hyd'].id}

    @pytest.mark.parametrize('q', ['+1 (512) 555-0101', '+15125550101', '512-555-0101', '5125550101'])
    def test_a_us_phone_in_any_form_finds_the_us_driver(self, super_admin, world, q):
        assert ids(super_admin.get(DRIVERS, {'q': q})) == {world['aus'].id}

    def test_a_partial_number_is_a_prefix_of_the_national_part(self, super_admin, world):
        assert ids(super_admin.get(DRIVERS, {'q': '98765'})) == {world['hyd'].id}
        assert ids(super_admin.get(DRIVERS, {'q': '9812'})) == {world['hyd_kyc'].id}
        # Two digits are a trip id and a plate prefix, never a phone partial.
        assert ids(super_admin.get(DRIVERS, {'q': '98'})) == set()

    def test_a_name_matches_case_insensitively_and_partially(self, super_admin, world):
        assert ids(super_admin.get(DRIVERS, {'q': 'ravi'})) == {world['hyd'].id, world['hyd_kyc'].id}
        assert ids(super_admin.get(DRIVERS, {'q': 'Ravi Kumar'})) == {world['hyd'].id}
        assert ids(super_admin.get(DRIVERS, {'q': 'KUMAR'})) == {world['hyd'].id}

    def test_the_limit_caps_a_name_search(self, super_admin, world):
        rows = results(super_admin.get(DRIVERS, {'q': 'ravi', 'limit': 1}))
        assert len(rows) == 1

    def test_scoped_like_the_driver_list(self, hyd_ops, aus_ops, super_admin, world):
        assert ids(hyd_ops.get(DRIVERS, {'q': 'ravi'})) == {world['hyd'].id, world['hyd_kyc'].id}
        assert ids(hyd_ops.get(DRIVERS, {'q': AUS_DRIVER_PHONE})) == set()
        assert ids(aus_ops.get(DRIVERS, {'q': AUS_DRIVER_PHONE})) == {world['aus'].id}
        assert ids(aus_ops.get(DRIVERS, {'q': 'ravi'})) == set()
        assert ids(super_admin.get(DRIVERS, {'q': 'driver'})) == {world['aus'].id}

    def test_a_kyc_only_operator_sees_only_drivers_still_in_kyc(self, kyc_only, world):
        """Same predicate as the driver list: approved + approved docs is not KYC's."""
        assert ids(kyc_only.get(DRIVERS, {'q': 'ravi'})) == {world['hyd_kyc'].id}
        assert ids(kyc_only.get(DRIVERS, {'q': HYD_DRIVER_PHONE})) == set()
        assert ids(kyc_only.get(DRIVERS, {'q': HYD_KYC_PHONE})) == {world['hyd_kyc'].id}

    def test_a_driver_row_carries_what_the_console_needs_and_nothing_more(self, super_admin, world):
        row = results(super_admin.get(DRIVERS, {'q': HYD_DRIVER_PHONE}))[0]
        assert set(row) == {'type', 'id', 'status', 'approved', 'doc_status', 'city_code', 'user'}
        assert row['city_code'] == 'IN-TG-HYD'
        assert set(row['user']) == {'id', 'full_name', 'phone'}


# ---------------------------------------------------------------------------
# Riders (global by design, gated on rider.read)
# ---------------------------------------------------------------------------

class TestRiders:
    @pytest.mark.parametrize('q', ['+919000011111', '90000 11111', '9000011111', '90000'])
    def test_a_phone_in_any_form_finds_the_rider(self, hyd_ops, world, q):
        assert ids(hyd_ops.get(RIDERS, {'q': q})) == {world['rider'].id}

    def test_a_name_finds_the_rider(self, hyd_ops, world):
        assert ids(hyd_ops.get(RIDERS, {'q': 'priya'})) == {world['rider'].id}

    def test_a_rider_links_to_their_latest_trip(self, hyd_ops, world):
        row = results(hyd_ops.get(RIDERS, {'q': 'priya'}))[0]
        assert row['last_trip_id'] == max(world['hyd_trip'].id, world['aus_trip'].id)
        assert row['city_code'] is None

    def test_riders_are_found_from_any_city(self, aus_ops, world):
        assert ids(aus_ops.get(RIDERS, {'q': 'priya'})) == {world['rider'].id}

    def test_a_driver_is_not_a_rider(self, super_admin, world):
        assert ids(super_admin.get(RIDERS, {'q': HYD_DRIVER_PHONE})) == set()


# ---------------------------------------------------------------------------
# Vehicles (scoped through the driver)
# ---------------------------------------------------------------------------

class TestVehicles:
    @pytest.mark.parametrize('q', ['TS09AB1234', 'ts-09-ab-1234', 'TS 09 AB 1234', 'ts09ab1234'])
    def test_a_plate_in_any_spelling_finds_the_vehicle_stored_with_spaces(self, super_admin, world, q):
        rows = results(super_admin.get(VEHICLES, {'q': q}))
        assert [r['id'] for r in rows] == [world['hyd_vehicle'].id]
        assert rows[0]['plate'] == HYD_PLATE_AS_TYPED
        assert rows[0]['driver_id'] == world['hyd'].id

    def test_a_prefix_finds_every_plate_that_starts_with_it(self, super_admin, world):
        assert ids(super_admin.get(VEHICLES, {'q': 'TS'})) == set()  # letters only is a name
        assert ids(super_admin.get(VEHICLES, {'q': 'TS0'})) == {world['hyd_vehicle'].id, world['kyc_vehicle'].id}
        assert ids(super_admin.get(VEHICLES, {'q': 'ts-07'})) == {world['kyc_vehicle'].id}

    @pytest.mark.parametrize('q', ['QAUS0001', 'qaus 0001', 'QAUS-0001'])
    def test_a_qa_plate_is_found(self, super_admin, world, q):
        assert ids(super_admin.get(VEHICLES, {'q': q})) == {world['aus_vehicle'].id}

    def test_scoped_through_the_driver(self, hyd_ops, aus_ops, world):
        assert ids(hyd_ops.get(VEHICLES, {'q': 'TS09AB1234'})) == {world['hyd_vehicle'].id}
        assert ids(hyd_ops.get(VEHICLES, {'q': AUS_PLATE})) == set()
        assert ids(aus_ops.get(VEHICLES, {'q': AUS_PLATE})) == {world['aus_vehicle'].id}
        assert ids(aus_ops.get(VEHICLES, {'q': 'TS09AB1234'})) == set()

    def test_a_kyc_only_operator_sees_only_kyc_drivers_vehicles(self, kyc_only, world):
        assert ids(kyc_only.get(VEHICLES, {'q': 'TS0'})) == {world['kyc_vehicle'].id}
        assert ids(kyc_only.get(VEHICLES, {'q': 'TS09AB1234'})) == set()

    @pytest.mark.parametrize('q', ['ravi', '+919876543210'])
    def test_other_term_kinds_find_no_vehicle(self, super_admin, world, q):
        assert results(super_admin.get(VEHICLES, {'q': q})) == []


# ---------------------------------------------------------------------------
# What leaves the server
# ---------------------------------------------------------------------------

class TestWhatLeavesTheServer:
    def test_the_phone_is_masked_even_for_an_operator_who_holds_pii_view(self, super_admin, world):
        """Search finds the record; the record's own page is where a reveal is logged."""
        for route, q in ((DRIVERS, HYD_DRIVER_PHONE), (RIDERS, RIDER_PHONE),
                         (TRIPS, str(world['hyd_trip'].id)), (VEHICLES, 'TS09AB1234')):
            body = body_of(super_admin.get(route, {'q': q}))
            assert HYD_DRIVER_PHONE not in body, route
            assert '9876543210' not in body, route
            assert RIDER_PHONE not in body, route
            assert '9000011111' not in body, route

    def test_the_masked_phone_keeps_the_country_code_and_last_four(self, super_admin, world):
        row = results(super_admin.get(DRIVERS, {'q': HYD_DRIVER_PHONE}))[0]
        assert row['user']['phone'] == '+91••••••3210'

    def test_no_email_anywhere(self, super_admin, world):
        for route, q in ((DRIVERS, 'ravi'), (RIDERS, 'priya'),
                         (TRIPS, str(world['hyd_trip'].id)), (VEHICLES, 'TS09AB1234')):
            body = body_of(super_admin.get(route, {'q': q}))
            assert HYD_DRIVER_EMAIL not in body, route
            assert RIDER_EMAIL not in body, route
            assert '@example.test' not in body, route
            assert 'email' not in body, route

    def test_no_documents_anywhere(self, super_admin, world):
        body = body_of(super_admin.get(VEHICLES, {'q': 'TS09AB1234'}))
        for forbidden in ('rc_doc', 'insurance_doc', 'permit_doc', 'license'):
            assert forbidden not in body

    def test_names_follow_pii_view(self, super_admin, analyst, world):
        with_pii = results(super_admin.get(DRIVERS, {'q': HYD_DRIVER_PHONE}))[0]
        assert with_pii['user']['full_name'] == HYD_DRIVER_NAME

        without = results(analyst.get(DRIVERS, {'q': HYD_DRIVER_PHONE}))[0]
        assert without['user']['full_name'] == 'Ravi K.'
        assert 'Kumar' not in body_of(analyst.get(DRIVERS, {'q': HYD_DRIVER_PHONE}))

    def test_an_out_of_scope_row_is_absent_not_forbidden(self, hyd_ops, world):
        """The same 200 with an empty list whether the row is elsewhere or nowhere."""
        elsewhere = hyd_ops.get(VEHICLES, {'q': AUS_PLATE})
        nowhere = hyd_ops.get(VEHICLES, {'q': 'ZZ99ZZ9999'})
        assert elsewhere.status_code == nowhere.status_code == 200
        assert elsewhere.json()['data'] == nowhere.json()['data']


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_each_route_carries_its_own_capability(self):
        from tests.test_admin_namespace_is_gated import admin_routes, gates_for

        routes = dict(admin_routes())
        expected = {
            'api/v1/search/admin/trips/': 'trip.read',
            'api/v1/search/admin/drivers/': 'driver.read',
            'api/v1/search/admin/riders/': 'rider.read',
            'api/v1/search/admin/vehicles/': 'driver.read',
        }
        for route, capability in expected.items():
            assert route in routes, route
            held = {getattr(p, 'permission', None) for p in gates_for(routes[route])['GET']}
            assert capability in held, (route, held)

    @pytest.mark.parametrize('route', ROUTES)
    def test_an_operator_without_the_capability_gets_403(self, marketing, world, route):
        response = marketing.get(route, {'q': 'ravi'})
        assert response.status_code == 403

    def test_a_kyc_verifier_may_search_drivers_and_vehicles_only(self, kyc_only, world):
        assert kyc_only.get(TRIPS, {'q': str(world['hyd_trip'].id)}).status_code == 403
        assert kyc_only.get(RIDERS, {'q': 'priya'}).status_code == 403
        assert kyc_only.get(DRIVERS, {'q': 'ravi'}).status_code == 200
        assert kyc_only.get(VEHICLES, {'q': 'TS0'}).status_code == 200

    @pytest.mark.parametrize('route', ROUTES)
    def test_a_rider_token_is_refused(self, route, world):
        user = world['rider'].user_id
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
        assert client.get(route, {'q': 'ravi'}).status_code == 403

    @pytest.mark.parametrize('route', ROUTES)
    def test_anonymous_is_refused(self, route, world):
        assert APIClient().get(route, {'q': 'ravi'}).status_code == 401


# ---------------------------------------------------------------------------
# Term validation over HTTP
# ---------------------------------------------------------------------------

class TestTermValidation:
    @pytest.mark.parametrize('route', ROUTES)
    def test_a_one_character_term_is_a_400(self, super_admin, world, route):
        response = super_admin.get(route, {'q': 'a'})
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'SEARCH_TERM_TOO_SHORT'

    def test_an_overlong_term_is_a_400(self, super_admin, world):
        response = super_admin.get(DRIVERS, {'q': 'x' * 65})
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'SEARCH_TERM_TOO_LONG'

    def test_an_email_is_unsupported_and_finds_nothing(self, super_admin, world):
        """Email is never searched: it is not a thing an operator is handed."""
        response = super_admin.get(RIDERS, {'q': RIDER_EMAIL})
        assert results(response) == []
        assert kind(response) == 'unsupported'

    def test_the_limit_is_clamped(self, super_admin, world):
        assert super_admin.get(DRIVERS, {'q': 'ravi', 'limit': '999'}).status_code == 200
        assert super_admin.get(DRIVERS, {'q': 'ravi', 'limit': 'lots'}).status_code == 200
        assert len(results(super_admin.get(DRIVERS, {'q': 'ravi', 'limit': '0'}))) == 1


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

class TestRateLimit:
    def test_the_configured_rate_is_sixty_a_minute(self, settings):
        assert settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['admin_search'] == '60/min'

    def test_the_sixty_first_search_in_a_minute_is_refused(self, monkeypatch, super_admin, world):
        """Per operator, per route. Run at 3/min so the test is three requests.

        Patched on the class: DRF copies `DEFAULT_THROTTLE_RATES` onto
        `SimpleRateThrottle.THROTTLE_RATES` at import, so overriding the
        setting mid-test changes nothing."""
        from servers.search.throttles import AdminSearchThrottle

        monkeypatch.setattr(AdminSearchThrottle, 'THROTTLE_RATES', {'admin_search': '3/min'})

        for _ in range(3):
            assert super_admin.get(DRIVERS, {'q': 'ravi'}).status_code == 200
        refused = super_admin.get(DRIVERS, {'q': 'ravi'})
        assert refused.status_code == 429

        # A different route is a different bucket: one search is one debit on
        # each of the four, not four on one.
        assert super_admin.get(VEHICLES, {'q': 'TS0'}).status_code == 200

    def test_the_bucket_is_per_operator(self, monkeypatch, super_admin, hyd_ops, world):
        from servers.search.throttles import AdminSearchThrottle

        monkeypatch.setattr(AdminSearchThrottle, 'THROTTLE_RATES', {'admin_search': '2/min'})

        for _ in range(2):
            super_admin.get(RIDERS, {'q': 'priya'})
        assert super_admin.get(RIDERS, {'q': 'priya'}).status_code == 429
        assert hyd_ops.get(RIDERS, {'q': 'priya'}).status_code == 200


# ---------------------------------------------------------------------------
# Logging: the term never reaches a log line
# ---------------------------------------------------------------------------

def test_the_log_carries_the_length_and_kind_never_the_term(caplog, super_admin, world):
    import logging

    with caplog.at_level(logging.INFO, logger='servers.search.views'):
        super_admin.get(DRIVERS, {'q': HYD_DRIVER_PHONE})

    lines = [r.getMessage() for r in caplog.records if r.name == 'servers.search.views']
    assert lines, caplog.text
    assert 'term_len=13' in lines[-1]
    assert 'kind=phone' in lines[-1]
    assert HYD_DRIVER_PHONE not in caplog.text
    assert '9876543210' not in caplog.text


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

class TestIndexes:
    def test_the_two_expression_indexes_are_declared_on_the_models(self):
        assert 'vehicle_plate_norm_idx' in {i.name for i in Vehicle._meta.indexes}
        assert 'user_phone_tail_idx' in {i.name for i in User._meta.indexes}

    def test_the_two_expression_indexes_exist_in_the_database(self):
        """Whatever the backend, the migration created them."""
        with connection.cursor() as cursor:
            for table in (Vehicle._meta.db_table, User._meta.db_table):
                names = {c for c in connection.introspection.get_constraints(cursor, table)}
                assert {'vehicle_plate_norm_idx', 'user_phone_tail_idx'} & names, (table, sorted(names))

    def test_the_index_expression_is_the_query_expression(self, super_admin, world):
        """A query that does not match the index expression never uses it."""
        from servers.driver.plates import normalised_plate_expression

        index = next(i for i in Vehicle._meta.indexes if i.name == 'vehicle_plate_norm_idx')
        assert repr(index.expressions[0]) == repr(normalised_plate_expression())

    @pytest.mark.postgres
    def test_postgresql_can_answer_both_lookups_from_the_indexes(self, super_admin, world):
        """With sequential scans disabled, both the exact and the prefix form
        of each lookup must still plan -- i.e. the planner can use the
        index for them. Whether it CHOOSES to on a three-row table is not
        the question; whether it CAN is."""
        from base.db_indexes import phone_tail
        from servers.driver.plates import normalised_plate_expression

        plates = Vehicle.objects.annotate(plate_norm=normalised_plate_expression())
        people = User.objects.annotate(phone_tail=phone_tail('phone_number'))
        queries = {
            'plate exact': plates.filter(plate_norm='TS09AB1234'),
            'plate prefix': plates.filter(plate_norm__startswith='TS09'),
            'phone tail exact': people.filter(phone_tail='9876543210'),
            'phone tail prefix': people.filter(phone_tail__startswith='98765'),
        }
        with connection.cursor() as cursor:
            cursor.execute('SET enable_seqscan = off')
            try:
                for label, qs in queries.items():
                    sql, params = qs.query.sql_with_params()
                    cursor.execute('EXPLAIN ' + sql, params)
                    plan = '\n'.join(row[0] for row in cursor.fetchall())
                    wanted = 'vehicle_plate_norm_idx' if 'plate' in label else 'user_phone_tail_idx'
                    assert wanted in plan, (label, plan)
            finally:
                cursor.execute('RESET enable_seqscan')
