"""Switching a completed trip to cash has to leave a row the DRIVER can settle.

THE DEFECT THIS CLOSES
----------------------
`switch_payment_method`'s cash branch wrote::

    Payment.objects.create(..., method='wallet', status='processing')

Cash is not settled by choosing it. The driver confirms collection over the
trip WebSocket, and that handler finds the row with::

    trip.payments.filter(method='cash').first()

Labelled `wallet`, the new row was invisible to it. The driver got "No cash
payment found for this trip", the trip could never move to `completed`, and
every switched-to-cash fare was reported as a wallet payment by anything that
groups by method.

The shape written here is now the same one `_create_payment_on_complete`
writes for a trip booked as cash in the first place, so a switched trip and a
natively-cash trip settle through identical code downstream.

Nothing here talks to a payment gateway: the gateway is stubbed, and the
assertions are about rows and status codes.
"""

from decimal import Decimal as D

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.payments.models import Payment

pytestmark = pytest.mark.django_db

SWITCH_URL = '/api/v1/payments/switch/'


def _client(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


class _NoGateway:
    """A gateway that knows nothing, so no network call is possible."""

    def get_order_status(self, order_id):  # pragma: no cover - never reached
        raise AssertionError('the test should not have reached the gateway')


class _PaidGateway:
    """A gateway that reports the order as already PAID."""

    def get_order_status(self, order_id):
        return {'order_status': 'PAID'}


@pytest.fixture
def completed_trip(db):
    """A completed trip owned by `rider`, with a pending ONLINE payment."""
    from servers.driver.models import Driver
    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Rider

    User = get_user_model()

    rider = User.objects.create_user(
        phone_number='+919990002001', role='rider', username='+919990002001')
    Rider.objects.create(user_id=rider)

    other = User.objects.create_user(
        phone_number='+919990002002', role='rider', username='+919990002002')
    Rider.objects.create(user_id=other)

    driver_user = User.objects.create_user(
        phone_number='+918880002001', role='driver', username='+918880002001')
    driver = Driver.objects.create(user_id=driver_user)

    completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver,
        pickup_lat=D('17.4450'), pickup_long=D('78.3800'),
        destination_lat=D('17.4650'), destination_long=D('78.4200'),
        pickup_address='Banjara Hills', destination_address='Hitech City',
        estimated_fare=D('180.00'), final_fare=D('195.00'),
        status_id=completed,
        payment_method='online',
        payment_status='pending',
    )

    # No gateway order id: the pre-switch reconciliation finds nothing to ask
    # about and returns without a network call.
    payment = Payment.objects.create(
        trip_id=trip, user_id=rider, amount=D('195.00'),
        method='online', status='pending',
    )

    return {
        'trip': trip, 'rider': rider, 'other': other,
        'driver': driver, 'payment': payment,
    }


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    monkeypatch.setattr(
        'servers.payments.views.get_payment_gateway_for_payments',
        lambda *a, **k: _NoGateway(),
    )


def _switch_to_cash(data):
    return _client(data['rider']).post(
        SWITCH_URL,
        data={'trip_id': data['trip'].id, 'new_method': 'cash'},
        format='json',
    )


class TestTheRowTheDriverSettles:
    def test_a_cash_payment_row_exists_afterwards(self, completed_trip):
        response = _switch_to_cash(completed_trip)

        assert response.status_code == 200, response.content.decode()
        assert Payment.objects.filter(
            trip_id=completed_trip['trip'], method='cash',
        ).exists()

    def test_the_driver_confirmation_lookup_finds_it(self, completed_trip):
        """The exact queryset the WS cash-confirm handler runs."""
        _switch_to_cash(completed_trip)

        found = completed_trip['trip'].payments.filter(method='cash').first()

        assert found is not None, (
            'the driver would be told "No cash payment found for this trip" '
            'and the fare could never be settled'
        )
        assert found.amount == D('195.00'), 'the final fare, not the estimate'

    def test_it_is_not_labelled_wallet(self, completed_trip):
        # The precise defect: a cash fare reported as a wallet payment.
        _switch_to_cash(completed_trip)

        assert not Payment.objects.filter(
            trip_id=completed_trip['trip'], method='wallet',
        ).exists()

    def test_it_is_pending_because_no_money_has_moved(self, completed_trip):
        _switch_to_cash(completed_trip)

        cash = completed_trip['trip'].payments.get(method='cash')
        assert cash.status == 'pending'
        assert cash.status != 'completed', 'choosing cash is not paying'

    def test_the_response_says_pending_too(self, completed_trip):
        response = _switch_to_cash(completed_trip)

        payload = response.json().get('data', response.json())
        assert payload['status'] == 'pending'
        assert payload['payment_id'] is not None
        assert 'gateway_order_id' not in payload, 'cash has no gateway order'


