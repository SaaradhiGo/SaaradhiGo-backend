"""`seed_us_test_city`: labelled US test data, and the cross-country proof.

THE RULING (owner, 2026-10-28)
------------------------------
    Cross-country check: QA has no US data, so seed the paused US city
    (clearly labelled seed) with a couple of test drivers and trips, then
    confirm a Hyderabad-scoped role can't see them (404, never 403) and
    Super Admin can.

Three things are proved here, and the third is the one the ruling is for:

  * the command itself -- refuses in production before any write, creates
    the rows with the fields scope keys on, is idempotent, is audited once
    per run, and `--remove` takes back exactly what it made;
  * the rows are where scope looks: `Trip.zone -> ServiceZone.geo_node`
    and `Driver.home_city`, both reaching `GeoNode US-TX-AUS`;
  * over real HTTP, a `city_ops_manager@IN-TG-HYD` sees none of it -- the
    lists omit the Austin rows and keep exactly the Hyderabad ones, and a
    direct read of an Austin id answers 404 with the SAME body as an id
    that never existed -- while `super_admin@GLOBAL` sees all of it.
"""

from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from conftest import SEED_ACTOR_EMAIL, seed_actor, seed_admin_access
from servers.admin_access.management.commands import seed_us_test_city as seed
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import Driver, Vehicle, WithdrawalRequest
from servers.pricing.models import ServiceZone
from servers.ride.models import FarePricing, Trip, TripStatus
from servers.rider.models import Rider

pytestmark = pytest.mark.django_db

User = get_user_model()

TRIPS_URL = '/api/v1/ride/admin/trips/'
DRIVERS_URL = '/api/v1/driver/admin/'

HYD_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[78.30, 17.40], [78.50, 17.40], [78.50, 17.50],
                     [78.30, 17.50], [78.30, 17.40]]],
}


# ---------------------------------------------------------------------------
# Driving the command
# ---------------------------------------------------------------------------

def run(*args, actor=SEED_ACTOR_EMAIL):
    out = StringIO()
    kwargs = {'stdout': out, 'stderr': out}
    if actor is not None:
        kwargs['actor_email'] = actor
    call_command('seed_us_test_city', *args, **kwargs)
    return out.getvalue()


def fingerprint():
    """A picture of every table the seed writes."""
    return (
        tuple(sorted(User.objects.values_list('phone_number', flat=True))),
        Driver.objects.count(), Vehicle.objects.count(), Rider.objects.count(),
        Trip.objects.count(), FarePricing.objects.count(),
        tuple(sorted(ServiceZone.objects.values_list('code', flat=True))),
    )


def rows_since(count):
    return list(AdminAuditLog.objects.order_by('id')[count:])


@pytest.fixture
def geo():
    """The geo tree and the role templates, seeded by the owning command."""
    seed_admin_access()
    return {node.code: node for node in GeoNode.objects.all()}


@pytest.fixture
def seeded(geo):
    """The seed, run once, with its rows looked up again by their keys."""
    run()
    return seed.find_seed_rows()


# ---------------------------------------------------------------------------
# People and places on the Indian side
# ---------------------------------------------------------------------------

def hyd_zone():
    """The Hyderabad zone the pricing migrations seed, linked to its node."""
    zone = ServiceZone.objects.filter(code='IN-TG-HYD').first()
    if zone is None:
        zone = ServiceZone.objects.create(
            code='IN-TG-HYD', name='Hyderabad', zone_type='city', city='Hyderabad',
            polygon_geojson=HYD_POLYGON, priority=10, is_active=True,
        )
    if zone.geo_node_id is None:
        zone.geo_node = GeoNode.objects.get(code='IN-TG-HYD')
        zone.save(update_fields=['geo_node'])
    return zone


def make_driver(phone, city):
    user = User.objects.create_user(phone_number=phone, username=phone, role='driver',
                                    full_name='A Real Driver')
    return Driver.objects.create(user_id=user, home_city=city, approved=True)


def make_trip(rider_user, driver, zone, status='completed', key=None):
    st, _ = TripStatus.objects.get_or_create(status_code=status)
    return Trip.objects.create(
        user_id=rider_user, driver_id=driver, status_id=st, zone=zone,
        client_request_id=key,
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.3850000'), destination_long=Decimal('78.4860000'),
        estimated_fare_minor=13568,
    )


