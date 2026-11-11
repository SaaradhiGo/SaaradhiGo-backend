"""The rider's self-refund is capped, and the driver reversal is a ledger line (AUDIT 27.76).

Owner decision on review B MEDIUM-4, treated as HIGH. `POST /payments/refund/` always refunded the
whole payment, ignoring refunds already issued by Support or Finance (`TripMoneyAction`) or by an
operator end, so the rider could get back more than they paid. And it took the driver's earnings back
with a bare balance edit and only a report row, so the driver's statement could not explain it.
"""

from decimal import Decimal as D

import pytest

from test_payment_gateway_contract import AMOUNT_MINOR, _client, paid_trip, rzp  # noqa: F401
from servers.payments.models import Payment
from servers.ride.models import TripMoneyAction, TripStatus
from servers.rider.models import Wallet, WalletTransaction, get_wallet

pytestmark = pytest.mark.django_db
REFUND = '/api/v1/payments/refund/'
NET = 16_575                     # what the driver's balance gained from the trip


@pytest.fixture
def cancelled_paid(rzp, paid_trip):  # noqa: F811
    """A cancelled trip the rider paid online, which had credited the driver its net."""
    order = _client(paid_trip.rider).post('/api/v1/payments/create-order/',
                                          {'trip_id': paid_trip.trip.id},
                                          format='json').json()['data']['order_id']
    pay = rzp.server.add_payment(order, 'captured')
    Payment.objects.filter(trip_id=paid_trip.trip).update(status='completed',
                                                          gateway_payment_id=pay['id'])
    trip = paid_trip.trip
    trip.status_id, _ = TripStatus.objects.get_or_create(status_code='cancelled')
    trip.payment_status = 'completed'
    trip.save()
    user = paid_trip.driver.user_id
    wallet = get_wallet(user, Wallet.SCOPE_DRIVER)
    wallet.balance_minor, wallet.currency = NET, 'INR'
    wallet.save()
    WalletTransaction.objects.create(
        user_id=user, currency='INR', amount_minor=NET, txn_type='credit', status='completed',
        purpose='trip_earnings', reference_id=f'TRIP_{trip.id}',
        idempotency_key=f'TRIP_{trip.id}_EARNING')
    return paid_trip


def _admin_refund(trip, minor):
    TripMoneyAction.objects.create(trip=trip, kind=TripMoneyAction.REFUND, amount_minor=minor,
                                   currency='INR', method=TripMoneyAction.GATEWAY,
                                   status=TripMoneyAction.COMPLETED, reason='Support refund')


def _driver_balance(paid_trip):  # noqa: F811
    return get_wallet(paid_trip.driver.user_id, Wallet.SCOPE_DRIVER).balance_minor


class TestTheCap:
    def test_a_refund_already_issued_is_taken_off(self, rzp, cancelled_paid):  # noqa: F811
        _admin_refund(cancelled_paid.trip, 5_000)
        r = _client(cancelled_paid.rider).post(REFUND, {'trip_id': cancelled_paid.trip.id},
                                               format='json')
        assert r.status_code == 200, r.content
        assert r.json()['data']['amount'] == '145.00'
        (refund,) = rzp.server.refunds.values()
        assert refund['amount'] == AMOUNT_MINOR - 5_000
        recorded = TripMoneyAction.objects.filter(trip=cancelled_paid.trip).order_by('id')
        assert [a.amount_minor for a in recorded] == [5_000, AMOUNT_MINOR - 5_000]
        assert Payment.objects.get(trip_id=cancelled_paid.trip).status == 'refunded'

    def test_nothing_left_is_refused_and_the_gateway_is_not_called(self, rzp,  # noqa: F811
                                                                   cancelled_paid):
        _admin_refund(cancelled_paid.trip, AMOUNT_MINOR)
        r = _client(cancelled_paid.rider).post(REFUND, {'trip_id': cancelled_paid.trip.id},
                                               format='json')
        assert r.status_code == 409 and r.json()['error']['code'] == 'ALREADY_REFUNDED'
        assert rzp.server.refunds == {}
        assert _driver_balance(cancelled_paid) == NET

    def test_a_second_self_refund_is_refused(self, rzp, cancelled_paid):  # noqa: F811
        client = _client(cancelled_paid.rider)
        assert client.post(REFUND, {'trip_id': cancelled_paid.trip.id},
                           format='json').status_code == 200
        r = client.post(REFUND, {'trip_id': cancelled_paid.trip.id}, format='json')
        assert r.status_code in (400, 409)
        assert len(rzp.server.refunds) == 1

    def test_the_total_refunded_never_exceeds_what_was_paid(self, rzp, cancelled_paid):  # noqa: F811
        from servers.ride.admin_trips import paid_minor, refunded_minor

        _admin_refund(cancelled_paid.trip, 7_500)
        _client(cancelled_paid.rider).post(REFUND, {'trip_id': cancelled_paid.trip.id},
                                           format='json')
        cancelled_paid.trip.refresh_from_db()
        assert refunded_minor(cancelled_paid.trip) == AMOUNT_MINOR
        assert paid_minor(cancelled_paid.trip) == 0          # the payment is refunded


class TestTheDriverLedger:
    def test_the_reversal_is_one_ledger_line_and_the_statement_explains_it(
            self, rzp, cancelled_paid):  # noqa: F811
        from datetime import timedelta

        from django.utils import timezone

        from servers.payments.ledger import build_statement

        _client(cancelled_paid.rider).post(REFUND, {'trip_id': cancelled_paid.trip.id},
                                           format='json')
        assert _driver_balance(cancelled_paid) == 0
        line = WalletTransaction.objects.get(user_id=cancelled_paid.driver.user_id,
                                             purpose='trip_refund_reversal')
        assert (line.txn_type, line.amount_minor, line.reference_id) == (
            'debit', NET, f'TRIP_{cancelled_paid.trip.id}')
        now = timezone.now()
        statement = build_statement(cancelled_paid.driver, now - timedelta(days=1),
                                    now + timedelta(minutes=1))
        assert statement['unexplained_minor'] == 0

    def test_a_retried_reversal_takes_nothing_more(self, cancelled_paid):  # noqa: F811
        from servers.driver.utils import reverse_trip_earnings

        assert reverse_trip_earnings(cancelled_paid.trip).amount_minor == NET
        assert reverse_trip_earnings(cancelled_paid.trip) is None
        assert _driver_balance(cancelled_paid) == 0

    def test_a_trip_that_left_the_driver_owing_reverses_nothing(self, cancelled_paid):  # noqa: F811
        from servers.driver.utils import reverse_trip_earnings

        WalletTransaction.objects.create(
            user_id=cancelled_paid.driver.user_id, currency='INR', amount_minor=NET + 1,
            txn_type='debit', status='completed', purpose='trip_commission',
            reference_id=f'TRIP_{cancelled_paid.trip.id}', idempotency_key='commission-x')
        assert reverse_trip_earnings(cancelled_paid.trip) is None


def test_the_cancellation_refund_is_capped_too(rzp, cancelled_paid):  # noqa: F811
    from servers.ride.utils import process_refund_on_cancel

    _admin_refund(cancelled_paid.trip, 4_000)
    result = process_refund_on_cancel(cancelled_paid.trip)
    assert result['refunded'] is True and result['amount'] == D('155.00')
    (refund,) = rzp.server.refunds.values()
    assert refund['amount'] == AMOUNT_MINOR - 4_000
