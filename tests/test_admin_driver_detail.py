"""Tests for /api/v1/driver/admin/<id>/full/ — ops driver-detail aggregate."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone


@pytest.mark.django_db
def test_driver_full_detail_returns_aggregate(auth_client_admin, auth_client_driver):
    admin_client, _ = auth_client_admin
    _, driver_user = auth_client_driver
    driver = driver_user.driver

    resp = admin_client.get(f'/api/v1/driver/admin/{driver.id}/full/')
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    assert data['id'] == driver.id
    assert data['user']['phone_number'] == driver_user.phone_number
    # Expected sections present
    for k in ('vehicles', 'fatigue', 'sessions', 'cancellations',
              'withdrawals', 'recent_trips', 'earnings'):
        assert k in data, f'missing section {k}'
    # Counts default to 0 on a fresh driver
    assert data['cancellations']['last_24h'] == 0
    assert data['earnings']['lifetime'] == '0.00'


@pytest.mark.django_db
def test_driver_full_detail_404_for_missing_driver(auth_client_admin):
    admin_client, _ = auth_client_admin
    resp = admin_client.get('/api/v1/driver/admin/9999999/full/')
    assert resp.status_code == 404
    assert resp.json()['error']['code'] == 'NOT_FOUND'


@pytest.mark.django_db
def test_driver_full_detail_requires_admin(auth_client_rider, auth_client_driver):
    rider_client, _ = auth_client_rider
    _, driver_user = auth_client_driver
    driver = driver_user.driver
    resp = rider_client.get(f'/api/v1/driver/admin/{driver.id}/full/')
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_driver_full_detail_surfaces_cancellation_counts_and_recent_trips(
    auth_client_admin, auth_client_rider, auth_client_driver,
):
    from servers.driver.models import DriverCancellation
    from servers.ride.models import Trip, TripStatus

    admin_client, _ = auth_client_admin
    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    driver = driver_user.driver

    accepted, _ = TripStatus.objects.get_or_create(status_code='accepted')
    completed, _ = TripStatus.objects.get_or_create(status_code='completed')

    for i in range(2):
        t = Trip.objects.create(
            user_id=rider_user, driver_id=driver,
            pickup_lat=Decimal('17.4'), pickup_long=Decimal('78.4'),
            destination_lat=Decimal('17.45'), destination_long=Decimal('78.36'),
            estimated_fare=Decimal('150.00'), status_id=accepted,
        )
        DriverCancellation.objects.create(
            driver=driver, trip=t, reason='no_show',
        )

    # One completed trip for earnings + recent_trips
    Trip.objects.create(
        user_id=rider_user, driver_id=driver,
        pickup_lat=Decimal('17.4'), pickup_long=Decimal('78.4'),
        destination_lat=Decimal('17.45'), destination_long=Decimal('78.36'),
        estimated_fare=Decimal('200.00'), final_fare=Decimal('210.00'),
        status_id=completed, completed_at=timezone.now(),
    )

    resp = admin_client.get(f'/api/v1/driver/admin/{driver.id}/full/')
    assert resp.status_code == 200
    data = resp.json()['data']
    assert data['cancellations']['last_24h'] == 2
    assert data['cancellations']['last_30d'] == 2
    # 3 trips total in recent_trips
    assert len(data['recent_trips']) == 3
    assert Decimal(data['earnings']['lifetime']) == Decimal('210.00')


@pytest.mark.django_db
def test_driver_full_detail_includes_fatigue_lockout_when_present(
    auth_client_admin, auth_client_driver,
):
    admin_client, _ = auth_client_admin
    _, driver_user = auth_client_driver
    driver = driver_user.driver
    until = timezone.now() + timedelta(hours=2)
    driver.fatigue_lockout_until = until
    driver.save(update_fields=['fatigue_lockout_until'])

    resp = admin_client.get(f'/api/v1/driver/admin/{driver.id}/full/')
    assert resp.status_code == 200
    data = resp.json()['data']
    assert data['fatigue']['locked'] is True
    assert data['fatigue']['reason'] == 'lockout'