class TestWhatHappensToTheOldPayment:
    def test_it_is_failed_and_linked_to_the_replacement(self, completed_trip):
        _switch_to_cash(completed_trip)

        old = Payment.objects.get(pk=completed_trip['payment'].pk)
        cash = completed_trip['trip'].payments.get(method='cash')

        assert old.status == 'failed'
        assert old.replaced_by_id == cash.pk

    def test_the_trip_now_says_cash_and_pending(self, completed_trip):
        _switch_to_cash(completed_trip)

        completed_trip['trip'].refresh_from_db()
        assert completed_trip['trip'].payment_method == 'cash'
        assert completed_trip['trip'].payment_status == 'pending'


class TestRefusals:
    def test_another_rider_cannot_switch_this_trip(self, completed_trip):
        response = _client(completed_trip['other']).post(
            SWITCH_URL,
            data={'trip_id': completed_trip['trip'].id, 'new_method': 'cash'},
            format='json',
        )

        assert response.status_code == 403
        assert response.json()['error']['code'] == 'FORBIDDEN'
        assert not Payment.objects.filter(
            trip_id=completed_trip['trip'], method='cash').exists()

    def test_a_trip_still_running_cannot_switch(self, completed_trip):
        from servers.ride.models import TripStatus
        running, _ = TripStatus.objects.get_or_create(status_code='in_progress')
        completed_trip['trip'].status_id = running
        completed_trip['trip'].save(update_fields=['status_id'])

        response = _switch_to_cash(completed_trip)

        assert response.status_code == 400
        assert response.json()['error']['code'] == 'INVALID_STATE'

    def test_an_already_completed_payment_is_409_not_a_second_collection(
            self, completed_trip):
        completed_trip['payment'].status = 'completed'
        completed_trip['payment'].save(update_fields=['status'])

        response = _switch_to_cash(completed_trip)

        assert response.status_code == 409
        assert response.json()['error']['code'] == 'ALREADY_PAID'
        assert not Payment.objects.filter(
            trip_id=completed_trip['trip'], method='cash').exists(), (
            'asking for cash on a paid trip charges the rider twice'
        )

    def test_paid_at_the_gateway_during_the_tap_is_409(
            self, completed_trip, monkeypatch):
        # The rider finished the online checkout moments before tapping cash.
        completed_trip['payment'].gateway_order_id = 'order_abc'
        completed_trip['payment'].cashfree_order_id = 'order_abc'
        completed_trip['payment'].save()
        monkeypatch.setattr(
            'servers.payments.views.get_payment_gateway_for_payments',
            lambda *a, **k: _PaidGateway(),
        )

        response = _switch_to_cash(completed_trip)

        assert response.status_code == 409
        assert response.json()['error']['code'] == 'ALREADY_PAID'
        assert not Payment.objects.filter(
            trip_id=completed_trip['trip'], method='cash').exists()

    def test_an_unknown_method_is_refused(self, completed_trip):
        response = _client(completed_trip['rider']).post(
            SWITCH_URL,
            data={'trip_id': completed_trip['trip'].id, 'new_method': 'gold'},
            format='json',
        )

        assert response.status_code == 400
        assert response.json()['error']['code'] == 'INVALID_METHOD'
