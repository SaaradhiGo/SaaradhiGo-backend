"""USA payments (14 Nov): card held at booking for the locked fare, captured at trip end, released on
cancel; tips after the trip, 100% to the driver; receipt emails in the message log."""
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.payments.models import CardHold, Payment, Tip
from servers.pricing.models import ServiceZone
from servers.ride import quotes
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider, WalletTransaction
from tests.conftest import seed_admin_access

User = get_user_model()
pytestmark = pytest.mark.django_db
FAKE = override_settings(CARD_FAKE_ALLOWED=True, IS_PRODUCTION=False)


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


@pytest.fixture
def world():
    seed_admin_access()
    zone = ServiceZone.objects.get(code='US-ID-TV')
    rider = User.objects.create_user(phone_number='+12085550401', role='rider', email='rider@example.test')
    Rider.objects.create(user_id=rider)
    du = User.objects.create_user(phone_number='+12085550402', role='driver')
    driver = Driver.objects.create(user_id=du, approved=True)
    vehicle = Vehicle.objects.create(driver_id=driver, vehicle_type_id=VehicleType.objects.get(type='standard'),
                                     vehicle_number='1TV0001', doors=4, year=2019)
    return {'zone': zone, 'rider': rider, 'driver': driver, 'vehicle': vehicle}


def _quote(rider, total='18.40'):
    qid, _ = quotes.issue({'total_fare': Decimal(total), 'currency': 'USD', 'vehicle_type': 'standard',
                           'zone_code': 'US-ID-TV'}, rider_id=rider.id, pickup=(43.615, -116.202),
                          drop=(43.60, -116.39), validated_km=10, validated_min=20)
    return qid


def _trip(w, status='completed', fare='18.40'):
    now = timezone.now()
    return Trip.objects.create(
        user_id=w['rider'], driver_id=w['driver'], vehicle_id=w['vehicle'], zone=w['zone'],
        status_id=TripStatus.objects.get_or_create(status_code=status)[0],
        pickup_lat=Decimal('43.615'), pickup_long=Decimal('-116.202'), destination_lat=Decimal('43.60'),
        destination_long=Decimal('-116.39'), estimated_fare=Decimal(fare), final_fare=Decimal(fare),
        payment_method='card', accepted_at=now, completed_at=now if status == 'completed' else None)


@FAKE
def test_the_hold_is_for_exactly_the_locked_fare(world):
    res = _client(world['rider']).post('/api/v1/payments/card/hold/', {'quote_id': _quote(world['rider'])},
                                       format='json')
    assert res.status_code == 201, res.content
    data = res.json()['data']
    assert data['amount'] == '18.40' and data['currency'] == 'USD' and data['test_mode'] is True
    hold = CardHold.objects.get(pk=data['hold_id'])
    assert hold.amount_minor == 1840 and hold.status == CardHold.AUTHORISED
    # Another rider cannot hold on this quote.
    other = User.objects.create_user(phone_number='+12085550499', role='rider')
    qid = _quote(world['rider'])
    assert _client(other).post('/api/v1/payments/card/hold/', {'quote_id': qid}, format='json').status_code == 409


@FAKE
def test_no_card_processor_in_production_means_no_hold(world):
    with override_settings(IS_PRODUCTION=True):
        res = _client(world['rider']).post('/api/v1/payments/card/hold/', {'quote_id': _quote(world['rider'])},
                                           format='json')
    assert res.status_code == 409 and res.json()['error']['code'] == 'CARD_UNAVAILABLE'


@FAKE
def test_captured_at_trip_end_and_settled_once(world):
    from servers.payments.card_hold import capture_for_trip, create_hold, link_to_trip

    hold, _ = create_hold(world['rider'], _quote(world['rider']))
    trip = _trip(world, status='accepted')
    assert link_to_trip(world['rider'], hold.id, trip) is None
    Trip.objects.filter(pk=trip.pk).update(status_id=TripStatus.objects.get_or_create(status_code='completed')[0],
                                           completed_at=timezone.now())
    with mock.patch('servers.driver.utils.settle_paid_by_platform') as settle:
        assert capture_for_trip(trip.id) == 'captured'
        assert capture_for_trip(trip.id) == 'nothing to do'
    settle.assert_called_once()
    trip.refresh_from_db()
    hold.refresh_from_db()
    assert trip.payment_status == 'completed' and hold.status == CardHold.CAPTURED and hold.captured_minor == 1840
    assert Payment.objects.get(trip_id=trip, gateway_order_id=hold.intent_id).status == 'completed'


