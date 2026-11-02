"""Checkout opened twice: every order id is kept, either one settles the trip once (AUDIT 27.76).

Owner decision on review B MEDIUM-1. `create_order` reused the trip's Payment row and overwrote its
order id, so a rider who opened checkout twice and paid the FIRST checkout could not be matched: the
webhook said "payment not found", verify said 404, the poll only asked about the newest order, and
the rider was asked to pay again. Now every order issued is kept (`PaymentOrder`); a capture on any of
them settles the trip once; a capture on another order after that follows the late-payment path
(critical Finance alert, automatic full refund of THAT capture).
"""

import pytest

from test_payment_gateway_contract import (  # noqa: F401
    _client, paid_trip, rzp, settleable, world,
)
from servers.command_centre.models import OperatorAlert
from servers.payments.models import Payment, PaymentOrder, TransactionHistory

pytestmark = pytest.mark.django_db
CREATE = '/api/v1/payments/create-order/'
VERIFY = '/api/v1/payments/verify/'


def _open_checkout(paid_trip):  # noqa: F811
    response = _client(paid_trip.rider).post(CREATE, {'trip_id': paid_trip.trip.id},
                                             format='json')
    assert response.status_code == 201, response.content
    return Payment.objects.get(trip_id=paid_trip.trip).gateway_order_id


class Refunds(list):
    """The refunds made, in order; `capture(gateway)` routes the late-payment refund through it."""


@pytest.fixture
def refunds(monkeypatch):
    calls = Refunds()

    def capture(gateway):
        real = gateway.create_refund

        def create_refund(payment_id, amount_minor=None, currency='INR'):
            calls.append((payment_id, amount_minor))
            return real(payment_id, amount_minor=amount_minor, currency=currency)

        gateway.create_refund = create_refund
        monkeypatch.setattr('servers.payments.late_paid._gateway', lambda name: gateway)

    calls.capture = capture
    return calls


class TestThroughTheEndpoints:
    """Razorpay: create-order twice, then verify with what Checkout returned."""

    def _verify(self, rzp, paid_trip, order_id):  # noqa: F811
        pay = rzp.server.add_payment(order_id, 'captured')
        proof = rzp.checkout_proof(order_id, pay['id'])
        response = _client(paid_trip.rider).post(VERIFY, {
            'razorpay_order_id': order_id, 'razorpay_payment_id': pay['id'],
            'razorpay_signature': proof['signature']}, format='json')
        return response, pay['id']

    def test_open_checkout_twice_pay_the_first(self, rzp, paid_trip):  # noqa: F811
        first = _open_checkout(paid_trip)
        second = _open_checkout(paid_trip)
        assert first != second
        assert set(PaymentOrder.objects.values_list('order_id', flat=True)) == {first, second}
        response, captured = self._verify(rzp, paid_trip, first)
        assert response.status_code == 200, response.content
        payment = Payment.objects.get(trip_id=paid_trip.trip)
        assert payment.status == 'completed'
        assert (payment.gateway_order_id, payment.gateway_payment_id) == (first, captured)
        paid_trip.trip.refresh_from_db()
        assert paid_trip.trip.payment_status == 'completed'

    def test_then_paying_the_second_is_refunded_and_never_settled(
            self, rzp, paid_trip, refunds, django_capture_on_commit_callbacks):  # noqa: F811
        from servers.payments.payment_gateways.factory import get_payment_gateway

        refunds.capture(get_payment_gateway('razorpay'))
        first = _open_checkout(paid_trip)
        second = _open_checkout(paid_trip)
        self._verify(rzp, paid_trip, first)
        with django_capture_on_commit_callbacks(execute=True):
            response, late = self._verify(rzp, paid_trip, second)
        assert response.status_code == 409 and response.json()['error']['code'] == 'ALREADY_PAID'
        payment = Payment.objects.get(trip_id=paid_trip.trip)
        assert payment.status == 'completed' and payment.gateway_order_id == first
        assert refunds == [(late, payment.amount_minor)]       # THAT capture, in full
        order = PaymentOrder.objects.get(order_id=second)
        assert (order.late_payment_id, order.late_refund) == (late, 'refunded')
        assert OperatorAlert.objects.get(kind='finance.paid_after_settled').severity == 'critical'