@pytest.fixture
def hyderabad(geo):
    """One real-looking driver and one trip in Hyderabad."""
    rider = User.objects.create_user(phone_number='+919000000100', username='+919000000100',
                                     role='rider', full_name='A Hyderabad Rider')
    Rider.objects.create(user_id=rider)
    driver = make_driver('+919000000101', geo['IN-TG-HYD'])
    trip = make_trip(rider, driver, hyd_zone())
    return {'rider': rider, 'driver': driver, 'trip': trip}


def admin_client(email, phone, role_code, scope):
    """An operator with exactly one assignment, and a client signed in as them."""
    user = User.objects.create_user(phone_number=phone, username=phone, email=email,
                                    role='admin', is_staff=True)
    AdminAssignment.objects.create(
        admin=user, role=AdminRole.objects.get(code=role_code), scope=scope,
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


@pytest.fixture
def hyd_ops(geo):
    return admin_client('hyd.ops@saaradhigo.test', '+918800009001',
                        'city_ops_manager', geo['IN-TG-HYD'])


@pytest.fixture
def super_admin(geo):
    return admin_client('sa@saaradhigo.test', '+918800009002',
                        'super_admin', geo['GLOBAL'])


def listed_ids(response):
    """Ids out of the project's paginated envelope."""
    assert response.status_code == 200, response.content
    body = response.json()
    payload = body.get('data', body)
    results = payload.get('results', payload)
    if isinstance(results, dict):
        results = results.get('results', [])
    return {row['id'] for row in results}


def error_code(response):
    return response.json().get('error', {}).get('code')


# ===========================================================================
# Refusals, before any write
# ===========================================================================

class TestRefusals:
    def test_refused_in_production_before_any_write(self, geo, settings):
        settings.IS_PRODUCTION = True
        before, rows = fingerprint(), AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match='production'):
            run()

        assert fingerprint() == before
        assert AdminAuditLog.objects.count() == rows
        assert not ServiceZone.objects.filter(code=seed.ZONE_CODE).exists()

    def test_remove_is_refused_in_production_too(self, seeded, settings):
        settings.IS_PRODUCTION = True
        before = fingerprint()

        with pytest.raises(CommandError, match='production'):
            run('--remove')

        assert fingerprint() == before

    def test_the_environment_variable_alone_refuses(self, geo, settings, monkeypatch):
        """Same rule as `seed_admin_access --test-admins`."""
        settings.IS_PRODUCTION = False
        monkeypatch.setenv('ENVIRONMENT', 'production')
        before = fingerprint()

        with pytest.raises(CommandError, match='production'):
            run()

        assert fingerprint() == before

    def test_refused_when_otp_gating_is_off(self, geo, settings):
        settings.TEST_PHONE_NUMBERS = {}

        with pytest.raises(CommandError, match='TEST_PHONE_NUMBERS'):
            run()

    def test_refused_without_the_geo_node(self):
        """The tree belongs to seed_admin_access; this never grows it."""
        seed_actor()
        assert not GeoNode.objects.filter(code=seed.ZONE_CODE).exists()

        with pytest.raises(CommandError, match='seed_admin_access') as exc:
            run()

        assert 'Nothing was changed' in str(exc.value)
        assert not ServiceZone.objects.filter(code=seed.ZONE_CODE).exists()
        assert not User.objects.filter(phone_number__startswith='+1512').exists()

    def test_refused_without_an_actor(self, geo):
        before = fingerprint()

        with pytest.raises(CommandError, match='--actor-email'):
            run(actor=None)

        assert fingerprint() == before


# ===========================================================================
# What it creates, and where scope will find it
# ===========================================================================

