"""`POST /rider/wallet/payment/`: the server sets the amount and a trip is charged once.

Owner ruling (AUDIT 27.71): the server is the only authority on the amount; a client amount is
never charged; one charge per trip, keyed per trip; a second charge is refused. The app used to
send its own amount with a timestamp key, so every tap was a new debit and the trip was never
marked paid (the driver could still collect cash for it).
"""

from decimal import Decimal

import pytest

from promotions_support import (  # noqa: F401
    block_the_network, client_for, geo, hyd_driver, launch, make_rider, promo_payload, world,
)
from finance_world import make_trip
from servers.payments.models import Payment, TripSettlement
from servers.rider.models import Wallet, WalletTransaction, get_wallet

pytestmark = [pytest.mark.django_db]

URL = '/api/v1/rider/wallet/payment/'


def cash_trip(world, rider, driver, fare='200.00'):
    """A cash trip completed through the real completion path: a pending cash payment, settled."""
    from servers.consumers import TripStatusConsumer

    trip = make_trip(world.hyd, rider, driver, fare=fare, method='cash')
    trip.payment_status = 'pending'
    trip.save(update_fields=['payment_status'])
    TripStatusConsumer()._create_payment_on_complete(trip)
    trip.refresh_from_db()
    return trip


def fund(rider, minor=50_000, currency='INR'):
    wallet = get_wallet(rider)
    wallet.balance_minor = minor
    wallet.currency = currency
    wallet.save()
    return wallet


def rider_balance(rider):
    return Wallet.objects.get(user_id=rider, scope=Wallet.SCOPE_RIDER).balance_minor


def driver_balance(driver):
    return Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER).balance_minor


def rider_debits(rider):
    return WalletTransaction.objects.filter(user_id=rider, txn_type='debit')


def pay(rider, **body):
    return client_for(rider).post(URL, body, format='json')


def error_code(response):
    return response.json()['error']['code']


@pytest.fixture
def setup(world):
    rider, driver = make_rider('W', '91'), hyd_driver(world, 'W')
    fund(rider)
    return rider, driver, cash_trip(world, rider, driver)


class TestTheServerSetsTheAmount:
    def test_the_trip_payable_is_charged_without_a_client_amount(self, setup):
        rider, _, trip = setup
        response = pay(rider, trip_id=trip.id)
        assert response.status_code == 201, response.json()
        data = response.json()['data']
        assert (data['amount'], data['currency'], data['duplicate']) == ('200.00', 'INR', False)
        assert rider_balance(rider) == 30_000
        debit = rider_debits(rider).get()
        assert (debit.amount_minor, debit.reference_id, debit.idempotency_key) == (
            20_000, f'TRIP_{trip.id}', f'trip_{trip.id}_payment')

    def test_a_tampered_amount_is_refused_and_nothing_moves(self, setup):
        rider, driver, trip = setup
        before = driver_balance(driver)
        response = pay(rider, trip_id=trip.id, amount='1.00')
        assert response.status_code == 409
        assert error_code(response) == 'AMOUNT_MISMATCH'
        assert response.json()['error']['details']['amount'] == '200.00'
        assert rider_balance(rider) == 50_000 and not rider_debits(rider).exists()
        assert driver_balance(driver) == before
        trip.refresh_from_db()
        assert (trip.payment_method, trip.payment_status) == ('cash', 'pending')

    def test_a_higher_tampered_amount_is_refused_too(self, setup):
        rider, _, trip = setup
        response = pay(rider, trip_id=trip.id, amount='450.00')
        assert error_code(response) == 'AMOUNT_MISMATCH'
        assert rider_balance(rider) == 50_000

    def test_the_matching_amount_is_accepted(self, setup):
        rider, _, trip = setup
        assert pay(rider, trip_id=trip.id, amount='200.00').status_code == 201
        assert rider_balance(rider) == 30_000

    @pytest.mark.parametrize('ref', ['{}', 'TRIP_{}'])
    def test_the_legacy_reference_id_names_the_trip(self, setup, ref):
        rider, _, trip = setup
        assert pay(rider, reference_id=ref.format(trip.id), amount='200.00').status_code == 201
        assert rider_balance(rider) == 30_000

    def test_a_payment_without_a_trip_is_refused(self, setup):
        rider, _, _ = setup
        response = pay(rider, amount='200.00', purpose='Trip payment')
        assert (response.status_code, error_code(response)) == (400, 'TRIP_REQUIRED')
        assert rider_balance(rider) == 50_000


