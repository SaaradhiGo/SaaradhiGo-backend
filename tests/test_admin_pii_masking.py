"""Personal data is withheld, not hidden.

THE ASSERTION THAT MATTERS
--------------------------
Every test here checks that the real value is ABSENT FROM THE RESPONSE
BODY, not that some field equals a masked string. Those are different
claims, and only the first one is worth anything: a payload that carries
the phone number and a console that prints dots is masked in the pixels
and nowhere else -- the number is still in the network tab, in any log
that captures a body, and on every machine the response crossed.

So the tests serialize the whole response and search it for the digits.

MASKED BY DEFAULT FOR EVERY OPERATOR (Phase 1(e))
-------------------------------------------------
`pii.view` no longer unmasks a list. It is the capability to REVEAL one
record through `/admin-access/reveal/`, logged per call. What it changes
in a list is the name (full, rather than first name + initial), the
coordinate precision and whether document links are served. The phone and
the email are masked for the KYC Verifier exactly as for the Analyst. The
exhaustive sweep lives in `test_pii_masking_everywhere.py`; this file keeps
the driver-list story readable.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.admin_access.masking import (
    STRICT, TRUSTED, mask_email, mask_name, mask_payload, mask_phone,
)
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.driver.models import Driver

pytestmark = pytest.mark.django_db

User = get_user_model()

DRIVER_PHONE = '+919876543210'
DRIVER_EMAIL = 'ravi.kumar@example.test'
DRIVER_NAME = 'Ravi Kumar'


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def driver(geo):
    user = User.objects.create_user(
        phone_number=DRIVER_PHONE, username=DRIVER_PHONE,
        email=DRIVER_EMAIL, full_name=DRIVER_NAME, role='driver',
    )
    return Driver.objects.create(user_id=user, home_city=geo['IN-TG-HYD'])


def client_for(role_code, scope, email, phone):
    user = User.objects.create_user(
        phone_number=phone, username=phone, email=email,
        role='admin', is_staff=True,
    )
    AdminAssignment.objects.create(
        admin=user, role=AdminRole.objects.get(code=role_code), scope=scope,
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


@pytest.fixture
def with_pii(geo):
    """KYC Verifier: holds pii.view, because reading identity documents is
    the job."""
    return client_for('kyc_verifier', geo['IN-TG-HYD'],
                      'kyc@saaradhigo.test', '+918811000001')


@pytest.fixture
def without_pii(geo):
    """Analyst: the brief says explicitly that this role sees no PII."""
    return client_for('analyst', geo['IN-TG-HYD'],
                      'analyst@saaradhigo.test', '+918811000002')


def body_of(response):
    return json.dumps(response.json())


# ---------------------------------------------------------------------------
# The maskers themselves
# ---------------------------------------------------------------------------

class TestTheMaskers:
    def test_a_phone_keeps_its_country_code_and_last_four(self):
        """Enough to confirm a number you were given; useless in bulk."""
        masked = mask_phone('+919876543210')

        assert masked.startswith('+91')
        assert masked.endswith('3210')
        assert '98765' not in masked

    def test_an_indian_and_a_us_number_stay_distinguishable(self):
        """An operator must be able to tell which market a record is in."""
        assert mask_phone('+919876543210').startswith('+91')
        assert mask_phone('+15125551234').startswith('+1')

    def test_an_email_keeps_its_domain(self):
        masked = mask_email('srinivas@deeptrics.com')

        assert masked.endswith('@deeptrics.com')
        assert 'srinivas' not in masked
        assert masked.startswith('s')

    def test_a_name_keeps_the_first_name(self):
        assert mask_name('Ravi Kumar') == 'Ravi K.'

    def test_a_single_name_survives(self):
        assert mask_name('Ravi') == 'Ravi'

    def test_a_name_that_is_a_phone_number_is_masked_as_one(self):
        """An account with no name shows its number in the name field."""
        assert mask_name('+919876543210') == '+91••••••3210'
        assert mask_name('ravi@example.test') == 'r•••@example.test'

    @pytest.mark.parametrize('policy', [STRICT, TRUSTED], ids=['strict', 'trusted'])
    def test_a_phone_in_a_name_field_is_masked_for_every_viewer(self, policy):
        payload = {'driver_name': '+919876543210', 'full_name': 'Ravi Kumar'}
        mask_payload(payload, policy)
        assert payload['driver_name'] == '+91••••••3210'
        assert payload['full_name'] == ('Ravi Kumar' if policy is TRUSTED else 'Ravi K.')

    def test_empty_values_pass_through(self):
        """A mask that turns None into bullets invents data."""
        assert mask_phone('') == ''
        assert mask_phone(None) is None
        assert mask_name(None) is None


# ---------------------------------------------------------------------------
# The list endpoint
# ---------------------------------------------------------------------------

class TestTheDriverList:
    def test_the_number_is_not_in_the_payload_at_all(self, without_pii, driver):
        """The whole point. Not "the field is masked" -- ABSENT."""
        response = without_pii.get('/api/v1/driver/admin/')

        assert response.status_code == 200
        assert DRIVER_PHONE not in body_of(response)
        assert '9876543210' not in body_of(response)

    def test_the_email_is_not_in_the_payload(self, without_pii, driver):
        assert DRIVER_EMAIL not in body_of(without_pii.get('/api/v1/driver/admin/'))

    def test_the_surname_is_not_in_the_payload(self, without_pii, driver):
        body = body_of(without_pii.get('/api/v1/driver/admin/'))

        assert 'Kumar' not in body
        # But the first name survives, or the list is unusable.
        assert 'Ravi' in body

    def test_a_role_with_pii_view_gets_the_full_name_and_still_no_phone(
        self, with_pii, driver,
    ):
        """`pii.view` is the reveal capability, not a plaintext list.

        The KYC Verifier recognises the person (full name) and reveals the
        number one record at a time, logged; the list itself never carries
        it. Masking the NAME for them too would make the queue unusable.
        """
        body = body_of(with_pii.get('/api/v1/driver/admin/'))

        assert DRIVER_PHONE not in body
        assert '9876543210' not in body
        assert DRIVER_NAME in body

    def test_the_row_is_still_listed(self, without_pii, driver):
        """Masked, not filtered out. An Analyst counts drivers."""
        payload = without_pii.get('/api/v1/driver/admin/').json()['data']

        assert payload['count'] == 1


# ---------------------------------------------------------------------------
# The detail endpoints
# ---------------------------------------------------------------------------

class TestTheDetailEndpoints:
    def test_the_detail_view_masks(self, without_pii, driver):
        response = without_pii.get(f'/api/v1/driver/admin/{driver.id}/')

        assert response.status_code == 200
        assert DRIVER_PHONE not in body_of(response)

    def test_the_full_detail_view_masks(self, without_pii, driver):
        """The big aggregate: licence, vehicles, earnings, sessions. It
        carries the person at several depths, which is why the masker
        recurses."""
        response = without_pii.get(f'/api/v1/driver/admin/{driver.id}/full/')

        assert response.status_code == 200
        assert DRIVER_PHONE not in body_of(response)
        assert DRIVER_EMAIL not in body_of(response)

    def test_the_full_detail_view_is_masked_for_kyc_too(self, with_pii, driver):
        """The document links are served (that is the job); the number is not."""
        response = with_pii.get(f'/api/v1/driver/admin/{driver.id}/full/')

        assert response.status_code == 200
        assert DRIVER_PHONE not in body_of(response)
        assert DRIVER_NAME in body_of(response)

    def test_kyc_reveals_the_number_through_the_reveal_endpoint(self, with_pii, driver):
        from servers.admin_audit.models import AdminAuditLog

        response = with_pii.post('/api/v1/admin-access/reveal/', {
            'target_type': 'driver', 'target_id': driver.id,
            'fields': ['phone_number'], 'reason': 'KYC call-back',
        }, format='json')

        assert response.status_code == 200, response.content
        assert response.json()['data']['fields']['phone_number'] == DRIVER_PHONE
        row = AdminAuditLog.objects.filter(action='pii_revealed').order_by('-id').first()
        assert row is not None and row.after['fields'] == ['phone_number']
        assert DRIVER_PHONE not in json.dumps(row.after)


# ---------------------------------------------------------------------------
# pii.view is scoped like everything else
# ---------------------------------------------------------------------------

class TestPiiViewIsScoped:
    def test_pii_view_in_one_city_does_not_unmask_another(self, geo, driver):
        """A KYC Verifier for Vijayawada holds `pii.view` -- but not here.

        If `pii.view` were a role property rather than a scoped permission,
        this would leak every driver in the country.
        """
        other_city = client_for('kyc_verifier', geo['IN-AP-VJA'],
                                'vja.kyc@saaradhigo.test', '+918811000003')

        response = other_city.get(f'/api/v1/driver/admin/{driver.id}/')

        # Out of scope entirely, so the record is not found -- which is a
        # stronger answer than masking.
        assert response.status_code == 404

    def test_a_country_scoped_verifier_reaches_the_record(self, geo, driver):
        """The control for the test above: same role, scope that covers.

        Reached, named in full -- and still masked, like every list.
        """
        india = client_for('kyc_verifier', geo['IN'],
                           'in.kyc@saaradhigo.test', '+918811000004')

        response = india.get(f'/api/v1/driver/admin/{driver.id}/')

        assert response.status_code == 200
        assert DRIVER_NAME in body_of(response)
        assert DRIVER_PHONE not in body_of(response)


# ---------------------------------------------------------------------------
# The legacy path
# ---------------------------------------------------------------------------

class TestUnmigratedAccounts:
    def test_an_unmigrated_operator_is_masked_like_everybody_else(
        self, driver, geo, settings,
    ):
        """Masking is not a migration question (owner's rule: masked by
        default for EVERY operator). What the bridge still decides for an
        unmigrated account is the trusted policy -- full names, exact
        coordinates, documents -- never the phone or the email.
        """
        settings.ADMIN_ACCESS_STRICT = False
        user = User.objects.create_user(
            phone_number='+918811000009', username='+918811000009',
            email='legacy@saaradhigo.test', role='admin', is_staff=True,
        )
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')

        body = body_of(client.get('/api/v1/driver/admin/'))

        assert DRIVER_PHONE not in body
        assert DRIVER_NAME in body
