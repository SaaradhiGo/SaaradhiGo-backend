"""A cash trip switched to online after completion settles the driver exactly once (AUDIT 27.72).

A cash trip is settled at completion ("the driver holds the fare, owes the commission"). When the rider
then switches to online and pays, every online-completion path called `credit_driver_wallet`, which
the existing cash settlement's key suppressed: the driver owed the commission and was never paid the
fare the platform had collected. Now each path goes through `settle_paid_by_platform`, so the driver
ends with the trip's net, the same as a trip paid online from the start, and the settlement's current
version says it was paid online.
"""

from decimal import Decimal

import pytest

from promotions_support import client_for, geo, hyd_driver, make_rider, world  # noqa: F401
from finance_world import make_trip
from servers.payments.models import Payment, TripSettlement
from servers.rider.models import Wallet, WalletTransaction

pytestmark = [pytest.mark.django_db]

SWITCH = '/api/v1/payments/switch/'
NET, COMMISSION, FARE = 17_000, 3_000, 20_000


class FakeGateway:
    """Takes an order, then reports it PAID once `paid` is set. No network."""

    def __init__(self):
        self.paid = False
        self.orders = 0

    def get_name(self):
        return 'razorpay'

    def supported_currencies(self):
        return ('INR',)

    def create_order(self, amount_minor, trip_id=None, currency='INR', **_):
        self.orders += 1
        return {'order_id': f'order_{trip_id}_{self.orders}'}

    def get_order_status(self, order_id):
        return {'order_status': 'PAID' if self.paid else 'ACTIVE'}


@pytest.fixture
def gateway(monkeypatch):
    g = FakeGateway()
    monkeypatch.setattr('servers.payments.views.get_payment_gateway_for_payments', lambda *a: g)
    return g


def complete(world, rider, driver, method):
    from servers.consumers import TripStatusConsumer

    trip = make_trip(world.hyd, rider, driver, fare='200.00', method=method)
    trip.payment_status = 'pending'
    trip.save(update_fields=['payment_status'])
    TripStatusConsumer()._create_payment_on_complete(trip)
    if method == 'online':
        # The order is created by the app's create-order call, not at completion.
        Payment.objects.filter(trip_id=trip, method='online').update(
            gateway_order_id=f'order_{trip.id}_start')
    trip.refresh_from_db()
    return trip


def balance(driver):
    return Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER).balance_minor


def switch(rider, trip, method):
    return client_for(rider).post(SWITCH, {'trip_id': trip.id, 'new_method': method},
                                  format='json')


def pay_online(trip, gateway):
    """The gateway reports the order paid and our poll settles it (the webhook path does the same)."""
    from servers.payments.views import _settle_payment_if_paid_at_gateway

    gateway.paid = True
    assert _settle_payment_if_paid_at_gateway(trip.id, gateway) is True


def current(trip):
    return TripSettlement.objects.filter(trip=trip).order_by('-version').first()


@pytest.fixture
def people(world):
    return make_rider('S', '91'), hyd_driver(world, 'S')


