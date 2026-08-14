"""Phase-0 launch-readiness tests.

Covers six items shipped together in feat/phase0-launch-readiness:

  1. Driver fatigue cap (MVA 2020 12h/24h)
  2. Driver-initiated cancellation + rolling penalty
  3. Trip receipts (issue + idempotency + resend endpoint)
  4. Notification preferences (DPDP opt-out)
  5. Ops admin dashboard endpoint
  6. Support ticket endpoints + admin reply
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone


# ---------------------------------------------------------------------------
# 1. Fatigue cap
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_fatigue_cap_lockout_at_12_hours(db):
    from django.contrib.auth import get_user_model
    from servers.driver.fatigue import (
        FATIGUE_CAP_SECONDS,
        get_fatigue_status,
        record_session_end,
        record_session_start,
    )
    from servers.driver.models import Driver, DriverSession

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918100000001', role='driver')
    driver = Driver.objects.create(user_id=user)

    now = timezone.now()
    # Plant 12h of completed sessions in the last 24h.
    DriverSession.objects.create(
        driver=driver,
        started_at=now - timedelta(hours=12, minutes=5),
        ended_at=now - timedelta(minutes=5),
        duration_seconds=12 * 3600,
    )
    status = get_fatigue_status(driver, now=now)
    assert status.locked is True
    assert status.reason == 'cap_reached'
    driver.refresh_from_db()
    assert driver.fatigue_lockout_until is not None
    assert driver.fatigue_lockout_until > now


@pytest.mark.django_db
def test_fatigue_cap_allows_when_under_limit(db):
    from django.contrib.auth import get_user_model
    from servers.driver.fatigue import get_fatigue_status
    from servers.driver.models import Driver, DriverSession

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918100000002', role='driver')
    driver = Driver.objects.create(user_id=user)

    now = timezone.now()
    DriverSession.objects.create(
        driver=driver,
        started_at=now - timedelta(hours=4),
        ended_at=now - timedelta(hours=2),
        duration_seconds=2 * 3600,
    )
    status = get_fatigue_status(driver, now=now)
    assert status.locked is False
    assert status.active_seconds_24h == 2 * 3600


@pytest.mark.django_db
def test_fatigue_old_sessions_outside_24h_window_dont_count(db):
    from django.contrib.auth import get_user_model
    from servers.driver.fatigue import get_fatigue_status
    from servers.driver.models import Driver, DriverSession

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918100000003', role='driver')
    driver = Driver.objects.create(user_id=user)

    now = timezone.now()
    DriverSession.objects.create(
        driver=driver,
        started_at=now - timedelta(days=2),
        ended_at=now - timedelta(days=2) + timedelta(hours=14),
        duration_seconds=14 * 3600,
    )
    status = get_fatigue_status(driver, now=now)
    assert status.locked is False
    assert status.active_seconds_24h == 0


@pytest.mark.django_db
def test_record_session_start_is_idempotent(db):
    from django.contrib.auth import get_user_model
    from servers.driver.fatigue import record_session_start
    from servers.driver.models import Driver, DriverSession

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918100000004', role='driver')
    driver = Driver.objects.create(user_id=user)
    s1 = record_session_start(driver)
    s2 = record_session_start(driver)
    assert s1.id == s2.id
    assert DriverSession.objects.filter(driver=driver, ended_at__isnull=True).count() == 1


# ---------------------------------------------------------------------------
# 2. Driver cancellation penalty
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_driver_cancel_records_row_and_increments_counter(db):
    from django.contrib.auth import get_user_model
    from decimal import Decimal as D
    from servers.driver.fatigue import apply_cancellation_penalty
    from servers.driver.models import Driver, DriverCancellation
    from servers.ride.models import Trip, TripStatus

    User = get_user_model()
    rider_user = User.objects.create_user(phone_number='+918100000101', role='rider')
    driver_user = User.objects.create_user(phone_number='+918100000102', role='driver')
    driver = Driver.objects.create(user_id=driver_user, ratings=D('4.50'))

    status, _ = TripStatus.objects.get_or_create(status_code='accepted')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        estimated_fare=D('150.00'),
        status_id=status,
    )
    p = apply_cancellation_penalty(driver, trip, reason='no_show')
    assert p.recent_count == 1
    assert p.locked is False
    assert DriverCancellation.objects.filter(driver=driver).count() == 1


@pytest.mark.django_db
def test_driver_cancel_triggers_lockout_at_threshold(db):
    from django.contrib.auth import get_user_model
    from decimal import Decimal as D
    from servers.driver.fatigue import apply_cancellation_penalty
    from servers.driver.models import Driver
    from servers.ride.models import Trip, TripStatus

    User = get_user_model()
    rider_user = User.objects.create_user(phone_number='+918100000201', role='rider')
    driver_user = User.objects.create_user(phone_number='+918100000202', role='driver')
    driver = Driver.objects.create(user_id=driver_user, ratings=D('4.50'))
    status, _ = TripStatus.objects.get_or_create(status_code='accepted')

    last_penalty = None
    for _ in range(3):
        trip = Trip.objects.create(
            user_id=rider_user, driver_id=driver,
            pickup_lat=D('17.4'), pickup_long=D('78.4'),
            destination_lat=D('17.45'), destination_long=D('78.36'),
            estimated_fare=D('150.00'), status_id=status,
        )
        last_penalty = apply_cancellation_penalty(driver, trip, reason='personal')

    assert last_penalty.locked is True
    assert last_penalty.recent_count == 3
    driver.refresh_from_db()
    assert driver.fatigue_lockout_until is not None
    # Rating dropped by 0.1
    assert driver.ratings == D('4.40')


@pytest.mark.django_db
def test_driver_cancel_endpoint(auth_client_driver, auth_client_rider):
    from decimal import Decimal as D
    from servers.driver.models import DriverCancellation
    from servers.ride.models import Trip, TripStatus

    driver_client, driver_user = auth_client_driver
    _, rider_user = auth_client_rider
    accepted, _ = TripStatus.objects.get_or_create(status_code='accepted')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        estimated_fare=D('150.00'), status_id=accepted,
    )
    resp = driver_client.post(
        f'/api/v1/ride/trip/{trip.id}/driver-cancel/',
        {'reason': 'no_show'}, format='json',
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()['data']
    assert body['status'] == 'cancelled'
    assert body['penalty']['recent_count'] == 1
    trip.refresh_from_db()
    assert trip.status_id.status_code == 'cancelled'
    assert DriverCancellation.objects.filter(trip=trip).exists()


@pytest.mark.django_db
def test_driver_cancel_endpoint_rejects_bad_reason(auth_client_driver, auth_client_rider):
    from decimal import Decimal as D
    from servers.ride.models import Trip, TripStatus

    driver_client, driver_user = auth_client_driver
    _, rider_user = auth_client_rider
    accepted, _ = TripStatus.objects.get_or_create(status_code='accepted')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        estimated_fare=D('150.00'), status_id=accepted,
    )
    resp = driver_client.post(
        f'/api/v1/ride/trip/{trip.id}/driver-cancel/',
        {'reason': 'mood'}, format='json',
    )
    assert resp.status_code == 400
    assert resp.json()['error']['code'] == 'INVALID_REASON'


# ---------------------------------------------------------------------------
# 3. Receipts
# ---------------------------------------------------------------------------

def _make_completed_trip(rider_user, driver_user):
    from decimal import Decimal as D
    from servers.ride.models import FarePricing, Trip, TripStatus

    completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        pickup_address='Banjara Hills',
        destination_address='Hitech City',
        estimated_fare=D('200.00'), final_fare=D('210.00'),
        status_id=completed, completed_at=timezone.now(),
        payment_method='online', payment_status='completed',
    )
    FarePricing.objects.create(
        trip_id=trip, base_fare=D('60'), distance_fare=D('120'),
        time_fare=D('30'), surge_multiplier=D('1.00'), total_fare=D('210'),
    )
    return trip


@pytest.mark.django_db
def test_issue_receipt_creates_row_and_sends_mail(auth_client_rider, auth_client_driver, settings):
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    _, rider_user = auth_client_rider
    rider_user.email = 'rider@example.test'
    rider_user.save(update_fields=['email'])
    _, driver_user = auth_client_driver
    trip = _make_completed_trip(rider_user, driver_user)

    from servers.ride.receipts import issue_receipt
    from servers.ride.models import Receipt

    r = issue_receipt(trip)
    assert r is not None
    assert Receipt.objects.filter(trip_id=trip).count() == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ['rider@example.test']
    assert 'VahanGo' in mail.outbox[0].subject
    assert 'Receipt' in mail.outbox[0].alternatives[0][0] or 'Trip' in mail.outbox[0].alternatives[0][0]


@pytest.mark.django_db
def test_issue_receipt_is_idempotent(auth_client_rider, auth_client_driver, settings):
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    trip = _make_completed_trip(rider_user, driver_user)

    from servers.ride.receipts import issue_receipt
    from servers.ride.models import Receipt
    r1 = issue_receipt(trip)
    r2 = issue_receipt(trip)
    assert r1.id == r2.id
    assert Receipt.objects.filter(trip_id=trip).count() == 1


@pytest.mark.django_db
def test_resend_receipt_endpoint(auth_client_rider, auth_client_driver, settings):
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    client, rider_user = auth_client_rider
    rider_user.email = 'rider@example.test'
    rider_user.save(update_fields=['email'])
    _, driver_user = auth_client_driver
    trip = _make_completed_trip(rider_user, driver_user)

    resp = client.post(f'/api/v1/ride/trip/{trip.id}/receipt/resend/')
    assert resp.status_code == 200, resp.content
    body = resp.json()['data']
    assert body['trip_id'] == trip.id
    assert body['sent_to'] == 'rider@example.test'
    assert len(mail.outbox) >= 1


@pytest.mark.django_db
def test_resend_receipt_rejects_incomplete_trip(auth_client_rider, auth_client_driver):
    from decimal import Decimal as D
    from servers.ride.models import Trip, TripStatus
    client, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    requested, _ = TripStatus.objects.get_or_create(status_code='requested')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        estimated_fare=D('100.00'), status_id=requested,
    )
    resp = client.post(f'/api/v1/ride/trip/{trip.id}/receipt/resend/')
    assert resp.status_code == 400
    assert resp.json()['error']['code'] == 'INVALID_STATE'


# ---------------------------------------------------------------------------
# 4. Notification preferences
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_notification_prefs_defaults_are_dpdp_safe(auth_client_rider):
    client, _ = auth_client_rider
    resp = client.get('/api/v1/rider/notifications/preferences/')
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    assert data['marketing'] is False  # DPDP: opt-in only
    assert data['promo'] is False
    assert data['ride_event'] is True
    assert data['transactional'] is True
    assert data['sos'] is True
    assert 'marketing' in data['user_toggleable']


@pytest.mark.django_db
def test_notification_prefs_user_can_opt_in_to_marketing(auth_client_rider):
    client, _ = auth_client_rider
    resp = client.patch(
        '/api/v1/rider/notifications/preferences/update/',
        {'marketing': True, 'promo': True}, format='json',
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()['data']
    assert 'marketing' in body['updated']
    assert body['preferences']['marketing'] is True


@pytest.mark.django_db
def test_notification_prefs_cannot_disable_safety_critical(auth_client_rider):
    client, _ = auth_client_rider
    # Attempting to flip sos / transactional / payout silently ignores
    resp = client.patch(
        '/api/v1/rider/notifications/preferences/update/',
        {'sos': False, 'transactional': False, 'payout': False}, format='json',
    )
    assert resp.status_code == 200
    # Confirm via GET they're still ON
    g = client.get('/api/v1/rider/notifications/preferences/')
    data = g.json()['data']
    assert data['sos'] is True
    assert data['transactional'] is True
    assert data['payout'] is True


@pytest.mark.django_db
def test_create_notification_respects_marketing_optout(auth_client_rider):
    from servers.rider.notifications import create_notification
    from servers.rider.models import NotificationPreference, Notification

    client, user = auth_client_rider
    # Default state: marketing off
    NotificationPreference.objects.get_or_create(user_id=user)
    n = create_notification(user, 'Promo', '50% off!', category='marketing')
    assert n is None
    assert Notification.objects.filter(user_id=user, title='Promo').count() == 0

    # Ride-event is on by default
    n = create_notification(user, 'Driver arrived', '', category='ride_event')
    assert n is not None


# ---------------------------------------------------------------------------
# 5. Ops admin dashboard
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_executive_revenue_view_uses_completed_trip_data(client):
    from decimal import Decimal
    from django.contrib.auth import get_user_model
    from servers.driver.models import VehicleType
    from servers.ride.models import Trip, TripStatus

    User = get_user_model()
    admin_user = User.objects.create_user(
        phone_number='+917000000001',
        role='admin',
        is_staff=True,
        is_superuser=True,
    )
    client.force_login(admin_user)

    status, _ = TripStatus.objects.get_or_create(status_code='completed')
    vehicle_type = VehicleType.objects.create(type='Sedan')
    Trip.objects.create(
        user_id=User.objects.create_user(phone_number='+917000000002', role='rider'),
        driver_id=None,
        requested_vehicle_type=vehicle_type,
        status_id=status,
        pickup_lat=Decimal('17.4'),
        pickup_long=Decimal('78.4'),
        destination_lat=Decimal('17.45'),
        destination_long=Decimal('78.36'),
        estimated_fare=Decimal('500.00'),
        final_fare=Decimal('420.00'),
        completed_at=timezone.now(),
    )

    resp = client.get('/executive_revenue/')
    assert resp.status_code == 200, resp.content
    assert resp.context['gbv'] == Decimal('420.00')
    assert any(item['name'] == 'Sedan' for item in resp.context['class_breakdown'])


@pytest.mark.django_db
def test_admin_dashboard_returns_expected_shape(auth_client_admin):
    client, _ = auth_client_admin
    resp = client.get('/api/v1/ride/admin/dashboard/')
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    assert 'trips' in data
    assert 'gmv' in data
    assert 'drivers' in data
    assert 'riders' in data
    assert 'cancellations' in data
    assert 'withdrawals' in data
    assert 'receipts' in data
    assert data['trips']['total'] == 0


@pytest.mark.django_db
def test_admin_dashboard_requires_admin(auth_client_rider):
    client, _ = auth_client_rider
    resp = client.get('/api/v1/ride/admin/dashboard/')
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 6. Support tickets
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_support_ticket_create_and_list(auth_client_rider):
    client, _ = auth_client_rider
    resp = client.post(
        '/api/v1/support/tickets/create/',
        {'issue_type': 'payment', 'description': 'Charged twice'},
        format='json',
    )
    assert resp.status_code == 201, resp.content
    ticket_id = resp.json()['data']['id']

    resp = client.get('/api/v1/support/tickets/')
    assert resp.status_code == 200
    body = resp.json()
    # Paginated list
    results = body.get('results') or body.get('data', {}).get('results') or body
    # accept either shape
    assert any(t['id'] == ticket_id for t in (results['data'] if isinstance(results, dict) and 'data' in results else results))


@pytest.mark.django_db
def test_support_ticket_user_reply_and_admin_reply(auth_client_rider, auth_client_admin):
    client, _ = auth_client_rider
    create = client.post(
        '/api/v1/support/tickets/create/',
        {'issue_type': 'fare_dispute', 'description': 'Fare too high'},
        format='json',
    )
    assert create.status_code == 201
    ticket_id = create.json()['data']['id']

    # User reply
    r = client.post(
        f'/api/v1/support/tickets/{ticket_id}/messages/',
        {'body': 'Adding more context here'}, format='json',
    )
    assert r.status_code == 201, r.content

    # Admin replies
    admin_client, _ = auth_client_admin
    r2 = admin_client.post(
        f'/api/v1/support/admin/tickets/{ticket_id}/reply/',
        {'body': 'Thanks, looking into it.', 'status': 'IN_PROGRESS'},
        format='json',
    )
    assert r2.status_code == 201, r2.content

    # Detail shows all 3 messages (initial create description + user reply + admin reply)
    detail = client.get(f'/api/v1/support/tickets/{ticket_id}/')
    assert detail.status_code == 200
    msgs = detail.json()['data']['messages']
    assert len(msgs) == 3
    assert detail.json()['data']['status'] == 'IN_PROGRESS'


@pytest.mark.django_db
def test_support_ticket_close(auth_client_rider):
    client, _ = auth_client_rider
    create = client.post(
        '/api/v1/support/tickets/create/',
        {'issue_type': 'other', 'description': 'Lost something in cab'},
        format='json',
    )
    ticket_id = create.json()['data']['id']
    close = client.post(f'/api/v1/support/tickets/{ticket_id}/close/')
    assert close.status_code == 200
    assert close.json()['data']['status'] == 'CLOSED'


@pytest.mark.django_db
def test_support_admin_only_for_admin_routes(auth_client_rider):
    rider_client, _ = auth_client_rider
    resp = rider_client.get('/api/v1/support/admin/tickets/')
    assert resp.status_code in (401, 403)
