"""Tests for /api/v1/ride/admin/trips/<id>/ — the ops trip-detail endpoint.

The endpoint aggregates across nine tables, so we mostly assert that
each section is present in the response shape and that admin-only RBAC
is enforced.
"""
from decimal import Decimal

import pytest
from django.utils import timezone


def _build_trip(rider_user, driver_user, status_code='completed'):
    from servers.ride.models import (
        ChatMessage, FarePricing, PromoCode, PromoRedemption, Rating,
        Trip, TripStatus,
    )
    st, _ = TripStatus.objects.get_or_create(status_code=status_code)
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=Decimal('17.385'), pickup_long=Decimal('78.486'),
        destination_lat=Decimal('17.44'), destination_long=Decimal('78.38'),
        pickup_address='Banjara Hills', destination_address='Hitech City',
        estimated_fare=Decimal('200.00'), final_fare=Decimal('210.00'),
        status_id=st, completed_at=timezone.now(),
        payment_method='online', payment_status='completed',
    )
    FarePricing.objects.create(
        trip_id=trip, base_fare=Decimal('60'), distance_fare=Decimal('120'),
        time_fare=Decimal('30'), surge_multiplier=Decimal('1.00'),
        total_fare=Decimal('210'),
    )
    ChatMessage.objects.create(trip=trip, sender=rider_user, sender_role='rider', body='Hi')
    ChatMessage.objects.create(trip=trip, sender=driver_user, sender_role='driver', body='Reached')
    Rating.objects.create(trip_id=trip, rater_id=rider_user, score=5, comments='Great')
    return trip


@pytest.mark.django_db
def test_admin_trip_detail_returns_full_aggregate(auth_client_admin, auth_client_rider, auth_client_driver):
    admin_client, _ = auth_client_admin
    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    trip = _build_trip(rider_user, driver_user)

    resp = admin_client.get(f'/api/v1/ride/admin/trips/{trip.id}/')
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    assert data['id'] == trip.id
    assert data['status'] == 'completed'
    # Each section present
    assert data['rider'] is not None
    assert data['driver'] is not None
    assert data['fare']['breakdown'] is not None
    assert isinstance(data['ratings'], list) and len(data['ratings']) == 1
    assert data['ratings'][0]['direction'] == 'rider_to_driver'
    assert data['chat']['total'] == 2
    assert len(data['chat']['messages']) == 2
    assert data['zone'] is not None  # Hyderabad polygon resolves


@pytest.mark.django_db
def test_admin_trip_detail_includes_promo_when_present(
    auth_client_admin, auth_client_rider, auth_client_driver,
):
    from datetime import timedelta
    from servers.ride.models import PromoCode, PromoRedemption

    admin_client, _ = auth_client_admin
    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    trip = _build_trip(rider_user, driver_user)

    now = timezone.now()
    promo = PromoCode.objects.create(
        code='OPSTEST',
        discount_type='flat',
        discount_value=Decimal('25.00'),
        valid_from=now - timedelta(days=1),
        valid_to=now + timedelta(days=7),
        is_active=True,
    )
    PromoRedemption.objects.create(
        promo=promo, user=rider_user, trip=trip,
        discount_amount=Decimal('25.00'),
    )

    resp = admin_client.get(f'/api/v1/ride/admin/trips/{trip.id}/')
    assert resp.status_code == 200
    data = resp.json()['data']
    assert data['promo'] is not None
    assert data['promo']['code'] == 'OPSTEST'
    assert data['promo']['discount_amount'] == '25.00'


@pytest.mark.django_db
def test_admin_trip_detail_404_for_missing_trip(auth_client_admin):
    admin_client, _ = auth_client_admin
    resp = admin_client.get('/api/v1/ride/admin/trips/999999/')
    assert resp.status_code == 404
    assert resp.json()['error']['code'] == 'NOT_FOUND'


@pytest.mark.django_db
def test_admin_trip_detail_forbidden_for_non_admin(
    auth_client_rider, auth_client_driver,
):
    client, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    trip = _build_trip(rider_user, driver_user)
    resp = client.get(f'/api/v1/ride/admin/trips/{trip.id}/')
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_admin_trip_detail_chat_pagination(
    auth_client_admin, auth_client_rider, auth_client_driver,
):
    from servers.ride.models import ChatMessage

    admin_client, _ = auth_client_admin
    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    trip = _build_trip(rider_user, driver_user)
    # 2 base messages from _build_trip; add 8 more for 10 total
    for i in range(8):
        ChatMessage.objects.create(
            trip=trip, sender=rider_user, sender_role='rider',
            body=f'msg{i}',
        )

    # Default returns up to 50
    r1 = admin_client.get(f'/api/v1/ride/admin/trips/{trip.id}/')
    assert r1.status_code == 200
    assert r1.json()['data']['chat']['total'] == 10
    assert len(r1.json()['data']['chat']['messages']) == 10

    # Limit + offset
    r2 = admin_client.get(
        f'/api/v1/ride/admin/trips/{trip.id}/?chat_limit=3&chat_offset=4',
    )
    assert r2.status_code == 200
    data = r2.json()['data']
    assert data['chat']['total'] == 10
    assert data['chat']['offset'] == 4
    assert data['chat']['limit'] == 3
    assert len(data['chat']['messages']) == 3