class TestOneChargePerTrip:
    def test_a_duplicate_request_returns_the_first_charge(self, setup):
        rider, driver, trip = setup
        first = pay(rider, trip_id=trip.id)
        driver_after_first = driver_balance(driver)
        second = pay(rider, trip_id=trip.id)
        assert (first.status_code, second.status_code) == (201, 200)
        assert second.json()['data']['duplicate'] is True
        assert second.json()['data']['transaction_id'] == first.json()['data']['transaction_id']
        assert rider_debits(rider).count() == 1 and rider_balance(rider) == 30_000
        assert driver_balance(driver) == driver_after_first

    def test_a_new_client_key_does_not_buy_a_second_charge(self, setup):
        """The app's key was `trip_<id>_<timestamp>`: a fresh one on every tap."""
        rider, _, trip = setup
        pay(rider, trip_id=trip.id, idempotency_key=f'trip_{trip.id}_1')
        pay(rider, trip_id=trip.id, idempotency_key=f'trip_{trip.id}_2')
        assert rider_debits(rider).count() == 1 and rider_balance(rider) == 30_000

    def test_a_trip_the_driver_confirmed_cash_for_is_refused(self, setup):
        rider, _, trip = setup
        trip.payments.filter(method='cash').update(status='completed')
        response = pay(rider, trip_id=trip.id)
        assert (response.status_code, error_code(response)) == (409, 'ALREADY_PAID')
        assert rider_balance(rider) == 50_000

    def test_the_driver_cannot_confirm_cash_after_a_wallet_payment(self, setup):
        from servers.consumers import TripStatusConsumer

        rider, _, trip = setup
        assert pay(rider, trip_id=trip.id).status_code == 201
        consumer = TripStatusConsumer()
        consumer.trip_id = trip.id
        result = consumer._confirm_cash_payment.__wrapped__(consumer)
        assert result['success'] is False
        assert not Payment.objects.filter(trip_id=trip, method='cash', status='completed').exists()

    def test_a_trip_booked_as_wallet_and_charged_at_completion_is_not_charged_again(self, world):
        from servers.consumers import TripStatusConsumer

        rider, driver = make_rider('B', '91'), hyd_driver(world, 'B')
        fund(rider)
        trip = make_trip(world.hyd, rider, driver, fare='200.00', method='wallet')
        trip.payment_status = 'pending'
        trip.save(update_fields=['payment_status'])
        TripStatusConsumer()._create_payment_on_complete(trip)
        assert rider_balance(rider) == 30_000
        response = pay(rider, trip_id=trip.id)
        assert response.status_code == 200 and response.json()['data']['duplicate'] is True
        assert rider_debits(rider).count() == 1 and rider_balance(rider) == 30_000


class TestWhoAndWhen:
    def test_another_riders_trip_reads_as_not_found(self, setup):
        _, _, trip = setup
        stranger = make_rider('S', '91')
        fund(stranger)
        response = pay(stranger, trip_id=trip.id)
        assert (response.status_code, error_code(response)) == (404, 'NOT_FOUND')
        assert rider_balance(stranger) == 50_000

    def test_a_trip_that_is_not_completed_is_refused(self, world):
        rider, driver = make_rider('N', '91'), hyd_driver(world, 'N')
        fund(rider)
        trip = make_trip(world.hyd, rider, driver, fare='200.00', method='cash', completed=False)
        response = pay(rider, trip_id=trip.id)
        assert (response.status_code, error_code(response)) == (409, 'INVALID_STATE')

    def test_an_insufficient_balance_is_refused_and_nothing_moves(self, world):
        rider, driver = make_rider('I', '91'), hyd_driver(world, 'I')
        fund(rider, minor=19_999)
        trip = cash_trip(world, rider, driver)
        response = pay(rider, trip_id=trip.id)
        assert (response.status_code, error_code(response)) == (400, 'INSUFFICIENT_BALANCE')
        assert rider_balance(rider) == 19_999 and not rider_debits(rider).exists()

    def test_a_wallet_in_another_currency_is_refused(self, world):
        rider, driver = make_rider('X', '91'), hyd_driver(world, 'X')
        fund(rider, currency='USD')
        trip = cash_trip(world, rider, driver)
        response = pay(rider, trip_id=trip.id)
        assert (response.status_code, error_code(response)) == (409, 'CURRENCY_MISMATCH')


