"""A trip ended under the platform-funded rule is never charged afterwards (review B2 H1, AUDIT 27.77).

An operator end for a safety incident or a platform fault sets `final_fare_minor = 0`: the rider pays
nothing and the platform pays the driver their earnings. `fare_minor` read `final or estimated`, and 0
is falsy, so the payable became the whole upfront fare; `POST /rider/wallet/payment/`, the payment
switch and the retry never asked about the rule, and settlement then credited the driver
`trip_earnings` on top of the platform's credit.
"""

import pytest

from promotions_support import client_for, geo, hyd_driver, make_rider, world  # noqa: F401
from finance_world import make_trip
from servers.payments.models import Payment
from servers.rider.models import Wallet, WalletTransaction, get_wallet

pytestmark = pytest.mark.django_db


@pytest.fixture
def free_trip(world):
    rider, driver = make_rider('F', '91'), hyd_driver(world, 'F')
    trip = make_trip(world.hyd, rider, driver, fare='53.00', method='cash')
    trip.end_rule, trip.end_reason_code = 'platform_funded', 'safety_incident'
    trip.final_fare_minor, trip.platform_funded_minor = 0, 3_920
    trip.payment_status = 'waived'
    trip.save(update_fields=['end_rule', 'end_reason_code', 'final_fare_minor',
                             'platform_funded_minor', 'payment_status'])
    trip.estimated_fare_minor = 5_300
    trip.save(update_fields=['estimated_fare_minor'])
    wallet = get_wallet(rider)
    wallet.balance_minor, wallet.currency = 50_000, 'INR'
    wallet.save()
    return trip


def test_the_payable_is_zero(free_trip):
    from servers.promotions.rider_promos import fare_minor, not_charged, payable_minor

    assert (fare_minor(free_trip), payable_minor(free_trip)) == (0, 0)
    assert 'not charged' in not_charged(free_trip)


def test_the_wallet_payment_is_refused_and_nothing_moves(free_trip):
    r = client_for(free_trip.user_id).post('/api/v1/rider/wallet/payment/',
                                           {'trip_id': free_trip.id}, format='json')
    assert r.status_code == 409 and r.json()['error']['code'] == 'NOT_CHARGED'
    assert get_wallet(free_trip.user_id).balance_minor == 50_000
    assert not WalletTransaction.objects.filter(user_id=free_trip.user_id,
                                                txn_type='debit').exists()


def test_settlement_never_credits_the_driver_again(free_trip):
    from servers.driver.utils import credit_driver_wallet

    credit_driver_wallet(free_trip)
    assert not WalletTransaction.objects.filter(purpose='trip_earnings').exists()
    assert not Wallet.objects.filter(user_id=free_trip.driver_id.user_id, scope='driver',
                                     balance_minor__gt=0).exists()


def test_switching_or_retrying_is_refused(free_trip, monkeypatch):
    class Gateway:
        def get_order_status(self, order_id):
            return {'order_status': 'ACTIVE'}

        def get_name(self):
            return 'razorpay'

        def supported_currencies(self):
            return ('INR',)

        def create_order(self, *a, **k):            # pragma: no cover - must never be reached
            raise AssertionError('an order was created for a free trip')

    monkeypatch.setattr('servers.payments.views.get_payment_gateway_for_payments',
                        lambda *a: Gateway())
    Payment.objects.create(trip_id=free_trip, user_id=free_trip.user_id, amount_minor=0,
                           method='cash', status='pending')
    client = client_for(free_trip.user_id)
    switched = client.post('/api/v1/payments/switch/', {'trip_id': free_trip.id,
                                                        'new_method': 'online'}, format='json')
    assert switched.status_code == 409 and switched.json()['error']['code'] == 'NOT_CHARGED'
    Payment.objects.filter(trip_id=free_trip).update(status='failed')
    retried = client.post('/api/v1/payments/retry/', {'trip_id': free_trip.id}, format='json')
    assert retried.status_code == 409 and retried.json()['error']['code'] == 'NOT_CHARGED'


def test_a_normal_trip_still_falls_back_to_the_estimate(world):
    from servers.promotions.rider_promos import fare_minor

    trip = make_trip(world.hyd, make_rider('N', '91'), hyd_driver(world, 'N'), fare='80.00')
    trip.final_fare_minor = None
    trip.save(update_fields=['final_fare_minor'])
    assert fare_minor(trip) == 8_000