class TestWhatItCreates:
    def test_the_zone_is_the_paused_us_city_linked_to_its_node(self, seeded, geo):
        (zone,) = seeded['zone']

        assert zone.code == 'US-TX-AUS'
        assert zone.name == 'Austin (QA US seed)'
        assert zone.country == 'US'
        assert zone.state_code == 'TX'
        assert zone.currency == 'USD'
        assert zone.distance_unit == 'mi'
        assert zone.timezone_name == 'America/Chicago'
        assert zone.is_active is False, 'a seed zone must never be quoted'
        # THE scope anchor for trips.
        assert zone.geo_node_id == geo['US-TX-AUS'].pk
        assert geo['US-TX-AUS'].status == 'paused'

    def test_two_drivers_anchored_in_austin(self, seeded, geo):
        drivers = seeded['drivers']

        assert len(drivers) == 2
        for driver in drivers:
            # THE scope anchor for drivers.
            assert driver.home_city_id == geo['US-TX-AUS'].pk
            assert driver.approved is True
            assert driver.doc_status == 'approved'
            assert driver.active_vehicle_id is not None
            assert driver.active_vehicle.driver_id_id == driver.pk
            account = driver.user_id
            assert account.role == 'driver'
            assert account.phone_number.startswith('+1512555')
            assert account.email.endswith('@saaradhigo.test')
            assert '(QA US seed)' in account.full_name
            assert not account.has_usable_password()

    def test_one_rider(self, seeded):
        (rider,) = seeded['rider']

        assert rider.user_id.role == 'rider'
        assert rider.user_id.phone_number == '+15125550199'
        assert '(QA US seed)' in rider.user_id.full_name
        assert not rider.user_id.has_usable_password()

    def test_two_trips_in_the_austin_zone_in_usd_cents(self, seeded):
        (zone,) = seeded['zone']
        trips = {t.status_id.status_code: t for t in seeded['trips']}

        assert set(trips) == {'completed', 'cancelled'}
        for trip in trips.values():
            assert trip.zone_id == zone.pk
            assert trip.currency == 'USD'
            assert isinstance(trip.estimated_fare_minor, int)
            assert trip.user_id.phone_number == '+15125550199'
            assert trip.driver_id.home_city.code == 'US-TX-AUS'
            assert trip.vehicle_id is not None
            # Inside the Austin box, both ends.
            for lat, lng in ((trip.pickup_lat, trip.pickup_long),
                             (trip.destination_lat, trip.destination_long)):
                assert Decimal('30.12') < lat < Decimal('30.52')
                assert Decimal('-97.95') < lng < Decimal('-97.55')

        done = trips['completed']
        assert done.estimated_fare_minor == 2450
        assert done.estimated_fare == Decimal('24.50')
        assert done.completed_at is not None and done.started_at is not None
        assert done.payment_status == 'completed'
        assert done.driver_id.total_trips == 1

        gone = trips['cancelled']
        assert gone.estimated_fare_minor == 1875
        assert gone.cancelled_at is not None and gone.completed_at is None
        assert gone.cancelled_by == 'rider'
        assert 'QA US seed' in gone.cancellation_reason
        assert gone.driver_id.total_trips == 0

    def test_the_completed_trip_has_a_fare_breakdown(self, seeded):
        (breakdown,) = seeded['fare_pricing']

        assert breakdown.trip_id.status_id.status_code == 'completed'
        assert breakdown.currency == 'USD'
        assert breakdown.total_fare_minor == 2450
        assert (breakdown.base_fare_minor + breakdown.distance_fare_minor
                + breakdown.time_fare_minor) == breakdown.total_fare_minor

    def test_no_rate_card(self, seeded):
        """Nothing in the console needs one, so none is left to explain."""
        from servers.pricing.models import RateCard

        assert not RateCard.objects.filter(zone__code=seed.ZONE_CODE).exists()

    def test_the_output_names_what_was_created(self, geo):
        out = run()
        rows = seed.find_seed_rows()

        assert 'created' in out
        for kind in ('zone', 'drivers', 'rider', 'trips', 'vehicles'):
            ids = [obj.pk for obj in rows[kind]]
            assert f'{kind} {ids}' in out, out


# ===========================================================================
# Audited once per run, by the operator who ran it
# ===========================================================================