class TestTheTripAndTheDriverLedger:
    def test_the_trip_is_marked_paid_by_wallet(self, setup):
        rider, _, trip = setup
        pay(rider, trip_id=trip.id)
        trip.refresh_from_db()
        assert (trip.payment_method, trip.payment_status) == ('wallet', 'completed')
        wallet_payment = Payment.objects.get(trip_id=trip, method='wallet')
        assert (wallet_payment.status, wallet_payment.amount_minor) == ('completed', 20_000)
        cash = Payment.objects.get(trip_id=trip, method='cash')
        assert cash.status == 'failed' and cash.replaced_by_id == wallet_payment.id

    def test_a_cash_settled_driver_ends_at_the_trip_net(self, setup):
        """Cash settlement: -3,000 commission. The platform collected 20,000 for the driver."""
        rider, driver, trip = setup
        assert driver_balance(driver) == -3_000
        pay(rider, trip_id=trip.id)
        assert driver_balance(driver) == 17_000
        credit = WalletTransaction.objects.get(purpose='fare_collected_by_platform')
        assert (credit.user_id_id, credit.amount_minor, credit.txn_type, credit.reference_id) == (
            driver.user_id_id, 20_000, 'credit', f'TRIP_{trip.id}')
        settlement = TripSettlement.objects.get(trip=trip)
        assert (settlement.version, settlement.commission_amount_minor,
                settlement.driver_net_minor) == (1, 3_000, 17_000)

    def test_the_driver_statement_explains_the_balance(self, setup):
        from datetime import timedelta

        from django.utils import timezone

        from servers.payments.ledger import ENTRY_LABELS, build_statement

        rider, driver, trip = setup
        pay(rider, trip_id=trip.id)
        now = timezone.now()
        statement = build_statement(driver, now - timedelta(days=1), now + timedelta(minutes=1))
        assert statement['unexplained_minor'] == 0
        assert 'fare_collected_by_platform' in ENTRY_LABELS

    def test_the_earnings_row_no_longer_says_the_driver_holds_cash(self, setup):
        from servers.driver.earnings import settlement_rows

        rider, driver, trip = setup
        pay(rider, trip_id=trip.id)
        (row,) = settlement_rows(driver)
        assert (row.gross, row.commission, row.net) == (
            Decimal('200.00'), Decimal('30.00'), Decimal('170.00'))
        assert row.driver_holds_cash is False

    def test_a_promo_cash_trip_charges_the_payable_and_the_driver_ends_at_net(self, world):
        from servers.ride.promos import redeem_promo

        launch(world.mkt[0], promo_payload())
        rider, driver = make_rider('P', '91'), hyd_driver(world, 'P')
        fund(rider)
        trip = make_trip(world.hyd, rider, driver, fare='200.00', method='cash')
        assert redeem_promo('WELCOME50', rider, trip, Decimal('200.00'), zone=world.hyd).ok
        trip.payment_status = 'pending'
        trip.save(update_fields=['payment_status'])
        from servers.consumers import TripStatusConsumer

        TripStatusConsumer()._create_payment_on_complete(trip)
        response = pay(rider, trip_id=trip.id)
        assert response.json()['data']['amount'] == '150.00'
        assert rider_balance(rider) == 35_000
        assert driver_balance(driver) == 17_000

    def test_an_unsettled_trip_settles_as_a_wallet_trip(self, world):
        from servers.ride.models import Trip

        rider, driver = make_rider('U', '91'), hyd_driver(world, 'U')
        fund(rider)
        trip = make_trip(world.hyd, rider, driver, fare='200.00', method='cash')
        Trip.objects.filter(pk=trip.pk).update(payment_status='pending')
        Payment.objects.create(trip_id=trip, user_id=rider, amount_minor=20_000, method='cash',
                               status='pending')
        assert not TripSettlement.objects.filter(trip=trip).exists()
        assert pay(rider, trip_id=trip.id).status_code == 201
        settlement = TripSettlement.objects.get(trip=trip)
        assert settlement.payment_method == 'wallet'
        assert driver_balance(driver) == 17_000
        assert not WalletTransaction.objects.filter(purpose='fare_collected_by_platform').exists()
