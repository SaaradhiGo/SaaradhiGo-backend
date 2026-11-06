"""The rider pays the fare on the ticket (owner ruling, 2026-11-06).

QA trip 136: the ticket said ₹101 and the trip booked at ₹102, because booking
priced the trip again. Now every estimate is a quote; booking with it inside its
lifetime, for the same trip, books at exactly that fare. Otherwise the booking is
refused with QUOTE_EXPIRED and a fresh quote, and nothing is booked.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.consumers import RideRequestConsumer
from servers.ride import quotes
from servers.ride.models import FarePricing, Trip, TripStatus
from servers.ride.utils import estimate_amount as _real_estimate
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

PICKUP = ('17.4450', '78.3800')
DROP = ('17.4660', '78.3800')


@pytest.fixture
def rider():
    u = User.objects.create_user(phone_number='+919566000091', role='rider')
    Rider.objects.create(user_id=u)
    TripStatus.objects.get_or_create(status_code='requested')
    return u


def _quote(user, vehicle_type='sedan'):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    r = c.post('/api/v1/ride/estimate-fare/', {
        'pickup_lat': PICKUP[0], 'pickup_long': PICKUP[1],
        'destination_lat': DROP[0], 'destination_long': DROP[1],
        'distance_km': '2.4', 'duration_min': '8', 'vehicle_type': vehicle_type,
    }, format='json')
    assert r.status_code == 200, r.content
    body = r.json()
    return body.get('data', body)


def _book(user, quote_id, pickup=PICKUP, drop=DROP, vehicle_type='sedan'):
    consumer = RideRequestConsumer.__new__(RideRequestConsumer)
    consumer.user = user
    consumer._promo_refusal = ''
    consumer._booking_refusal = None
    consumer._fresh_quote = None
    with mock.patch('servers.ride.tasks.auto_cancel_trip.apply_async'), \
            mock.patch('servers.redis_client.cache_trip'):
        trip, _ = RideRequestConsumer.__dict__['_create_trip'].__wrapped__(
            consumer, pickup[0], pickup[1], drop[0], drop[1],
            'HITEC City', 'Inorbit Mall', distance_km='2.4', duration_min='8',
            vehicle_type=vehicle_type, payment_method='cash', quote_id=quote_id)
    return trip, consumer


def _priced_differently(*args, **kwargs):
    """What booking would charge if it priced the trip again: ₹1 more."""
    fare = dict(_real_estimate(*args, **kwargs))
    fare['total_fare'] = fare['total_fare'] + Decimal('1')
    return fare


def test_the_estimate_is_a_quote():
    q = _quote(User.objects.create_user(phone_number='+919566000092', role='rider'))
    assert q['quote_id']
    assert q['quote_expires_at']


def test_booking_inside_the_lifetime_books_the_quoted_fare_to_the_paisa(rider):
    q = _quote(rider)
    with mock.patch('servers.ride.utils.estimate_amount', side_effect=_priced_differently):
        trip, _ = _book(rider, q['quote_id'])
    assert trip is not None
    assert trip.estimated_fare == Decimal(q['estimated_fare'])
    assert FarePricing.objects.get(trip_id=trip).total_fare == Decimal(q['estimated_fare'])


def test_an_expired_quote_books_nothing_and_offers_a_fresh_one(rider):
    q = _quote(rider)
    cache.delete(quotes.KEY.format(q['quote_id']))
    before = Trip.objects.count()
    trip, consumer = _book(rider, q['quote_id'])
    assert trip is None
    assert Trip.objects.count() == before
    assert consumer._booking_refusal[0] == 'QUOTE_EXPIRED'
    assert consumer._fresh_quote['reason'] == 'expired'
    fresh = consumer._fresh_quote['fresh_quote']
    assert fresh['quote_id'] and fresh['quote_id'] != q['quote_id']
    assert fresh['estimated_fare']


def test_a_quote_past_its_expiry_time_is_expired(rider):
    q = _quote(rider)
    later = timezone.now() + timezone.timedelta(seconds=quotes.DEFAULT_VALID_SECONDS + 1)
    # The cache entry's own timeout is the expiry; simulate it lapsing.
    with mock.patch('servers.ride.quotes.cache.get', return_value=None):
        trip, consumer = _book(rider, q['quote_id'])
    assert trip is None and consumer._fresh_quote['reason'] == 'expired'
    assert later > timezone.now()


def test_a_moved_pickup_books_nothing(rider):
    q = _quote(rider)
    trip, consumer = _book(rider, q['quote_id'], pickup=('17.4470', '78.3800'))  # ~220 m
    assert trip is None
    assert consumer._booking_refusal[0] == 'QUOTE_EXPIRED'
    assert consumer._fresh_quote['reason'] == 'moved'


def test_another_class_books_nothing(rider):
    q = _quote(rider, vehicle_type='sedan')
    trip, consumer = _book(rider, q['quote_id'], vehicle_type='suv')
    assert trip is None and consumer._fresh_quote['reason'] == 'moved'


def test_another_riders_quote_is_not_theirs(rider):
    other = User.objects.create_user(phone_number='+919566000093', role='rider')
    q = _quote(other)
    trip, consumer = _book(rider, q['quote_id'])
    assert trip is None and consumer._fresh_quote['reason'] == 'expired'


def test_no_quote_is_priced_at_booking_as_before(rider):
    trip, _ = _book(rider, None)
    assert trip is not None and trip.estimated_fare > 0


def test_the_wallet_receives_the_earning_on_the_locked_fare(rider):
    from servers.driver.models import Driver, Vehicle, VehicleType
    from servers.driver.utils import credit_driver_wallet
    from servers.ride.dispatch import _offer_money
    from servers.rider.models import Wallet, get_wallet

    q = _quote(rider)
    with mock.patch('servers.ride.utils.estimate_amount', side_effect=_priced_differently):
        trip, _ = _book(rider, q['quote_id'])
    offered = Decimal(_offer_money(trip)['earning'])

    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    du = User.objects.create_user(phone_number='+919800000991', role='driver')
    d = Driver.objects.create(user_id=du, approved=True)
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vt, vehicle_number='TS09GP0991')
    trip.driver_id = d
    trip.payment_method = 'online'
    trip.status_id, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip.completed_at = timezone.now()
    trip.save(update_fields=['driver_id', 'payment_method', 'status_id', 'completed_at'])
    credit_driver_wallet(trip)
    assert Decimal(get_wallet(du, Wallet.SCOPE_DRIVER).balance_minor) / 100 == offered


def test_app_config_states_the_quote_lifetime_and_home_radius():
    from servers.platform_ops.app_config import _policy
    p = _policy('IN')
    assert p['quote_valid_seconds'] == 300
    assert p['home_fare_places_max_km'] == 60
    assert quotes.valid_seconds('IN') == 300
    assert quotes.valid_seconds('ZZ') == quotes.DEFAULT_VALID_SECONDS