@FAKE
def test_a_failed_capture_goes_to_the_existing_retry_path(world):
    from servers.payments.card_hold import capture_for_trip, create_hold, link_to_trip

    hold, _ = create_hold(world['rider'], _quote(world['rider']))
    trip = _trip(world)
    link_to_trip(world['rider'], hold.id, trip)
    with mock.patch('integrations.payments.fake_card.FakeCardGateway.capture_hold',
                    return_value={'error': 'card_declined'}):
        assert capture_for_trip(trip.id) == 'failed'
    trip.refresh_from_db()
    assert trip.payment_status == 'failed'      # the unsettled-trip retry path picks it up


@FAKE
def test_a_cancelled_trip_releases_the_hold(world):
    from servers.payments.card_hold import create_hold, link_to_trip, release_for_trip

    hold, _ = create_hold(world['rider'], _quote(world['rider']))
    trip = _trip(world, status='accepted')
    link_to_trip(world['rider'], hold.id, trip)
    assert release_for_trip(trip) is True
    hold.refresh_from_db()
    assert hold.status == CardHold.RELEASED


@FAKE
def test_a_tip_goes_100_percent_to_the_driver_and_onto_the_receipt(world):
    from servers.payments.card_hold import capture_for_trip, create_hold, link_to_trip

    hold, _ = create_hold(world['rider'], _quote(world['rider']))
    trip = _trip(world)
    link_to_trip(world['rider'], hold.id, trip)
    capture_for_trip(trip.id)
    c = _client(world['rider'])
    res = c.post(f'/api/v1/ride/trips/{trip.id}/tip/', {'percent': 18}, format='json')
    assert res.status_code == 201, res.content
    assert res.json()['data']['amount'] == '3.31' and res.json()['data']['to_driver'] == '100%'   # 18% of 18.40
    credit = WalletTransaction.objects.get(purpose='tip', reference_id=f'TRIP_{trip.id}')
    assert credit.amount_minor == 331 and credit.user_id_id == world['driver'].user_id_id
    assert c.post(f'/api/v1/ride/trips/{trip.id}/tip/', {'amount': '2.00'}, format='json').status_code == 409
    from servers.ride.receipts import _tip_rows_html
    assert 'Tip (100% to your driver)' in _tip_rows_html(trip, 'USD')


@FAKE
def test_no_tips_in_india(world):
    from servers.payments.card_hold import give_tip

    trip = _trip(world)
    with mock.patch('servers.pricing.regulation.rules_for_zone', return_value={'tips_enabled': False}):
        tip, err = give_tip(world['rider'], trip, percent=15)
    assert tip is None and err[0] == 'TIPS_OFF'
    assert not Tip.objects.exists()


def test_a_receipt_email_is_in_the_message_log_even_when_not_sent(world):
    from servers.notifications.models import NotificationMessage
    from servers.ride.models import Receipt
    from servers.ride.receipts import _send_receipt_email

    trip = _trip(world)
    receipt = Receipt.objects.create(trip_id=trip, user_id=world['rider'], receipt_number='SG-TEST-1',
                                     total_fare=Decimal('18.40'), gst_amount=Decimal('0'), currency='USD',
                                     sent_to_email='rider@example.test', html_body='<p>x</p>')
    with mock.patch('servers.ride.receipts.send_message', return_value=(False, 'email is not configured')):
        _send_receipt_email(receipt)
    row = NotificationMessage.objects.get(template_key='trip_receipt')
    assert row.channel == 'email' and row.status == 'failed' and 'not configured' in row.last_error
    assert row.recipient_masked == 'ri***@example.test'


@override_settings(STRIPE_SECRET_KEY='sk_test_unit', IS_PRODUCTION=False)
def test_the_stripe_hold_is_manual_capture_with_apple_and_google_pay():
    from integrations.payments.stripe import StripeGateway

    with mock.patch('integrations.stripe_http.request', return_value=(200, {'id': 'pi_1', 'client_secret': 's'})) as rq:
        out = StripeGateway().create_hold(1840, currency='USD', customer_id='cus_1', reference='r1')
    assert out['id'] == 'pi_1'
    method, path, body = rq.call_args[0][:3]
    assert (method, path) == ('POST', '/payment_intents')
    assert body['capture_method'] == 'manual' and body['amount'] == 1840 and body['currency'] == 'usd'
    assert body['automatic_payment_methods'] == {'enabled': 'true'}      # cards, Apple Pay, Google Pay
    assert body['setup_future_usage'] == 'off_session' and body['customer'] == 'cus_1'
    with mock.patch('integrations.stripe_http.request', return_value=(200, {'status': 'succeeded'})) as rq:
        StripeGateway().capture_hold('pi_1', 1800, reference='t1')
    assert rq.call_args[0][1] == '/payment_intents/pi_1/capture' and rq.call_args[0][2] == {'amount_to_capture': 1800}
