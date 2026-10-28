"""Cross-country denial over real HTTP, in both directions.

The unit tests in `test_admin_geo_scope.py` prove the helpers. These prove
the ENDPOINTS, because the gap between the two is where this class of bug
lives: a correct `scope_queryset` that some view forgot to call.

So every assertion here goes through the URL conf with a JWT, exactly as
the console does, and checks the status code and the body.

WHAT "DENIED" HAS TO LOOK LIKE
------------------------------
404, never 403. A 403 confirms the row exists, which lets an out-of-scope
admin walk ids to count another country's drivers. A denied read and a
non-existent row must be indistinguishable.

SCOPE APPLIES ONLY TO MIGRATED ACCOUNTS
---------------------------------------
While `ADMIN_ACCESS_STRICT` is False an account with no assignment keeps
today's unscoped access, so that migrating operators one at a time does not
black out the console. These tests therefore give every admin an explicit
assignment — and `TestTheMigrationBridge` covers the unmigrated case, which
is just as important to get right.
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.driver.models import Driver

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


def admin_client(email, phone, role_code=None, scope=None):
    """An operator, optionally with one assignment, and a client for them."""
    user = User.objects.create_user(
        phone_number=phone, username=phone, email=email,
        role='admin', is_staff=True,
    )
    if role_code:
        AdminAssignment.objects.create(
            admin=user, role=AdminRole.objects.get(code=role_code),
            scope=scope,
        )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client, user


def make_driver(phone, city):
    user = User.objects.create_user(
        phone_number=phone, username=phone, role='driver',
    )
    return Driver.objects.create(user_id=user, home_city=city)


@pytest.fixture
def two_drivers(geo):
    """One driver in Hyderabad, one in Austin."""
    return {
        'hyd': make_driver('+919000000001', geo['IN-TG-HYD']),
        'aus': make_driver('+919000000002', geo['US-TX-AUS']),
    }


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

class TestTheDriverList:
    def test_an_india_admin_sees_only_the_indian_driver(self, geo, two_drivers):
        client, _ = admin_client(
            'in@saaradhigo.test', '+918800001001', 'city_ops_manager', geo['IN'],
        )

        response = client.get('/api/v1/driver/admin/')

        assert response.status_code == 200
        ids = _driver_ids(response)
        assert two_drivers['hyd'].id in ids
        assert two_drivers['aus'].id not in ids

    def test_a_us_admin_sees_only_the_us_driver(self, geo, two_drivers):
        client, _ = admin_client(
            'us@saaradhigo.test', '+918800001002', 'city_ops_manager', geo['US'],
        )

        response = client.get('/api/v1/driver/admin/')

        assert response.status_code == 200
        ids = _driver_ids(response)
        assert two_drivers['aus'].id in ids
        assert two_drivers['hyd'].id not in ids

    def test_a_city_admin_sees_only_their_city(self, geo, two_drivers):
        other = make_driver('+919000000003', geo['IN-AP-VJA'])
        client, _ = admin_client(
            'hyd@saaradhigo.test', '+918800001003',
            'city_ops_manager', geo['IN-TG-HYD'],
        )

        response = client.get('/api/v1/driver/admin/')

        ids = _driver_ids(response)
        assert two_drivers['hyd'].id in ids
        assert other.id not in ids

    def test_a_global_super_admin_sees_both(self, geo, two_drivers):
        client, _ = admin_client(
            'sa@saaradhigo.test', '+918800001004', 'super_admin', geo['GLOBAL'],
        )

        response = client.get('/api/v1/driver/admin/')

        ids = _driver_ids(response)
        assert two_drivers['hyd'].id in ids
        assert two_drivers['aus'].id in ids


def _driver_ids(response):
    """Pull driver ids out of the project's paginated envelope."""
    body = response.json()
    payload = body.get('data', body)
    results = payload.get('results', payload)
    if isinstance(results, dict):
        results = results.get('results', [])
    return {row['id'] for row in results}


# ---------------------------------------------------------------------------
# Detail reads — the id-walking attack
# ---------------------------------------------------------------------------

