"""15 Nov ruling: a completed trip pays the driver even if the rider's payment fails; the platform
carries the loss. The card is retried, the rider cannot book until it is paid, and repeated
failures between one rider and one driver are flagged for fraud review. Online payments only, in
every market."""
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.payments import unpaid
from servers.payments.models import CardHold, Payment, Tip, TripSettlement
from servers.ride.models import Trip, TripStatus
from test_driver_earnings_period import LAT, LNG, _api, driver  # noqa: F401

User = get_user_model()
pytestmark = pytest.mark.django_db


def _rider(n=1):
    return User.objects.create_user(phone_number=f'+91970011{n:04d}', role='rider')


def _failed_trip(driver, rider, *, method='card', fare='240', card=True, payment_failed=True):  # noqa: F811
    status, _ = TripStatus.objects.get_or_create(status_code='completed')
    t = Trip.objects.create(
        user_id=rider, status_id=status, pickup_lat=LAT, pickup_long=LNG, destination_lat=LAT,
        destination_long=LNG, estimated_fare=Decimal(fare), payment_method=method,
        commission_percent_snapshot=Decimal('15.00'))
    t.driver_id = driver
    t.completed_at = timezone.now()
    t.payment_status = 'failed' if card else 'pending'
    t.save(update_fields=['driver_id', 'completed_at', 'payment_status'])
    if payment_failed:
        Payment.objects.create(trip_id=t, user_id=rider, amount=Decimal(fare), method='online',
                               status='failed', gateway_order_id=f'pi_{t.id}')
    if card:
        CardHold.objects.create(user=rider, trip=t, provider='stripe', intent_id=f'pi_{t.id}',
                                amount_minor=int(Decimal(fare) * 100), currency=t.currency or 'INR',
                                customer_id='cus_1', payment_method_id='pm_1', status=CardHold.FAILED)
    return t


class _Card:
    def __init__(self, ok):
        self.ok, self.calls = ok, 0

    def get_name(self):
        return 'stripe'

    def charge_saved(self, *a, **k):
        self.calls += 1
        return {'id': f'pi_retry_{self.calls}', 'status': 'succeeded'} if self.ok else None


def test_the_driver_is_paid_once_for_a_trip_whose_payment_failed(driver):  # noqa: F811
    trip = _failed_trip(driver, _rider())
    with mock.patch('servers.payments.card_hold._gateway', return_value=None):
        assert unpaid.sweep()['settled'] == 1
        assert unpaid.sweep()['settled'] == 0
    s = TripSettlement.objects.get(trip=trip)
    assert s.payment_method == 'card' and s.driver_net_minor == 20400
    assert Trip.objects.get(pk=trip.pk).payment_status == 'failed'
    rows = _api(driver).get('/api/v1/driver/earnings/').json()
    rows = rows.get('data', rows)
    assert trip.id in [r['trip_id_val'] for r in rows['results']]
    assert rows['unsettled'] == []


def test_the_rider_cannot_book_until_it_is_paid(driver):  # noqa: F811
    rider = _rider()
    trip = _failed_trip(driver, rider)
    refusal = unpaid.refusal_for(rider)
    assert refusal['code'] == 'UNPAID_LAST_TRIP' and refusal['message'] == 'Pay for your last trip'
    assert refusal['trip_id'] == trip.id and refusal['amount'] == '240.00'
    from servers.payments.card_hold import create_hold
    from servers.ride import quotes
    with mock.patch.object(quotes.cache, 'get', return_value={'rider_id': rider.id, 'fare': {}}):
        assert create_hold(rider, 'q1')[1][0] == 'UNPAID_LAST_TRIP'


def test_a_card_retry_that_succeeds_clears_the_block_and_pays_nothing_twice(driver):  # noqa: F811
    rider = _rider()
    trip = _failed_trip(driver, rider)
    card = _Card(ok=True)
    with mock.patch('servers.payments.card_hold._gateway', return_value=card):
        unpaid.sweep()
        assert Trip.objects.get(pk=trip.pk).payment_status == 'completed'
    assert card.calls == 1
    assert unpaid.refusal_for(rider) is None
    assert TripSettlement.objects.filter(trip=trip).count() == 1
    assert Payment.objects.filter(trip_id=trip, status='completed').count() == 1