class TestTheAuditRow:
    def test_one_summary_row_naming_the_actor_and_the_ids(self, geo):
        actor = seed_actor()
        count = AdminAuditLog.objects.count()

        run()

        rows = rows_since(count)
        assert len(rows) == 1, [r.action for r in rows]
        row = rows[0]
        assert row.action == seed.SEED_ACTION
        assert row.target_type == seed.TARGET_TYPE
        assert row.target_id == 'US-TX-AUS'
        assert row.actor_id == actor.pk
        assert row.actor_email == SEED_ACTOR_EMAIL
        assert row.actor_label == f'{SEED_ACTOR_EMAIL} via seed_us_test_city'
        assert row.after['label'] == 'QA US seed'
        found = seed.find_seed_rows()
        for kind in ('zone', 'drivers', 'rider', 'trips', 'vehicles', 'users', 'fare_pricing'):
            assert row.after['created'][kind] == [obj.pk for obj in found[kind]], kind
        assert all(ids == [] for ids in row.after['already_present'].values())

    def test_the_seed_rows_are_not_access_rows_so_nothing_else_is_written(self, geo):
        count = AdminAuditLog.objects.count()

        run()

        assert AdminAuditLog.objects.count() == count + 1


# ===========================================================================
# Idempotent
# ===========================================================================

class TestSecondRun:
    def test_creates_nothing_and_says_so(self, seeded):
        before = fingerprint()
        count = AdminAuditLog.objects.count()

        out = run()

        assert fingerprint() == before
        assert 'nothing created' in out
        assert 'already present' in out
        rows = rows_since(count)
        assert len(rows) == 1
        assert all(ids == [] for ids in rows[0].after['created'].values())
        assert rows[0].after['already_present']['trips'] == [
            t.pk for t in seeded['trips']]

    def test_a_half_present_seed_is_completed_not_duplicated(self, seeded):
        """Delete one trip by hand; the next run puts back only that one."""
        gone = [t for t in seeded['trips'] if t.status_id.status_code == 'cancelled'][0]
        gone.delete()
        before = fingerprint()

        run()

        after = seed.find_seed_rows()
        assert len(after['trips']) == 2
        assert Trip.objects.count() == before[4] + 1
        assert User.objects.count() == len(before[0])


# ===========================================================================
# THE PROOF: a Hyderabad role cannot see Austin, Super Admin can
# ===========================================================================

class TestHyderabadScopedOperator:
    def test_the_trip_list_is_exactly_hyderabad(self, seeded, hyderabad, hyd_ops):
        ids = listed_ids(hyd_ops.get(TRIPS_URL))

        assert ids == {hyderabad['trip'].id}
        assert not ids & {t.id for t in seeded['trips']}

    def test_the_driver_list_is_exactly_hyderabad(self, seeded, hyderabad, hyd_ops):
        ids = listed_ids(hyd_ops.get(DRIVERS_URL))

        assert ids == {hyderabad['driver'].id}
        assert not ids & {d.id for d in seeded['drivers']}

    @pytest.mark.parametrize('status', ['completed', 'cancelled'])
    def test_an_austin_trip_by_id_is_404_never_403(self, seeded, hyderabad, hyd_ops, status):
        trip = [t for t in seeded['trips'] if t.status_id.status_code == status][0]

        out_of_scope = hyd_ops.get(f'{TRIPS_URL}{trip.id}/')
        never_existed = hyd_ops.get(f'{TRIPS_URL}999999/')

        assert out_of_scope.status_code == 404
        assert out_of_scope.status_code != 403
        # Indistinguishable from a trip that does not exist.
        assert never_existed.status_code == 404
        assert error_code(out_of_scope) == error_code(never_existed) == 'NOT_FOUND'
        assert out_of_scope.json()['error']['message'] == \
            never_existed.json()['error']['message']

    def test_an_austin_driver_by_id_is_404_never_403(self, seeded, hyderabad, hyd_ops):
        for driver in seeded['drivers']:
            for suffix in ('', 'full/', 'vehicles/'):
                response = hyd_ops.get(f'{DRIVERS_URL}{driver.id}/{suffix}')
                assert response.status_code == 404, (suffix, response.content)
                assert error_code(response) == 'NOT_FOUND'
        never_existed = hyd_ops.get(f'{DRIVERS_URL}999999/')
        assert never_existed.status_code == 404
        assert error_code(never_existed) == 'NOT_FOUND'

    def test_the_same_operator_can_open_hyderabad_rows(self, seeded, hyderabad, hyd_ops):
        """So the 404s above are scope, not a broken route."""
        assert hyd_ops.get(f'{TRIPS_URL}{hyderabad["trip"].id}/').status_code == 200
        assert hyd_ops.get(f'{DRIVERS_URL}{hyderabad["driver"].id}/').status_code == 200

    def test_a_missing_capability_is_still_403(self, seeded, hyderabad, geo):
        """404 is for scope. The wrong ROLE in the right place stays a 403,
        because that answer leaks nothing about any particular row."""
        marketing = admin_client('mk@saaradhigo.test', '+918800009003',
                                 'marketing', geo['IN-TG-HYD'])

        assert marketing.get(TRIPS_URL).status_code == 403
        assert marketing.get(f'{TRIPS_URL}{hyderabad["trip"].id}/').status_code == 403

    def test_the_scope_switcher_cannot_reach_austin(self, seeded, hyderabad, hyd_ops):
        response = hyd_ops.get(f'{TRIPS_URL}?scope=US-TX-AUS')

        assert response.status_code == 403
        assert error_code(response) == 'SCOPE_NOT_HELD'


