"""An old online order paid after the trip was paid another way (independent review HIGH-3, AUDIT 27.75).

The reviewer's sequence: an online order is open; the rider switches to cash and the driver confirms
it (or pays from the wallet); then the old order is paid at the gateway. The webhook used to hit the
one-completed-payment constraint and 500 (Razorpay retried forever; Cashfree deduped the event), so
the rider had paid twice with nothing recorded or refunded. Now the payment is never completed or
settled; Finance gets a critical alert; the gateway payment is refunded in full. Runs on both
payment adapters.
"""

from decimal import Decimal as D

import pytest

from test_payment_gateway_contract import paid_trip, settleable, world  # noqa: F401
from servers.command_centre.models import FINANCE_ALERT_KINDS, OperatorAlert
from servers.payments.models import Payment, TransactionHistory

pytestmark = pytest.mark.django_db


@pytest.fixture
def refunds(world, monkeypatch):  # noqa: F811
    """The refund goes through the world's mocked provider; record each call."""
    gateway = world.gateway()
    calls = []
    real = gateway.create_refund

    def create_refund(payment_id, amount_minor=None, currency='INR'):
        calls.append((payment_id, amount_minor, currency))
        return real(payment_id, amount_minor=amount_minor, currency=currency)

    gateway.create_refund = create_refund
    monkeypatch.setattr('servers.payments.late_paid._gateway', lambda name: gateway)
    return calls


def _settled_by_cash(settleable):  # noqa: F811
    """The rider switched to cash and the driver confirmed it: the online row is superseded."""
    online = settleable.payment
    cash = Payment.objects.create(trip_id=settleable.trip, user_id=online.user_id,
                                  amount=online.amount, method='cash', status='completed')
    online.status, online.replaced_by = 'failed', cash
    online.save(update_fields=['status', 'replaced_by'])
    trip = settleable.trip
    trip.payment_method, trip.payment_status = 'cash', 'completed'
    trip.save(update_fields=['payment_method', 'payment_status'])
    return cash


def _deliver(client, world, settleable, event_id='evt_late'):  # noqa: F811
    body, headers = world.webhook_request(settleable.order_id, settleable.payment_id, event_id)
    return client.post(world.webhook_path, data=body, content_type='application/json', **headers)


class TestTheLateOrder:
    def test_it_is_refunded_never_completed_or_settled(self, world, settleable, refunds,  # noqa: F811
                                                        api_client,
                                                        django_capture_on_commit_callbacks):
        cash = _settled_by_cash(settleable)
        with django_capture_on_commit_callbacks(execute=True):
            response = _deliver(api_client, world, settleable)
        assert response.status_code == 200, response.content
        online = Payment.objects.get(pk=settleable.payment.pk)
        assert online.status == 'refunded'
        assert online.gateway_metadata['late_paid']['refund'] == 'refunded'
        assert refunds == [(settleable.payment_id, online.amount_minor, 'INR')]
        assert list(Payment.objects.filter(trip_id=settleable.trip, status='completed')) == [cash]
        assert not TransactionHistory.objects.filter(trip_id=settleable.trip,
                                                     method='online').exists()

    def test_finance_gets_one_critical_alert_saying_it_was_refunded(
            self, world, settleable, refunds, api_client,  # noqa: F811
            django_capture_on_commit_callbacks):
        _settled_by_cash(settleable)
        with django_capture_on_commit_callbacks(execute=True):
            _deliver(api_client, world, settleable)
        alert = OperatorAlert.objects.get(kind='finance.paid_after_settled')
        assert alert.kind in FINANCE_ALERT_KINDS and alert.severity == 'critical'
        assert alert.payload['refund'] == 'refunded' and 'refunded in full' in alert.title
        assert alert.payload['payment_id'] == settleable.payment.pk

    def test_a_redelivered_webhook_refunds_once(self, world, settleable, refunds,  # noqa: F811
                                                api_client, django_capture_on_commit_callbacks):
        _settled_by_cash(settleable)
        for i in range(3):
            with django_capture_on_commit_callbacks(execute=True):
                assert _deliver(api_client, world, settleable, f'evt_{i}').status_code == 200
        assert len(refunds) == 1
        assert OperatorAlert.objects.filter(kind='finance.paid_after_settled').count() == 1

    def test_after_a_wallet_payment_too(self, world, settleable, refunds, api_client,  # noqa: F811
                                        django_capture_on_commit_callbacks):
        online = settleable.payment
        wallet = Payment.objects.create(trip_id=settleable.trip, user_id=online.user_id,
                                        amount=online.amount, method='wallet', status='completed')
        online.status, online.replaced_by = 'failed', wallet
        online.save(update_fields=['status', 'replaced_by'])
        with django_capture_on_commit_callbacks(execute=True):
            assert _deliver(api_client, world, settleable).status_code == 200
        assert Payment.objects.get(pk=online.pk).status == 'refunded'

    def test_the_normal_order_still_settles(self, world, settleable, refunds, api_client):  # noqa: F811
        assert _deliver(api_client, world, settleable).status_code == 200
        assert Payment.objects.get(pk=settleable.payment.pk).status == 'completed'
        assert refunds == []
        assert not OperatorAlert.objects.filter(kind='finance.paid_after_settled').exists()


class TestWhenTheRefundCannotBeMade:
    def test_no_gateway_payment_id_leaves_a_manual_refund_alert(self, world, settleable,  # noqa: F811
                                                                refunds,
                                                                django_capture_on_commit_callbacks):
        from django.db import transaction

        from servers.payments import late_paid

        _settled_by_cash(settleable)
        Payment.objects.filter(pk=settleable.payment.pk).update(gateway_payment_id=None,
                                                                cashfree_payment_id=None)
        with django_capture_on_commit_callbacks(execute=True):
            with transaction.atomic():
                p = Payment.objects.select_for_update(of=('self',)).select_related(
                    'trip_id').get(pk=settleable.payment.pk)
                assert late_paid.superseded(p)
                late_paid.record(p, gateway_name=world.name, source='poll')
        p = Payment.objects.get(pk=settleable.payment.pk)
        assert p.status == 'failed' and p.gateway_metadata['late_paid']['refund'] == (
            'manual_refund_needed')
        assert refunds == []
        assert 'REFUND IT MANUALLY' in OperatorAlert.objects.get(
            kind='finance.paid_after_settled').title


def test_settle_paid_by_platform_defers_without_a_commission_rule(paid_trip,  # noqa: F811
                                                                  monkeypatch):
    """Review MEDIUM-3: no commission rule must not undo the rider's payment."""
    from servers.driver.utils import settle_paid_by_platform
    from servers.payments.models import TripSettlement
    from servers.pricing.commission import CommissionNotConfigured

    def no_rule(trip):
        raise CommissionNotConfigured('no rule governs this trip')

    monkeypatch.setattr('servers.pricing.services.commission_percent_for_trip', no_rule)
    trip = paid_trip.trip
    assert settle_paid_by_platform(trip, 19_500) is None          # no exception
    assert OperatorAlert.objects.filter(kind='settlement.no_commission_rule').exists()
    assert not TripSettlement.objects.filter(trip=trip).exists()
    assert D('195.00') == trip.final_fare
