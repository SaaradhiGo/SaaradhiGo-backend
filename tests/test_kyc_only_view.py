"""KYC sees only KYC queues (owner, QA survey 2026-10-28).

The `kyc_verifier` template holds `driver.read`, `driver.kyc` and `pii.view`
and no longer `trip.read`. An operator whose driver authority is KYC alone
(`driver.kyc` without `driver.write` or `trip.read`) gets:

  * the driver list narrowed to drivers still in KYC -- not yet approved,
    or documents pending or rejected;
  * NOT FOUND for an already-approved driver's detail, full detail and
    vehicles, the same answer as a driver outside their city;
  * 403 on the trip list, and on the trip-gated dashboard.

City Ops (holds `driver.write` and `trip.read`) is unchanged.
"""

import pytest
from django.contrib.auth import get_user_model

from servers.admin_access.roles import ROLE_TEMPLATES
from servers.driver.admin_views import kyc_only
from servers.driver.models import Driver
from servers.ride.models import Trip
from test_sos_support_zones_matrix import (
    _client_for, _rider, _trip, _zone, geo, ids_in, operator,  # noqa: F401 - fixtures
)

pytestmark = pytest.mark.django_db

User = get_user_model()
DRIVERS = '/api/v1/driver/admin/'
TRIPS = '/api/v1/ride/admin/trips/'
DASHBOARD = '/api/v1/ride/admin/dashboard/'

_n = iter(range(1, 9999))


def _driver(city, **state):
    i = next(_n)
    phone = f'+9198770{i:05d}'
    user = User.objects.create_user(phone_number=phone, username=phone,
                                    full_name=f'Driver {i}', role='driver')
    return Driver.objects.create(user_id=user, home_city=city, **state)


@pytest.fixture
def drivers(geo):
    hyd = geo['IN-TG-HYD']
    return {
        'pending': _driver(hyd),                                        # approved=False, docs pending
        'rejected': _driver(hyd, doc_status='rejected'),
        'approved': _driver(hyd, approved=True, doc_status='approved'),
        # Approved once, documents back under review (a renewal): KYC's again.
        'reupload': _driver(hyd, approved=True, doc_status='pending'),
        'austin_pending': _driver(geo['US-TX-AUS']),
    }


def _ids(response):
    body = response.json()
    payload = body.get('data', body)
    results = payload.get('results', payload)
    return {row['id'] for row in results}


class TestTheTemplate:
    def test_kyc_verifier_holds_no_trip_read(self):
        kyc = next(t for t in ROLE_TEMPLATES if t['code'] == 'kyc_verifier')
        assert set(kyc['permissions']) == {'driver.read', 'driver.kyc', 'pii.view'}

    @pytest.mark.parametrize('role, expected', [
        ('kyc_verifier', True),
        ('city_ops_manager', False),      # driver.write
        ('safety_officer', False),        # driver.write
        ('super_admin', False),
        ('support_agent', False),         # no driver.kyc at all
    ])
    def test_kyc_only_is_exactly_the_kyc_verifier(self, geo, role, expected):
        _, user = operator(role, geo)
        assert kyc_only(user) is expected


class TestKycVerifier:
    @pytest.fixture
    def kyc(self, geo):
        return operator('kyc_verifier', geo)[0]

    def test_the_list_is_the_kyc_queue_of_their_city(self, kyc, drivers):
        ids = _ids(kyc.get(DRIVERS))

        assert {drivers['pending'].id, drivers['rejected'].id, drivers['reupload'].id} <= ids
        assert drivers['approved'].id not in ids
        assert drivers['austin_pending'].id not in ids          # scope still applies

    def test_an_approved_driver_is_not_found_to_them(self, kyc, drivers):
        approved = drivers['approved'].id
        assert kyc.get(f'{DRIVERS}{approved}/').status_code == 404
        assert kyc.get(f'{DRIVERS}{approved}/full/').status_code == 404
        assert kyc.get(f'{DRIVERS}{approved}/vehicles/').status_code == 404
        # Same answer as a driver that does not exist: nothing to enumerate.
        missing = kyc.get(f'{DRIVERS}999999/')
        assert missing.status_code == 404
        assert kyc.get(f'{DRIVERS}{approved}/').json()['error']['code'] == missing.json()['error']['code']

    def test_a_pending_driver_is_theirs_to_review(self, kyc, drivers):
        pending = drivers['pending'].id
        assert kyc.get(f'{DRIVERS}{pending}/').status_code == 200
        assert kyc.get(f'{DRIVERS}{pending}/full/').status_code == 200

        decided = kyc.patch(f'{DRIVERS}{pending}/update-kyc/', {'approved': True}, format='json')
        assert decided.status_code not in (401, 403, 404), decided.content[:200]

    def test_no_trips_and_no_trip_dashboard(self, kyc, geo, drivers):
        rider = _rider('+919876700001', 'Some Rider')
        _trip(rider, _zone('IN-TG-HYD', geo['IN-TG-HYD']), drivers['approved'])
        assert Trip.objects.exists()

        assert kyc.get(TRIPS).status_code == 403
        assert kyc.get(DASHBOARD).status_code == 403


class TestCityOpsIsUnchanged:
    @pytest.fixture
    def city_ops(self, geo):
        return operator('city_ops_manager', geo)[0]

    def test_sees_every_driver_in_their_city(self, city_ops, drivers):
        ids = _ids(city_ops.get(DRIVERS))

        assert {drivers['pending'].id, drivers['rejected'].id,
                drivers['approved'].id, drivers['reupload'].id} <= ids
        assert drivers['austin_pending'].id not in ids

    def test_opens_an_approved_driver_and_reads_trips(self, city_ops, drivers):
        approved = drivers['approved'].id
        assert city_ops.get(f'{DRIVERS}{approved}/').status_code == 200
        assert city_ops.get(f'{DRIVERS}{approved}/full/').status_code == 200
        assert city_ops.get(TRIPS).status_code == 200
        assert city_ops.get(DASHBOARD).status_code == 200


class TestTheLegacyBridge:
    def test_an_unmigrated_driver_ops_account_is_not_narrowed_yet(self, drivers, settings):
        """Legacy `driver_ops` still holds `trip.read` through the bridge, so
        the KYC-only view waits for the account to be migrated -- the
        bridge's contract everywhere else."""
        settings.ADMIN_ACCESS_STRICT = False
        phone = '+919800098001'
        user = User.objects.create_user(phone_number=phone, username=phone, role='admin',
                                        is_staff=True, ops_role='driver_ops')

        assert kyc_only(user) is False
        assert drivers['approved'].id in _ids(_client_for(user).get(DRIVERS))