class TestReadingOneDriverByItsId:
    """The list being filtered is not enough if the detail route is open."""

    @pytest.fixture
    def india_client(self, geo):
        client, _ = admin_client(
            'in2@saaradhigo.test', '+918800002001',
            'city_ops_manager', geo['IN'],
        )
        return client

    def test_an_indian_admin_cannot_open_the_us_driver(
        self, india_client, two_drivers,
    ):
        response = india_client.get(f'/api/v1/driver/admin/{two_drivers["aus"].id}/')

        assert response.status_code == 404

    def test_the_same_admin_can_open_the_indian_driver(
        self, india_client, two_drivers,
    ):
        """So the 404 above is scope, not a broken route."""
        response = india_client.get(f'/api/v1/driver/admin/{two_drivers["hyd"].id}/')

        assert response.status_code == 200

    def test_the_denial_is_404_and_not_403(self, india_client, two_drivers):
        """A 403 would confirm the driver exists. See `scoped_or_none`."""
        out_of_scope = india_client.get(
            f'/api/v1/driver/admin/{two_drivers["aus"].id}/'
        )
        never_existed = india_client.get('/api/v1/driver/admin/99999/')

        assert out_of_scope.status_code == never_existed.status_code == 404
        # And the bodies must not differ either.
        assert out_of_scope.json().get('error', {}).get('code') == \
            never_existed.json().get('error', {}).get('code')

    def test_the_full_detail_endpoint_is_scoped_too(
        self, india_client, two_drivers,
    ):
        """This one serves licence images and earnings. It was unscoped, and
        the structural test in test_admin_api_is_scoped.py caught it."""
        response = india_client.get(
            f'/api/v1/driver/admin/{two_drivers["aus"].id}/full/'
        )

        assert response.status_code == 404

    def test_the_vehicle_documents_endpoint_is_scoped_too(
        self, india_client, two_drivers,
    ):
        response = india_client.get(
            f'/api/v1/driver/admin/{two_drivers["aus"].id}/vehicles/'
        )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Writes — the ones that cost money or a livelihood
# ---------------------------------------------------------------------------

class TestWrites:
    def test_a_us_kyc_verifier_cannot_approve_an_indian_driver(
        self, geo, two_drivers,
    ):
        client, _ = admin_client(
            'uskyc@saaradhigo.test', '+918800003001',
            'kyc_verifier', geo['US-TX'],
        )

        response = client.patch(
            f'/api/v1/driver/admin/{two_drivers["hyd"].id}/update-kyc/',
            {'approved': True}, format='json',
        )

        assert response.status_code == 404
        two_drivers['hyd'].refresh_from_db()
        assert two_drivers['hyd'].approved is False

    def test_an_indian_kyc_verifier_cannot_deactivate_a_us_driver(
        self, geo, two_drivers,
    ):
        client, _ = admin_client(
            'inkyc@saaradhigo.test', '+918800003002',
            'city_ops_manager', geo['IN'],
        )

        response = client.delete(
            f'/api/v1/driver/admin/{two_drivers["aus"].id}/delete/'
        )

        assert response.status_code in (403, 404)
        assert Driver.objects.filter(id=two_drivers['aus'].id).exists()

    def test_a_support_agent_cannot_approve_kyc_in_their_own_city(
        self, geo, two_drivers,
    ):
        """Scope is not authority. Right place, wrong role."""
        client, _ = admin_client(
            'sup@saaradhigo.test', '+918800003003',
            'support_agent', geo['IN-TG-HYD'],
        )

        response = client.patch(
            f'/api/v1/driver/admin/{two_drivers["hyd"].id}/update-kyc/',
            {'approved': True}, format='json',
        )

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------

class TestTheUserList:
    def test_an_analyst_is_refused(self, geo):
        """The Analyst role must never reach personal data."""
        client, _ = admin_client(
            'an@saaradhigo.test', '+918800004001', 'analyst', geo['IN-TG-HYD'],
        )

        response = client.get('/api/v1/auth/admin/users/')

        assert response.status_code == 403

    def test_a_city_ops_manager_is_allowed(self, geo):
        client, _ = admin_client(
            'co@saaradhigo.test', '+918800004002',
            'city_ops_manager', geo['IN-TG-HYD'],
        )

        response = client.get('/api/v1/auth/admin/users/')

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# The migration bridge
# ---------------------------------------------------------------------------

