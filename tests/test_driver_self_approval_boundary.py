"""A driver must never be able to approve themselves.

`approved` is the gate that lets a driver accept rides, and it is the outcome of an
MVA-2020 document review performed by an admin. Before this, the only thing
stopping a driver from setting it was the four-field allow-list inside
`update_driver_profile` — `DriverProfileSerializer` itself declared `approved`
writable. That is one refactor away from handing out the KYC gate with no error and
no failing test.

These tests attack the boundary from three directions and also pin that the
legitimate admin path still works, because a hardening that breaks approval is not
a hardening.
"""

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.driver.serializers import DriverProfileSerializer

User = get_user_model()

PROFILE = '/api/v1/driver/driver/update/'


@pytest.fixture
def driver(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    u = User.objects.create_user(phone_number='+919800000701', role='driver')
    d = Driver.objects.create(user_id=u, approved=False, status='off')
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vt, vehicle_number='TS09SEC001')
    return d


@pytest.fixture
def api(driver):
    from rest_framework.test import APIClient

    c = APIClient()
    c.force_authenticate(user=driver.user_id)
    return c


# ---------------------------------------------------------------------------
# 1. Through the profile-update endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_driver_cannot_approve_themselves_via_profile_update(api, driver):
    """The headline attack: PATCH your own profile with approved=true."""
    resp = api.patch(PROFILE, {'approved': True, 'license_expiry': '2030-01-01'},
                     format='json')

    driver.refresh_from_db()
    assert driver.approved is False, 'a driver must not be able to self-approve'
    # The legitimate part of the same request is still honoured, so this is a
    # boundary and not a blanket refusal.
    assert resp.status_code == 200, resp.content
    assert str(driver.license_expiry) == '2030-01-01'


@pytest.mark.django_db
def test_a_driver_cannot_set_their_own_status_or_trip_counters(api, driver):
    """`status`, `total_trips` and `ratings` are outcomes, not inputs.

    Ratings in particular: a driver editing their own rating is a trust defect
    even though it moves no money.
    """
    driver.total_trips = 7
    driver.save(update_fields=['total_trips'])

    api.patch(PROFILE, {'status': 'active', 'total_trips': 9999,
                        'ratings': '5.00', 'license_expiry': '2030-01-01'},
              format='json')

    driver.refresh_from_db()
    assert driver.status == 'off'
    assert driver.total_trips == 7
    assert driver.ratings == 0


@pytest.mark.django_db
def test_approval_state_survives_a_payload_of_only_forbidden_fields(api, driver):
    """No legitimate field at all. The view should refuse the request outright,
    and nothing may change either way."""
    resp = api.patch(PROFILE, {'approved': True, 'status': 'active'}, format='json')

    driver.refresh_from_db()
    assert driver.approved is False
    assert driver.status == 'off'
    assert resp.status_code == 400


@pytest.mark.django_db
def test_unexpected_payload_fields_are_ignored(api, driver):
    """Fields nobody anticipated must not become an attack surface.

    Includes `doc_status`, which is the compliance state the approval workflow
    owns, and `user_id`, which would be an account-takeover primitive.
    """
    other = User.objects.create_user(phone_number='+919800000799', role='driver')

    api.patch(PROFILE, {
        'license_expiry': '2030-01-01',
        'doc_status': 'approved',
        'doc_rejection_reason': None,
        'user_id': other.id,
        'id': 99999,
        'is_staff': True,
        'role': 'admin',
    }, format='json')

    driver.refresh_from_db()
    assert driver.approved is False
    assert driver.doc_status != 'approved'
    assert driver.user_id_id != other.id
    other.refresh_from_db()
    assert other.is_staff is False
    assert other.role == 'driver'


# ---------------------------------------------------------------------------
# 2. Through the serializer directly (mass assignment)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_serializer_itself_refuses_to_write_approval(driver):
    """The defence that does not depend on the view remembering its allow-list.

    This is the regression that matters: if someone later passes `request.data`
    straight into this serializer, or reuses it in a new endpoint, the gate must
    still hold.
    """
    s = DriverProfileSerializer(
        driver,
        data={'approved': True, 'status': 'active', 'total_trips': 500,
              'ratings': '5.00'},
        partial=True,
    )
    assert s.is_valid(), s.errors
    # Read-only fields are dropped at validation, so they cannot reach .save().
    assert 'approved' not in s.validated_data
    assert 'status' not in s.validated_data
    assert 'total_trips' not in s.validated_data
    assert 'ratings' not in s.validated_data

    s.save()
    driver.refresh_from_db()
    assert driver.approved is False
    assert driver.status == 'off'


@pytest.mark.django_db
def test_the_lifecycle_fields_are_declared_read_only(driver):
    """Stated as a contract, so removing it is a deliberate act with a red test."""
    declared = DriverProfileSerializer().fields
    for name in ('approved', 'status', 'total_trips', 'ratings'):
        assert declared[name].read_only is True, name
    # And the fields a driver legitimately owns are still writable.
    for name in ('license_doc', 'license_doc_back', 'license_expiry', 'active_vehicle'):
        assert declared[name].read_only is False, name


# ---------------------------------------------------------------------------
# 3. The admin path must still work
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_admin_kyc_path_can_still_approve(driver):
    """A hardening that breaks approval is not a hardening.

    KYCApprovalSerializer is a separate serializer that declares `approved` and
    `status` writable and enforces the MVA-2020 checks. It must be unaffected.
    """
    from servers.driver.serializers import KYCApprovalSerializer

    vehicle = Vehicle.objects.filter(driver_id=driver).first()
    vehicle.rc_doc = 'rc_docs/qa-placeholder.png'
    vehicle.save(update_fields=['rc_doc'])
    driver.license_doc = 'license_docs/qa-placeholder.png'
    driver.license_expiry = timezone.localdate().replace(
        year=timezone.localdate().year + 2)
    driver.active_vehicle = vehicle
    driver.save(update_fields=['license_doc', 'license_expiry', 'active_vehicle'])

    s = KYCApprovalSerializer(driver, data={'approved': True}, partial=True)
    assert s.is_valid(), s.errors
    s.save()

    driver.refresh_from_db()
    assert driver.approved is True


@pytest.mark.django_db
def test_the_admin_kyc_path_still_enforces_the_document_requirements(driver):
    """The MVA checks are not weakened: approval without documents still fails."""
    from servers.driver.serializers import KYCApprovalSerializer

    s = KYCApprovalSerializer(driver, data={'approved': True}, partial=True)
    assert not s.is_valid(), 'approval without documents must be refused'

    driver.refresh_from_db()
    assert driver.approved is False


@pytest.mark.django_db
def test_a_driver_cannot_reach_the_admin_kyc_endpoint(api, driver):
    """The endpoint requires is_staff AND role=admin; a driver token has neither."""
    resp = api.patch(f'/api/v1/driver/admin/{driver.id}/update-kyc/',
                     {'approved': True}, format='json')
    assert resp.status_code in (401, 403)

    driver.refresh_from_db()
    assert driver.approved is False
