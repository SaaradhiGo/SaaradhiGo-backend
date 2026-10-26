"""The driver's own onboarding checklist.

WHY IT MATTERS
--------------
Approval is gated on documents -- `KYCApprovalSerializer` refuses
`approved=True` without a licence, a future expiry, an assigned vehicle and
that vehicle's registration. A driver had no way to see any of that: the
website offered no upload, and the profile reported `approved: false` with
no reason. The honest answer to "how are we approving drivers without
documents?" was that nobody could reach approval through the website at all.

The checklist must agree with the reviewer BY CONSTRUCTION, which is why it
is computed from `compliance.evaluate_*` rather than written down a second
time. These tests are mostly about that agreement, and about the checklist
never being mistakable for an approval.
"""

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from servers.driver.models import Driver, Vehicle, VehicleType

pytestmark = pytest.mark.django_db

User = get_user_model()

URL = '/api/v1/driver/driver/onboarding/'


@pytest.fixture
def driver():
    user = User.objects.create_user(phone_number='+919800000801', role='driver')
    return Driver.objects.create(user_id=user, approved=False, status='off')


@pytest.fixture
def api(driver):
    client = APIClient()
    client.force_authenticate(user=driver.user_id)
    return client


def _body(response):
    return response.json()['data']


class TestItTellsTheDriverWhatIsOutstanding:
    def test_a_brand_new_driver_is_told_what_is_missing(self, api, driver):
        r = api.get(URL)

        assert r.status_code == 200, r.content.decode()
        body = _body(r)
        assert body['approved'] is False
        assert body['ready_for_review'] is False
        assert body['outstanding'], 'a driver with nothing uploaded has nothing outstanding?'
        # The licence is required in every country we serve.
        keys = {d['key'] for d in body['driver_documents']}
        assert 'license_front' in keys

    def test_every_document_says_how_to_upload_it(self, api):
        """The client must not need its own copy of the key -> kind mapping."""
        body = _body(api.get(URL))

        for doc in body['driver_documents']:
            assert doc['upload_kind'], (
                f"{doc['key']} has no upload_kind, so a client cannot presign it"
            )

    def test_an_expired_licence_is_not_ready_even_with_the_photo(self, api, driver):
        """The KYC gate checks the DATE separately from the document.

        A driver with a perfect photo of an expired licence is refused, so
        the checklist has to say so before they wait for a review that
        cannot succeed.
        """
        driver.license_doc = 'license_docs/x.jpg'
        driver.license_expiry = timezone.localdate() - timezone.timedelta(days=1)
        driver.save()

        body = _body(api.get(URL))

        assert body['license_expiry_ok'] is False
        assert body['ready_for_review'] is False


class TestItAgreesWithTheReviewer:
    def test_it_does_not_claim_ready_without_an_active_vehicle(self, api, driver):
        """`KYCApprovalSerializer` requires `active_vehicle`. So does this."""
        vt, _ = VehicleType.objects.get_or_create(type='sedan')
        vehicle = Vehicle.objects.create(
            driver_id=driver, vehicle_type_id=vt, vehicle_number='TS09AB1234',
        )
        # Every document present, but the vehicle is not the ACTIVE one.
        for column in ('rc_doc', 'insurance_doc', 'permit_doc', 'fitness_doc',
                       'puc_doc', 'inspection_doc'):
            if hasattr(vehicle, column):
                setattr(vehicle, column, f'{column}s/x.jpg')
        vehicle.save()
        driver.license_doc = 'license_docs/x.jpg'
        driver.license_doc_back = 'license_docs_back/x.jpg'
        driver.license_expiry = timezone.localdate() + timezone.timedelta(days=365)
        driver.active_vehicle = None
        driver.save()

        body = _body(api.get(URL))

        assert body['ready_for_review'] is False, (
            'the checklist said ready while the KYC gate would refuse for '
            'a missing active_vehicle'
        )


class TestItIsNotAnApproval:
    def test_ready_for_review_does_not_approve_anybody(self, api, driver):
        api.get(URL)
        driver.refresh_from_db()
        assert driver.approved is False

    def test_it_is_read_only(self, api):
        assert api.post(URL, {}, format='json').status_code == 405


class TestItIsTheDriversOwn:
    def test_a_rider_cannot_read_it(self):
        rider = User.objects.create_user(
            phone_number='+919800000802', role='rider')
        client = APIClient()
        client.force_authenticate(user=rider)

        assert client.get(URL).status_code in (403, 401)

    def test_it_reports_the_review_outcome_an_operator_set(self, api, driver):
        driver.doc_status = 'rejected'
        driver.doc_rejection_reason = 'Licence photo is unreadable.'
        driver.save(update_fields=['doc_status', 'doc_rejection_reason'])

        body = _body(api.get(URL))

        assert body['doc_status'] == 'rejected'
        assert body['doc_rejection_reason'] == 'Licence photo is unreadable.'


class TestItCarriesTheVehicleCatalogue:
    """A page that registers a car must not keep its own list of categories.

    `/pricing/vehicle-types/` is the rider-facing endpoint and needs a
    lat/lng: it answers "what can I be quoted here", which is not the
    question "what kind of car do you drive". A driver adding a vehicle has
    no pickup point.
    """

    def test_the_categories_are_offered(self, api):
        VehicleType.objects.get_or_create(type='sedan')
        VehicleType.objects.get_or_create(type='auto')

        body = _body(api.get(URL))

        types = {c['type'] for c in body['vehicle_types']}
        assert {'sedan', 'auto'} <= types, types