class TestTheMigrationBridge:
    """An unmigrated operator must keep working, and must be logged.

    If this breaks, deploying the scope model blacks out the console for
    everybody until the last operator has been migrated by hand.
    """

    def test_an_unmigrated_operator_still_sees_everything(
        self, geo, two_drivers, settings,
    ):
        # Pinned, not inherited: this test is ABOUT the non-strict path,
        # so it must say so rather than quietly invert when the
        # environment sets ADMIN_ACCESS_STRICT.
        settings.ADMIN_ACCESS_STRICT = False
        client, _ = admin_client('old@saaradhigo.test', '+918800005001')

        response = client.get('/api/v1/driver/admin/')

        assert response.status_code == 200
        ids = _driver_ids(response)
        assert two_drivers['hyd'].id in ids
        assert two_drivers['aus'].id in ids

    def test_the_unscoped_grant_is_logged(
        self, geo, two_drivers, caplog, settings,
    ):
        settings.ADMIN_ACCESS_STRICT = False
        """The log volume IS the migration's progress bar."""
        client, _ = admin_client('old2@saaradhigo.test', '+918800005002')

        with caplog.at_level('WARNING'):
            client.get('/api/v1/driver/admin/')

        assert any(
            'UNSCOPED' in r.message or 'LEGACY' in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_strict_mode_closes_the_legacy_path(
        self, geo, two_drivers, settings,
    ):
        """The end state: no assignment means no access."""
        settings.ADMIN_ACCESS_STRICT = True
        client, _ = admin_client('old3@saaradhigo.test', '+918800005003')

        response = client.get('/api/v1/driver/admin/')

        assert response.status_code == 403

    def test_strict_mode_leaves_migrated_admins_working(
        self, geo, two_drivers, settings,
    ):
        settings.ADMIN_ACCESS_STRICT = True
        client, _ = admin_client(
            'new@saaradhigo.test', '+918800005004',
            'city_ops_manager', geo['IN'],
        )

        response = client.get('/api/v1/driver/admin/')

        assert response.status_code == 200
        ids = _driver_ids(response)
        assert two_drivers['hyd'].id in ids
        assert two_drivers['aus'].id not in ids


# ---------------------------------------------------------------------------
# The scope switcher
# ---------------------------------------------------------------------------

class TestWhoamiAndTheSwitcher:
    """`/admin-access/me/` is what lets the console draw itself honestly."""

    def test_it_reports_the_roles_and_scopes_held(self, geo):
        client, _ = admin_client(
            'who@saaradhigo.test', '+918800006001',
            'city_ops_manager', geo['IN-TG'],
        )

        body = client.get('/api/v1/admin-access/me/').json()['data']

        assert body['roles'] == [{
            'role': 'city_ops_manager', 'role_name': 'City Ops Manager',
            'scope': 'IN-TG', 'scope_name': 'Telangana', 'scope_kind': 'state',
        }]
        assert 'driver.write' in body['permissions']
        assert 'finance.payout' not in body['permissions']

    def test_the_switcher_offers_the_scope_and_its_children(self, geo):
        client, _ = admin_client(
            'who2@saaradhigo.test', '+918800006002',
            'city_ops_manager', geo['IN-TG'],
        )

        body = client.get('/api/v1/admin-access/me/').json()['data']
        codes = {s['code'] for s in body['scopes']}

        assert codes == {'IN-TG', 'IN-TG-HYD', 'IN-TG-WGL'}

    def test_it_offers_nothing_outside_the_assignment(self, geo):
        client, _ = admin_client(
            'who3@saaradhigo.test', '+918800006003',
            'city_ops_manager', geo['IN-TG'],
        )

        codes = {
            s['code']
            for s in client.get('/api/v1/admin-access/me/').json()['data']['scopes']
        }

        assert 'US' not in codes
        assert 'IN-AP' not in codes
        assert 'GLOBAL' not in codes

    def test_it_says_when_an_account_is_unmigrated(self, geo, settings):
        settings.ADMIN_ACCESS_STRICT = False
        """So the console can say "unscoped" rather than imply a scope that
        is not being enforced."""
        client, _ = admin_client('who4@saaradhigo.test', '+918800006004')

        body = client.get('/api/v1/admin-access/me/').json()['data']

        assert body['access_model'] == 'legacy_unscoped'
        assert body['scopes'] == []

    def test_a_rider_cannot_ask(self, geo):
        rider = User.objects.create_user(
            phone_number='+919111111111', username='+919111111111',
            role='rider',
        )
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(rider)}'
        )

        assert client.get('/api/v1/admin-access/me/').status_code == 403


class TestTheScopeParameterOnlyNarrows:
    """A narrowing control that can widen is a privilege escalation."""

    @pytest.fixture
    def india_client(self, geo):
        client, _ = admin_client(
            'narrow@saaradhigo.test', '+918800007001',
            'city_ops_manager', geo['IN'],
        )
        return client

    def test_it_narrows_within_the_held_scope(self, india_client, geo, two_drivers):
        vja = make_driver('+919000000009', geo['IN-AP-VJA'])

        response = india_client.get('/api/v1/driver/admin/?scope=IN-TG-HYD')

        ids = _driver_ids(response)
        assert two_drivers['hyd'].id in ids
        assert vja.id not in ids

    def test_it_cannot_reach_another_country(self, india_client, two_drivers):
        response = india_client.get('/api/v1/driver/admin/?scope=US')

        assert response.status_code == 403
        assert response.json()['error']['code'] == 'SCOPE_NOT_HELD'

    def test_it_cannot_widen_to_global(self, india_client):
        response = india_client.get('/api/v1/driver/admin/?scope=GLOBAL')

        assert response.status_code == 403

    def test_an_unknown_code_is_refused_not_ignored(self, india_client):
        """Ignoring it would silently serve the full held scope, which is
        the opposite of what the operator asked for."""
        response = india_client.get('/api/v1/driver/admin/?scope=NOPE')

        assert response.status_code == 403

    def test_the_trip_list_narrows_too(self, india_client, geo):
        response = india_client.get('/api/v1/ride/admin/trips/?scope=US-TX')

        assert response.status_code == 403
        assert response.json()['error']['code'] == 'SCOPE_NOT_HELD'

    def test_no_parameter_means_everything_held(self, india_client, two_drivers, geo):
        vja = make_driver('+919000000010', geo['IN-AP-VJA'])

        ids = _driver_ids(india_client.get('/api/v1/driver/admin/'))

        assert {two_drivers['hyd'].id, vja.id} <= ids