class TestSuperAdmin:
    def test_sees_austin_and_hyderabad_in_both_lists(self, seeded, hyderabad, super_admin):
        trip_ids = listed_ids(super_admin.get(TRIPS_URL))
        driver_ids = listed_ids(super_admin.get(DRIVERS_URL))

        assert {t.id for t in seeded['trips']} | {hyderabad['trip'].id} <= trip_ids
        assert {d.id for d in seeded['drivers']} | {hyderabad['driver'].id} <= driver_ids

    def test_the_trip_list_renders_the_austin_rows_in_usd(self, seeded, super_admin):
        body = super_admin.get(TRIPS_URL).json()
        payload = body.get('data', body)
        results = payload.get('results', payload)
        rows = {row['id']: row for row in results}

        for trip in seeded['trips']:
            row = rows[trip.id]
            assert row['currency'] == 'USD'
            assert row['status'] == trip.status_id.status_code
            assert row['estimated_fare'] == str(trip.estimated_fare)
            assert row['driver_name'] and 'QA US seed' in row['driver_name']
            assert row['vehicle_info']['vehicle_number'].startswith('QAUS')
            assert 'Austin' in row['pickup_address']
        done = [t for t in seeded['trips'] if t.status_id.status_code == 'completed'][0]
        assert rows[done.id]['estimated_fare'] == '24.50'

    def test_opens_every_austin_row(self, seeded, super_admin):
        for trip in seeded['trips']:
            response = super_admin.get(f'{TRIPS_URL}{trip.id}/')
            assert response.status_code == 200, response.content
            data = response.json()['data']
            assert data['driver']['driver_id'] == trip.driver_id_id
            assert 'QA US seed' in data['rider']['full_name']
            assert 'Austin' in data['pickup']['address']
        done = [t for t in seeded['trips'] if t.status_id.status_code == 'completed'][0]
        assert super_admin.get(f'{TRIPS_URL}{done.id}/').json()['data']['fare']['breakdown']['total_fare'] == '24.50'
        for driver in seeded['drivers']:
            for suffix in ('', 'full/', 'vehicles/'):
                response = super_admin.get(f'{DRIVERS_URL}{driver.id}/{suffix}')
                assert response.status_code == 200, (suffix, response.content)


# ===========================================================================
# --remove takes back exactly what the seed made
# ===========================================================================

