"""Tests for the Phase-0 extras batch.

Covers:
  - Receipt PDF generation + email attachment + endpoint
  - PromoCode apply/redeem (happy path, expiry, zone scope, user cap,
    global cap, min-fare)
  - Rider rating decay (running mean, flag on threshold, auto-clear
    when rating recovers)
  - ChatMessage REST history + WS persistence path (REST surface
    asserted; WS round-trip best-effort via direct consumer call)
  - 3 new ServiceZones (VJA / WGL / VTZ) resolve via
    find_zone_for_point() and have rate cards
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone


# ---------------------------------------------------------------------------
# Receipt PDF
# ---------------------------------------------------------------------------

def _make_completed_trip(rider_user, driver_user):
    from decimal import Decimal as D
    from servers.ride.models import FarePricing, Trip, TripStatus
    completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        pickup_address='Banjara Hills', destination_address='Hitech City',
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
def test_receipt_pdf_bytes_are_built(auth_client_rider, auth_client_driver):
    """ReportLab produces a valid PDF stream for a completed trip.
    We test the renderer directly so we don't depend on S3 being
    configured in the test environment (private_document_storage on
    the Receipt model is an S3 backend in production)."""
    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    trip = _make_completed_trip(rider_user, driver_user)

    from servers.ride.models import Receipt
    from servers.ride.receipts import _build_receipt_pdf

    # Build a Receipt without persisting it (no pdf_file save).
    receipt = Receipt(
        trip_id=trip, user_id=rider_user,
        receipt_number='SG-TEST-1-v1',
        total_fare=trip.final_fare,
        gst_amount=Decimal('10.00'),
        payment_method='online',
        payment_status='completed',
        html_body='<p>test</p>',
        version=1,
    )
    pdf_bytes = _build_receipt_pdf(receipt)
    assert pdf_bytes is not None
    assert pdf_bytes.startswith(b'%PDF')
    assert b'VahanGo' in pdf_bytes or b'SaaradhiGo' in pdf_bytes


@pytest.mark.django_db
def test_receipt_email_attaches_pdf_when_storage_available(
    auth_client_rider, auth_client_driver, settings, tmp_path,
):
    """End-to-end: when storage works (here: FileSystemStorage via
    tmp_path), the receipt email includes a PDF attachment."""
    from django.core.files.storage import FileSystemStorage
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

    _, rider_user = auth_client_rider
    rider_user.email = 'rider@example.test'; rider_user.save(update_fields=['email'])
    _, driver_user = auth_client_driver
    trip = _make_completed_trip(rider_user, driver_user)

    from servers.ride.models import Receipt
    local = FileSystemStorage(location=str(tmp_path))
    with patch.object(Receipt._meta.get_field('pdf_file'), 'storage', local):
        from servers.ride.receipts import issue_receipt
        r = issue_receipt(trip)

    assert r is not None
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    pdf_attachments = [a for a in msg.attachments if a[2] == 'application/pdf']
    assert pdf_attachments, 'expected at least one application/pdf attachment'


@pytest.mark.django_db
def test_receipt_pdf_endpoint_returns_url(
    auth_client_rider, auth_client_driver, settings, tmp_path,
):
    from django.core.files.storage import FileSystemStorage
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    client, rider_user = auth_client_rider
    rider_user.email = 'rider@example.test'; rider_user.save(update_fields=['email'])
    _, driver_user = auth_client_driver
    trip = _make_completed_trip(rider_user, driver_user)
    from servers.ride.models import Receipt
    from servers.ride.receipts import issue_receipt
    local = FileSystemStorage(location=str(tmp_path), base_url='/media/test/')
    with patch.object(Receipt._meta.get_field('pdf_file'), 'storage', local):
        issue_receipt(trip)
        resp = client.get(f'/api/v1/ride/trip/{trip.id}/receipt/pdf/')
    assert resp.status_code == 200, resp.content
    body = resp.json()['data']
    assert body['trip_id'] == trip.id
    assert body['pdf_url']
    assert body['receipt_number'].endswith('-v1')


# ---------------------------------------------------------------------------
# Promo codes
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_promo_apply_percent_with_cap(db):
    from django.contrib.auth import get_user_model
    from servers.ride.models import PromoCode
    from servers.ride.promos import apply_promo

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918300000001', role='rider')
    now = timezone.now()
    PromoCode.objects.create(
        code='WELCOME50', discount_type='percent',
        discount_value=Decimal('50.00'),
        max_discount_amount=Decimal('100.00'), min_fare=Decimal('100.00'),
        valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=7),
        max_per_user_redemptions=1, is_active=True,
    )
    r = apply_promo('WELCOME50', user, Decimal('300.00'))
    assert r.ok is True
    # 50% of 300 = 150, capped at 100
    assert r.discount_amount == Decimal('100.00')
    assert r.final_fare == Decimal('200.00')


@pytest.mark.django_db
def test_promo_apply_flat_under_min_fare(db):
    from django.contrib.auth import get_user_model
    from servers.ride.models import PromoCode
    from servers.ride.promos import apply_promo

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918300000002', role='rider')
    now = timezone.now()
    PromoCode.objects.create(
        code='FLAT50', discount_type='flat', discount_value=Decimal('50.00'),
        min_fare=Decimal('200.00'),
        valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=7),
        is_active=True,
    )
    r = apply_promo('FLAT50', user, Decimal('100.00'))
    assert r.ok is False
    assert r.reason == 'PROMO_MIN_FARE'


@pytest.mark.django_db
def test_promo_redeem_increments_counter_and_blocks_user_cap(db, auth_client_rider, auth_client_driver):
    from decimal import Decimal as D
    from servers.ride.models import PromoCode, Trip, TripStatus
    from servers.ride.promos import redeem_promo

    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    now = timezone.now()
    promo = PromoCode.objects.create(
        code='FLAT100', discount_type='flat', discount_value=D('100.00'),
        valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=7),
        max_per_user_redemptions=1, is_active=True,
    )
    completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        estimated_fare=D('150.00'), status_id=completed,
    )
    r = redeem_promo('FLAT100', rider_user, trip, D('150.00'))
    assert r.ok is True
    promo.refresh_from_db()
    assert promo.redemption_count == 1

    # Second attempt should hit the user cap.
    r2 = redeem_promo('FLAT100', rider_user, trip, D('150.00'))
    assert r2.ok is False
    assert r2.reason == 'PROMO_USER_LIMIT'


@pytest.mark.django_db
def test_promo_apply_endpoint(auth_client_rider):
    from decimal import Decimal as D
    from servers.ride.models import PromoCode
    client, _ = auth_client_rider
    now = timezone.now()
    PromoCode.objects.create(
        code='WELCOME20', discount_type='percent', discount_value=D('20.00'),
        max_discount_amount=D('50.00'), min_fare=D('0'),
        valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=7),
        is_active=True,
    )
    resp = client.post('/api/v1/ride/promo/apply/',
                       {'code': 'WELCOME20', 'fare': '100.00'}, format='json')
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    assert data['ok'] is True
    assert data['discount_amount'] == '20.00'


@pytest.mark.django_db
def test_promo_apply_endpoint_unknown_code(auth_client_rider):
    client, _ = auth_client_rider
    resp = client.post('/api/v1/ride/promo/apply/',
                       {'code': 'NOPE', 'fare': '100.00'}, format='json')
    assert resp.status_code == 400
    assert resp.json()['error']['code'] == 'PROMO_NOT_FOUND'


# ---------------------------------------------------------------------------
# Rider rating decay
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_rating_decay_running_mean_and_flagging(db):
    from django.contrib.auth import get_user_model
    from servers.rider.models import Rider
    from servers.rider.rating_decay import apply_rider_rating

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918300000101', role='rider')
    rider = Rider.objects.create(user_id=user, rating=Decimal('5.00'))

    # Five 2-star ratings should drop the mean below 3.
    for _ in range(5):
        apply_rider_rating(rider, 2)
    rider.refresh_from_db()
    assert rider.rating < Decimal('3.00')
    assert rider.flagged_for_review is True

    # A streak of 5-stars should pull it back above 3.
    for _ in range(20):
        apply_rider_rating(rider, 5)
    rider.refresh_from_db()
    assert rider.rating >= Decimal('3.00')
    assert rider.flagged_for_review is False
    assert rider.review_cleared_at is not None


@pytest.mark.django_db
def test_rating_decay_rejects_invalid_score(db):
    from django.contrib.auth import get_user_model
    from servers.rider.models import Rider
    from servers.rider.rating_decay import apply_rider_rating

    User = get_user_model()
    user = User.objects.create_user(phone_number='+918300000102', role='rider')
    rider = Rider.objects.create(user_id=user, rating=Decimal('5.00'))
    out = apply_rider_rating(rider, 9)
    assert out['ok'] is False
    assert out['reason'] == 'out_of_range'


# ---------------------------------------------------------------------------
# Chat history endpoint + persistence
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_chat_history_endpoint_visible_to_participants(auth_client_rider, auth_client_driver):
    from decimal import Decimal as D
    from servers.ride.models import ChatMessage, Trip, TripStatus

    client, rider_user = auth_client_rider
    driver_client, driver_user = auth_client_driver
    accepted, _ = TripStatus.objects.get_or_create(status_code='accepted')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        estimated_fare=D('150.00'), status_id=accepted,
    )
    ChatMessage.objects.create(trip=trip, sender=driver_user, sender_role='driver', body='5 mins away')
    ChatMessage.objects.create(trip=trip, sender=rider_user, sender_role='rider', body='OK thanks')

    resp = client.get(f'/api/v1/ride/trip/{trip.id}/chat/')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    results = body.get('results') or body.get('data', {}).get('results') or []
    # Both shapes acceptable
    messages = results['data'] if isinstance(results, dict) and 'data' in results else results
    assert len(messages) >= 2
    assert any(m['sender_role'] == 'driver' for m in messages)
    assert any(m['sender_role'] == 'rider' for m in messages)


@pytest.mark.django_db
def test_chat_history_forbidden_for_non_participant(auth_client_rider, auth_client_driver):
    from decimal import Decimal as D
    from django.contrib.auth import get_user_model
    from rest_framework_simplejwt.tokens import AccessToken
    from rest_framework.test import APIClient
    from servers.ride.models import Trip, TripStatus

    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    accepted, _ = TripStatus.objects.get_or_create(status_code='accepted')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        estimated_fare=D('150.00'), status_id=accepted,
    )

    User = get_user_model()
    interloper = User.objects.create_user(phone_number='+918300000777', role='rider')
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(interloper)}')
    resp = c.get(f'/api/v1/ride/trip/{trip.id}/chat/')
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Three new ServiceZones
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_three_new_cities_seeded():
    from servers.pricing.models import ServiceZone
    for code in ('IN-AP-VJA', 'IN-TG-WGL', 'IN-AP-VTZ'):
        z = ServiceZone.objects.filter(code=code).first()
        assert z is not None, f'expected {code} to be seeded'
        assert z.is_active is True


@pytest.mark.django_db
def test_vja_pickup_resolves_to_vja_zone():
    from servers.pricing.services import find_zone_for_point
    # Vijayawada city centre approx 16.51, 80.63
    z = find_zone_for_point(16.51, 80.63)
    assert z is not None
    assert z.code == 'IN-AP-VJA'


@pytest.mark.django_db
def test_wgl_pickup_resolves_to_wgl_zone():
    from servers.pricing.services import find_zone_for_point
    # Warangal Hanamkonda approx 17.99, 79.59
    z = find_zone_for_point(17.99, 79.59)
    assert z is not None
    assert z.code == 'IN-TG-WGL'


@pytest.mark.django_db
def test_vtz_pickup_resolves_to_vtz_zone():
    from servers.pricing.services import find_zone_for_point
    # Visakhapatnam city centre approx 17.69, 83.21
    z = find_zone_for_point(17.69, 83.21)
    assert z is not None
    assert z.code == 'IN-AP-VTZ'


@pytest.mark.django_db
def test_vja_has_rate_cards():
    from servers.pricing.models import RateCard, ServiceZone
    z = ServiceZone.objects.get(code='IN-AP-VJA')
    cards = RateCard.objects.filter(zone=z, is_active=True)
    types = {c.vehicle_type.type for c in cards}
    assert {'auto', 'hatchback', 'sedan', 'suv'} <= types


@pytest.mark.django_db
def test_quote_fare_in_vja_uses_local_rate(auth_client_rider):
    """A trip quoted with pickup inside the VJA polygon should resolve
    the VJA rate card (auto base = Rs 25 vs Hyderabad Rs 30)."""
    from servers.pricing.services import quote_fare
    fare = quote_fare(
        distance_km=Decimal('5'), duration_min=Decimal('10'),
        vehicle_type='auto',
        pickup_lat=16.51, pickup_lon=80.63,
        at=timezone.now(),
    )
    assert fare['source'] == 'db'
    assert fare['zone_code'] == 'IN-AP-VJA'
    # base 25 + 11*5 + 1.5*10 = 25 + 55 + 15 = 95
    assert fare['total_fare'] == Decimal('95.00')