def _deliver(client, world, order_id, payment_id, event_id):  # noqa: F811
    body, headers = world.webhook_request(order_id, payment_id, event_id)
    return client.post(world.webhook_path, data=body, content_type='application/json', **headers)


class TestThroughTheWebhookAndPoll:
    """Both providers: the webhook and the gateway poll match any order the payment was issued."""

    @pytest.fixture(autouse=True)
    def distinct_cashfree_orders(self, monkeypatch):
        """Cashfree order ids are `<ref><unix seconds>`: two checkouts in one second would share an
        id (and then there is only one order to pay). Move its clock between checkouts."""
        import itertools
        import time as real_time
        from types import SimpleNamespace

        ticks = itertools.count(int(real_time.time()))
        monkeypatch.setattr('integrations.payments.cashfree.time',
                            SimpleNamespace(time=lambda: next(ticks), sleep=real_time.sleep))

    def test_a_webhook_for_the_first_order_settles_once(self, world, paid_trip,  # noqa: F811
                                                        api_client):
        first = _open_checkout(paid_trip)
        _open_checkout(paid_trip)
        gateway = world.gateway()
        paid = world.pay(gateway, first)
        assert _deliver(api_client, world, first, paid, 'evt_1').status_code == 200
        assert _deliver(api_client, world, first, paid, 'evt_2').status_code == 200
        payment = Payment.objects.get(trip_id=paid_trip.trip)
        assert payment.status == 'completed' and payment.gateway_order_id == first
        # One payment row for the capture, however often the webhook is delivered.
        assert TransactionHistory.objects.filter(trip_id=paid_trip.trip,
                                                 gateway_payment_id=paid).count() == 1

    def test_a_late_capture_on_the_other_order_is_refunded(
            self, world, paid_trip, refunds, api_client,  # noqa: F811
            django_capture_on_commit_callbacks):
        first = _open_checkout(paid_trip)
        second = _open_checkout(paid_trip)
        assert first != second
        gateway = world.gateway()
        refunds.capture(gateway)
        _deliver(api_client, world, first, world.pay(gateway, first), 'evt_1')
        late = world.pay(gateway, second)
        with django_capture_on_commit_callbacks(execute=True):
            r = _deliver(api_client, world, second, late, 'evt_late')
            assert r.status_code == 200 and r.json()['status'] == 'superseded_refunding', r.content
        payment = Payment.objects.get(trip_id=paid_trip.trip)
        assert payment.status == 'completed' and payment.gateway_order_id == first
        assert len(refunds) == 1
        assert PaymentOrder.objects.get(order_id=second).late_refund == 'refunded'
        # Redelivered: nothing more.
        with django_capture_on_commit_callbacks(execute=True):
            _deliver(api_client, world, second, late, 'evt_late_2')
        assert len(refunds) == 1
        assert OperatorAlert.objects.filter(kind='finance.paid_after_settled').count() == 1

    def test_the_poll_settles_on_an_earlier_paid_order(self, world, paid_trip):  # noqa: F811
        from servers.payments.views import _settle_payment_if_paid_at_gateway

        first = _open_checkout(paid_trip)
        _open_checkout(paid_trip)
        gateway = world.gateway()
        world.pay(gateway, first)
        assert _settle_payment_if_paid_at_gateway(paid_trip.trip.id, gateway) is True
        payment = Payment.objects.get(trip_id=paid_trip.trip)
        assert payment.status == 'completed' and payment.gateway_order_id == first


def test_the_migration_backfills_existing_order_ids():
    from importlib import import_module

    from django.apps import apps

    migration = import_module('servers.payments.migrations.0024_payment_orders')
    assert callable(migration.backfill)
    assert apps.get_model('payments', 'PaymentOrder')._meta.get_field('order_id').unique