class TestRemove:
    def test_removes_only_the_seed_rows(self, seeded, hyderabad, geo):
        # A stranger who happens to live in Austin references the NODE, not
        # the seed zone, and must survive.
        local = make_driver('+919000000102', geo['US-TX-AUS'])
        actor = seed_actor()
        count = AdminAuditLog.objects.count()
        expected = {kind: [obj.pk for obj in objs] for kind, objs in seeded.items()}

        out = run('--remove')

        assert 'removed' in out
        empty = seed.find_seed_rows()
        assert all(objs == [] for objs in empty.values()), empty
        assert not ServiceZone.objects.filter(code='US-TX-AUS').exists()
        assert not User.objects.filter(phone_number__startswith='+1512555').exists()
        # Not touched: the Hyderabad rows, the stranger, the geo tree.
        assert Trip.objects.filter(pk=hyderabad['trip'].pk).exists()
        assert Driver.objects.filter(pk=hyderabad['driver'].pk).exists()
        assert Driver.objects.filter(pk=local.pk).exists()
        assert GeoNode.objects.filter(code='US-TX-AUS').exists()
        assert ServiceZone.objects.filter(code='IN-TG-HYD').exists()
        # One summary row, naming the actor and what went.
        rows = rows_since(count)
        assert len(rows) == 1
        assert rows[0].action == seed.REMOVE_ACTION
        assert rows[0].actor_id == actor.pk
        assert rows[0].actor_label == f'{SEED_ACTOR_EMAIL} via seed_us_test_city'
        assert rows[0].after['removed'] == expected

    def test_a_second_remove_finds_nothing_and_says_so(self, seeded):
        run('--remove')
        before = fingerprint()

        out = run('--remove')

        assert 'nothing removed' in out
        assert fingerprint() == before

    def test_seed_after_remove_recreates_everything(self, seeded):
        run('--remove')

        run()

        again = seed.find_seed_rows()
        assert len(again['trips']) == 2 and len(again['drivers']) == 2
        assert len(again['zone']) == 1

    def test_refuses_when_a_stranger_trip_is_in_the_austin_zone(self, seeded, hyderabad):
        """A trip somebody booked against the seed zone would lose its zone
        (SET_NULL) if the zone went. Refused, before anything is deleted."""
        (zone,) = seeded['zone']
        stranger = make_trip(hyderabad['rider'], hyderabad['driver'], zone)
        before = fingerprint()
        count = AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match='did not create') as exc:
            run('--remove')

        assert f'ride.Trip #{stranger.pk}' in str(exc.value)
        assert 'Nothing was deleted' in str(exc.value)
        assert fingerprint() == before
        assert AdminAuditLog.objects.count() == count

    def test_refuses_when_a_stranger_trip_names_a_seed_driver(self, seeded, hyderabad):
        """`Trip.driver_id` is DO_NOTHING: the database would refuse the
        driver's delete half-way. Refused up front instead."""
        driver = seeded['drivers'][0]
        stranger = make_trip(hyderabad['rider'], driver, hyd_zone())
        before = fingerprint()

        with pytest.raises(CommandError, match='did not create') as exc:
            run('--remove')

        assert f'ride.Trip #{stranger.pk}' in str(exc.value)
        assert fingerprint() == before

    def test_refuses_when_a_withdrawal_hangs_off_a_seed_driver(self, seeded):
        """CASCADE would take it silently. Money rows are never taken silently."""
        driver = seeded['drivers'][1]
        withdrawal = WithdrawalRequest.objects.create(
            driver=driver, amount_minor=5000, currency='USD')
        before = fingerprint()

        with pytest.raises(CommandError, match='did not create') as exc:
            run('--remove')

        assert f'driver.WithdrawalRequest #{withdrawal.pk}' in str(exc.value)
        assert fingerprint() == before
        assert WithdrawalRequest.objects.filter(pk=withdrawal.pk).exists()

    def test_refuses_when_a_stranger_vehicle_was_added_to_a_seed_driver(self, seeded):
        driver = seeded['drivers'][0]
        extra = Vehicle.objects.create(
            driver_id=driver, vehicle_type_id=driver.active_vehicle.vehicle_type_id,
            vehicle_number='TX-REAL-01')
        before = fingerprint()

        with pytest.raises(CommandError, match='did not create') as exc:
            run('--remove')

        assert f'driver.Vehicle #{extra.pk}' in str(exc.value)
        assert fingerprint() == before


# ===========================================================================
# The command's shape
# ===========================================================================

def test_it_takes_the_actor_flag_and_the_remove_flag():
    from django.core.management import load_command_class

    parser = load_command_class('servers.admin_access', 'seed_us_test_city').create_parser(
        'manage.py', 'seed_us_test_city')
    flags = {flag for action in parser._actions for flag in action.option_strings}

    assert {'--actor-email', '--remove'} <= flags


def test_the_seed_phone_numbers_are_fictional():
    """512-555-01xx is the NANP block reserved for fiction; nobody holds these."""
    for spec in seed.DRIVERS + [seed.RIDER]:
        assert spec['phone'].startswith('+1512555')
        assert spec['phone'][8:10] == '01'
        assert spec['email'].endswith('@saaradhigo.test')
        assert 'QA US seed' in spec['name']
