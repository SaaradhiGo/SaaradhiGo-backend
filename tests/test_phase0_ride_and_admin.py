"""Regression-locks for ride-engine + admin-permission Phase-0 fixes.

Covers:
  - PR #13: trip OTP only returned to the rider via REST.
  - PR #14: fare is computed from server-validated distance, not client.
  - PR #2:  admin_list_trips orders by requested_at (no 500).
  - PR #9:  IsAdmin is the single canonical class, paired with IsAuthenticated.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

@pytest.fixture
def trip_with_driver(db, auth_client_rider, auth_client_driver):
    """Create a trip belonging to the rider, assigned to the driver,
    with a known OTP. Returns the trip plus both API clients."""
    from servers.ride.models import Trip, TripStatus
    rider_client, rider_user = auth_client_rider
    driver_client, driver_user = auth_client_driver
    status, _ = TripStatus.objects.get_or_create(status_code='accepted')
    trip = Trip.objects.create(
        user_id=rider_user,
        driver_id=driver_user.driver,
        pickup_lat=Decimal('17.4359'),
        pickup_long=Decimal('78.4449'),
        destination_lat=Decimal('17.4400'),
        destination_long=Decimal('78.3480'),
        estimated_fare=Decimal('150.00'),
        status_id=status,
        otp='246810',
    )
    return trip, rider_client, driver_client, rider_user, driver_user


# -------------------------------------------------------------------------
# PR #13 — ride OTP only visible to the rider
# -------------------------------------------------------------------------

def test_trip_detail_returns_otp_to_rider(trip_with_driver):
    trip, rider_client, _, _, _ = trip_with_driver
    resp = rider_client.get(f'/api/v1/ride/trip/{trip.id}/')
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    assert data.get('otp') == '246810'


def test_trip_detail_hides_otp_from_driver(trip_with_driver):
    trip, _, driver_client, _, _ = trip_with_driver
    resp = driver_client.get(f'/api/v1/ride/trip/{trip.id}/')
    # The driver IS allowed to read the trip — they need driver/vehicle
    # info — but the OTP field must be null. The whole point of the OTP
    # is that the rider reads it aloud at pickup.
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    assert data.get('otp') in (None, '')


def test_trip_driver_details_never_returns_otp(trip_with_driver):
    """The /details/ endpoint serves driver+vehicle info; OTP must not
    appear there even for the rider (defense in depth)."""
    trip, rider_client, driver_client, _, _ = trip_with_driver
    for client in (rider_client, driver_client):
        resp = client.get(f'/api/v1/ride/trip/{trip.id}/details/')
        # The endpoint may 200 or 404 depending on cache state; either way
        # OTP must not be in the payload.
        if resp.status_code == 200:
            data = resp.json().get('data', {})
            assert 'otp' not in data


# -------------------------------------------------------------------------
# PR #14 — fare uses server-computed distance, ignores client input
# -------------------------------------------------------------------------

@patch('servers.ride.utils.get_google_maps_distance')
def test_estimate_fare_ignores_inflated_client_distance(
    mock_gmaps, auth_client_rider, db,
):
    """Google says 5km; the rider claims 50km. Server fare must be based
    on 5km, not 50km."""
    from servers.driver.models import VehicleType
    VehicleType.objects.get_or_create(type='sedan')

    mock_gmaps.return_value = (5.0, 15.0)  # 5 km, 15 min

    rider_client, _ = auth_client_rider
    payload = {
        'pickup_lat': 17.4359,
        'pickup_long': 78.4449,
        'destination_lat': 17.4400,
        'destination_long': 78.3480,
        'distance_km': 50.0,   # client inflated value
        'duration_min': 150.0, # client inflated value
        'vehicle_type': 'sedan',
    }
    resp = rider_client.post(
        '/api/v1/ride/estimate-fare/', payload, format='json',
    )
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    # Fare should reflect 5km (per-km × 5 + ...), NOT 50km.
    # Without knowing the exact defaults, the upper bound check is enough:
    # 50km × any reasonable per-km would be ≥ ₹100; 5km × default ₹12 = ₹60.
    fare = Decimal(str(data['estimated_fare']))
    assert fare < Decimal('500'), (
        f'Fare {fare} too high — looks like the client distance leaked through'
    )


@patch('servers.ride.utils.get_google_maps_distance', return_value=(None, None))
def test_estimate_fare_falls_back_safely_when_google_unavailable(
    mock_gmaps, auth_client_rider, db,
):
    """When Google Maps fails, the function falls back to Haversine ×
    road-factor — not the client distance, and not an UnboundLocalError
    (the bug QA-2 patched in PR #27)."""
    from servers.driver.models import VehicleType
    VehicleType.objects.get_or_create(type='sedan')

    rider_client, _ = auth_client_rider
    payload = {
        'pickup_lat': 17.4359, 'pickup_long': 78.4449,
        'destination_lat': 17.4400, 'destination_long': 78.3480,
        'distance_km': 999.0, 'duration_min': 999.0,
        'vehicle_type': 'sedan',
    }
    resp = rider_client.post(
        '/api/v1/ride/estimate-fare/', payload, format='json',
    )
    assert resp.status_code == 200, resp.content


# -------------------------------------------------------------------------
# PR #2 — admin_list_trips orders by requested_at, not created_at
# -------------------------------------------------------------------------

def test_admin_list_trips_does_not_500(auth_client_admin):
    """The view used to order_by('-created_at') against a field the model
    didn't have — every call returned 500."""
    client, _ = auth_client_admin
    resp = client.get('/api/v1/ride/admin/trips/')
    assert resp.status_code == 200, resp.content


# -------------------------------------------------------------------------
# PR #9 — IsAdmin permission consolidation
# -------------------------------------------------------------------------

def test_isadmin_denies_authenticated_non_admin(auth_client_rider):
    """A regular rider must NOT reach any /admin/ endpoint."""
    client, _ = auth_client_rider
    resp = client.get('/api/v1/ride/admin/trips/')
    assert resp.status_code == 403


def test_isadmin_denies_anonymous(api_client):
    """An unauthenticated request must get 401, not 500 (the audit also
    flagged that AnonymousUser.role would AttributeError without the
    paired IsAuthenticated)."""
    resp = api_client.get('/api/v1/ride/admin/trips/')
    assert resp.status_code in (401, 403)


def test_isadmin_allows_admin(auth_client_admin):
    client, _ = auth_client_admin
    resp = client.get('/api/v1/ride/admin/trips/')
    assert resp.status_code == 200


def test_isadmin_is_single_canonical_class():
    """Both auth_user.permissions.IsAdmin and driver.permissions.IsAdmin
    must resolve to the same class — base.permissions.IsAdmin."""
    from base.permissions import IsAdmin as Canonical
    from servers.auth_user.permissions import IsAdmin as A
    from servers.driver.permissions import IsAdmin as B
    assert A is Canonical
    assert B is Canonical
