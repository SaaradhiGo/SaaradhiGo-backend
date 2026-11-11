"""The driver's ride offer carries the settlement's own earning (2026-11-06).

The offer shows "You earn ₹X · Fare ₹Y − Z% commission ₹W". Those numbers
come from `settlement_amounts`, the arithmetic `credit_driver_wallet` settles
with, so the offer and the ledger cannot disagree. Proven here by settling the
same trip and comparing what the wallet received with what the offer said.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.driver.utils import credit_driver_wallet, settlement_amounts
from servers.ride.dispatch import _offer_money, _offer_payload
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider, Wallet, get_wallet

User = get_user_model()

pytestmark = pytest.mark.django_db

LAT, LNG = Decimal('17.4450000'), Decimal('78.3800000')


def _trip(fare='248', payment_method='online', snapshot=Decimal('15.00'), rider_rating=None):
    rider = User.objects.create_user(phone_number='+919700000981', role='rider', full_name='Priya R')
    r = Rider.objects.create(user_id=rider)
    if rider_rating is not None:
        r.rating, r.rating_count = Decimal(rider_rating), 3
        r.save(update_fields=['rating', 'rating_count'])
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    du = User.objects.create_user(phone_number='+919800000981', role='driver')
    d = Driver.objects.create(user_id=du, approved=True)
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vt, vehicle_number='TS09GP0981')
    status, _ = TripStatus.objects.get_or_create(status_code='pending')
    t = Trip.objects.create(
        user_id=rider, status_id=status, pickup_lat=LAT, pickup_long=LNG,
        destination_lat=LAT, destination_long=LNG, estimated_fare=Decimal(fare),
        payment_method=payment_method, commission_percent_snapshot=snapshot,
    )
    return t, d


def test_the_offer_shows_the_settlements_numbers():
    trip, _ = _trip()
    a = settlement_amounts(trip)
    money = _offer_money(trip)
    assert Decimal(money['commission_rate']) == a['commission_rate'] == Decimal('15.00')
    assert Decimal(money['earning']) + Decimal(money['commission_amount']) == Decimal('248')
    assert Decimal(money['earning']) == Decimal(a['net_minor']) / 100


def test_the_wallet_receives_exactly_what_the_offer_said():
    trip, driver = _trip(fare='185', payment_method='online')
    said = Decimal(_offer_money(trip)['earning'])

    trip.driver_id = driver
    trip.status_id, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip.completed_at = timezone.now()
    trip.save(update_fields=['driver_id', 'status_id', 'completed_at'])
    credit_driver_wallet(trip)

    wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER)
    assert Decimal(wallet.balance_minor) / 100 == said


def test_no_commission_rule_shows_the_fare_alone():
    from servers.pricing.commission import CommissionNotConfigured

    trip, _ = _trip(snapshot=None)
    with mock.patch('servers.pricing.services.commission_percent_for_trip',
                    side_effect=CommissionNotConfigured('none')):
        money = _offer_money(trip)
    assert money['earning'] is None
    assert money['commission_rate'] is None
    assert money['commission_amount'] is None


def test_a_rider_with_no_ratings_has_no_rating_not_five():
    trip, _ = _trip()
    assert _offer_money(trip)['rider_rating'] is None


def test_a_rated_rider_shows_their_rating():
    trip, _ = _trip(rider_rating='4.60')
    assert _offer_money(trip)['rider_rating'] == 4.6


def test_the_payload_keeps_its_old_fields_and_adds_the_new():
    trip, _ = _trip()
    p = _offer_payload(trip, 'Priya', 'sedan')
    for key in ('trip_id', 'estimated_fare', 'currency', 'payment_method', 'vehicle_type'):
        assert key in p
    for key in ('earning', 'commission_rate', 'commission_amount', 'rider_rating'):
        assert key in p