class TestCashThenOnline:
    def test_the_driver_ends_at_the_net_exactly_like_an_online_trip(self, world, people, gateway):
        rider, driver = people
        trip = complete(world, rider, driver, 'cash')
        assert balance(driver) == -COMMISSION
        assert switch(rider, trip, 'online').status_code == 200
        pay_online(trip, gateway)
        assert balance(driver) == NET

        # The same numbers as a trip paid online from the start.
        other_driver = hyd_driver(world, 'O')
        online = complete(world, make_rider('O', '91'), other_driver, 'online')
        pay_online(online, gateway)
        assert balance(other_driver) == NET
        a, b = current(trip), current(online)
        assert (a.gross_fare_minor, a.commission_amount_minor, a.driver_net_minor,
                a.payment_method) == (b.gross_fare_minor, b.commission_amount_minor,
                                      b.driver_net_minor, b.payment_method) == (
            FARE, COMMISSION, NET, 'online')

    def test_the_settlement_is_corrected_append_only(self, world, people, gateway):
        rider, driver = people
        trip = complete(world, rider, driver, 'cash')
        v1 = current(trip)
        switch(rider, trip, 'online')
        pay_online(trip, gateway)
        rows = list(TripSettlement.objects.filter(trip=trip).order_by('version'))
        assert [r.version for r in rows] == [1, 2]
        assert (rows[0].pk, rows[0].payment_method) == (v1.pk, 'cash')
        assert rows[1].settled_at == v1.settled_at
        credit = WalletTransaction.objects.get(purpose='fare_collected_by_platform')
        assert rows[1].wallet_transaction_id == credit.id and credit.amount_minor == FARE

    def test_duplicate_paid_signals_credit_once(self, world, people, gateway):
        from servers.driver.utils import settle_paid_by_platform

        rider, driver = people
        trip = complete(world, rider, driver, 'cash')
        switch(rider, trip, 'online')
        pay_online(trip, gateway)
        pay_online(trip, gateway)
        trip.refresh_from_db()
        settle_paid_by_platform(trip, FARE)              # a webhook arriving after the poll
        assert balance(driver) == NET
        assert WalletTransaction.objects.filter(purpose='fare_collected_by_platform').count() == 1
        assert TripSettlement.objects.filter(trip=trip).count() == 2

    def test_the_earnings_row_shows_the_net_and_no_cash_in_hand(self, world, people, gateway):
        from servers.driver.earnings import settlement_rows

        rider, driver = people
        trip = complete(world, rider, driver, 'cash')
        switch(rider, trip, 'online')
        pay_online(trip, gateway)
        (row,) = settlement_rows(driver)
        assert (row.gross, row.commission, row.net) == (
            Decimal('200.00'), Decimal('30.00'), Decimal('170.00'))
        assert row.driver_holds_cash is False and row.method == 'online'

    def test_the_driver_cannot_confirm_cash_after_the_online_payment(self, world, people, gateway):
        from servers.consumers import TripStatusConsumer

        rider, driver = people
        trip = complete(world, rider, driver, 'cash')
        switch(rider, trip, 'online')
        pay_online(trip, gateway)
        consumer = TripStatusConsumer()
        consumer.trip_id = trip.id
        assert consumer._confirm_cash_payment.__wrapped__(consumer)['success'] is False
        assert balance(driver) == NET


class TestOtherDirectionsAndRoundTrips:
    def test_online_then_cash_settles_once_as_cash(self, world, people, gateway):
        from servers.consumers import TripStatusConsumer

        rider, driver = people
        trip = complete(world, rider, driver, 'online')
        assert not TripSettlement.objects.filter(trip=trip).exists()   # online: settles on payment
        assert switch(rider, trip, 'cash').status_code == 200
        consumer = TripStatusConsumer()
        consumer.trip_id = trip.id
        assert consumer._confirm_cash_payment.__wrapped__(consumer)['success'] is True
        consumer._confirm_cash_payment.__wrapped__(consumer)              # a retried frame
        assert balance(driver) == -COMMISSION
        assert [s.payment_method for s in TripSettlement.objects.filter(trip=trip)] == ['cash']

    def test_cash_online_cash_online_still_credits_once(self, world, people, gateway):
        rider, driver = people
        trip = complete(world, rider, driver, 'cash')
        for method in ('online', 'cash', 'online'):
            assert switch(rider, trip, method).status_code == 200, method
        pay_online(trip, gateway)
        assert balance(driver) == NET
        assert WalletTransaction.objects.filter(purpose='fare_collected_by_platform').count() == 1

    def test_a_paid_trip_cannot_be_switched_back_to_cash(self, world, people, gateway):
        rider, driver = people
        trip = complete(world, rider, driver, 'cash')
        switch(rider, trip, 'online')
        pay_online(trip, gateway)
        response = switch(rider, trip, 'cash')
        assert response.status_code == 409
        assert balance(driver) == NET

    def test_a_wallet_payment_after_cash_takes_the_same_path(self, world, people):
        from servers.rider.models import get_wallet

        rider, driver = people
        trip = complete(world, rider, driver, 'cash')
        wallet = get_wallet(rider)
        wallet.balance_minor, wallet.currency = 50_000, 'INR'
        wallet.save()
        assert client_for(rider).post('/api/v1/rider/wallet/payment/', {'trip_id': trip.id},
                                      format='json').status_code == 201
        assert balance(driver) == NET
        assert current(trip).payment_method == 'wallet' and current(trip).version == 2

    def test_an_online_trip_paid_from_the_start_is_unchanged(self, world, people, gateway):
        rider, driver = people
        trip = complete(world, rider, driver, 'online')
        pay_online(trip, gateway)
        assert balance(driver) == NET
        assert list(TripSettlement.objects.filter(trip=trip).values_list('version', flat=True)) == [1]
        assert not WalletTransaction.objects.filter(purpose='fare_collected_by_platform').exists()
        assert Payment.objects.get(trip_id=trip, status='completed').method == 'online'