def test_a_declined_card_is_retried_on_a_schedule_and_at_most_five_times(driver):  # noqa: F811
    from datetime import timedelta

    rider = _rider()
    trip = _failed_trip(driver, rider)
    card = _Card(ok=False)
    now = timezone.now()
    with mock.patch('servers.payments.card_hold._gateway', return_value=card):
        assert unpaid.retry_card(trip, now=now) == 'declined'
        assert unpaid.retry_card(trip, now=now + timedelta(hours=1)) == 'not_due'
        for i in range(2, 8):
            unpaid.retry_card(trip, now=now + timedelta(hours=6 * i))
    assert card.calls == unpaid.MAX_RETRIES
    assert unpaid.refusal_for(rider) is not None


def test_the_rider_pays_now_from_the_banner(driver):  # noqa: F811
    from rest_framework.test import APIClient

    rider = _rider()
    _failed_trip(driver, rider)
    client = APIClient()
    client.force_authenticate(user=rider)
    body = client.get('/api/v1/payments/unpaid/').json()
    assert body.get('data', body)['unpaid']['can_retry_card'] is True
    with mock.patch('servers.payments.card_hold._gateway', return_value=_Card(ok=False)):
        r = client.post('/api/v1/payments/unpaid/pay/')
        assert r.status_code == 402
    with mock.patch('servers.payments.card_hold._gateway', return_value=_Card(ok=True)):
        r = client.post('/api/v1/payments/unpaid/pay/')
        assert r.status_code == 200 and r.json().get('data', r.json())['result'] == 'paid'
    body = client.get('/api/v1/payments/unpaid/').json()
    assert body.get('data', body)['unpaid'] is None


def test_repeated_failures_with_one_driver_raise_a_fraud_flag(driver):  # noqa: F811
    from servers.fraud.models import RiskFlag

    rider = _rider()
    first = _failed_trip(driver, rider)
    unpaid.on_payment_failed(first)
    assert not RiskFlag.objects.filter(kind='unpaid_pair').exists()
    second = _failed_trip(driver, rider)
    unpaid.on_payment_failed(second)
    flag = RiskFlag.objects.get(kind='unpaid_pair', subject=rider)
    assert sorted(flag.evidence['refs']['trip_ids']) == sorted([first.id, second.id])
    assert flag.evidence['refs']['counterpart_user_ids'] == [driver.user_id_id]
    # Another rider with one failure on the same driver raises nothing.
    unpaid.on_payment_failed(_failed_trip(driver, _rider(2)))
    assert RiskFlag.objects.filter(kind='unpaid_pair').count() == 1


def test_india_online_payment_failed_counts_switching_and_cash_do_not(driver):  # noqa: F811
    rider = _rider()
    upi = _failed_trip(driver, rider, method='upi', card=False)
    assert unpaid.unpaid_trip_for(rider) == upi
    with mock.patch('servers.payments.card_hold._gateway', return_value=None):
        unpaid.sweep()
    assert TripSettlement.objects.filter(trip=upi).exists()
    assert unpaid.summary(upi)['can_retry_card'] is False

    other = _rider(3)
    switched = _failed_trip(driver, other, method='upi', card=False)
    Payment.objects.filter(trip_id=switched).update(switch_reason='user_requested')
    cash = _failed_trip(driver, other, method='cash', card=False)
    never_tried = _failed_trip(driver, other, method='upi', card=False, payment_failed=False)
    assert unpaid.unpaid_trip_for(other) is None
    assert not {switched.id, cash.id, never_tried.id} & set(unpaid.unpaid_trips().values_list('id', flat=True))


def test_ride_history_sends_the_total_charged(driver):  # noqa: F811
    from rest_framework.test import APIClient

    rider = _rider()
    trip = _failed_trip(driver, rider)
    Tip.objects.create(trip=trip, rider=rider, driver=driver, amount_minor=4300, currency=trip.currency,
                       status='succeeded')
    client = APIClient()
    client.force_authenticate(user=rider)
    body = client.get('/api/v1/ride/ride-history/').json()
    body = body.get('data', body)
    row = next(r for r in body['results'] if r['id'] == trip.id)
    assert Decimal(row['total_charged']) == Decimal('283.00')
