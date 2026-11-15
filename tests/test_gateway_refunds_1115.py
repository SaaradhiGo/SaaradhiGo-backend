"""An approved card refund is sent to Stripe once, and shows on the rider's receipt (US round 5)."""
from types import SimpleNamespace

import pytest

from finance_world import make_payment, make_trip
from servers.admin_audit.models import AdminAuditLog
from servers.ride import gateway_refunds
from servers.ride.models import TripMoneyAction
from servers.ride.serializers import TripDetailSerializer
from test_driver_fee_plans import india  # noqa: F401
from test_pricing_module import BASE, geo, make_rule, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]


class FakeStripe:
    def __init__(self):
        self.calls = []

    def __bool__(self):
        return True

    def create_refund(self, ref, amount_minor=None, currency='USD', refund_reference=None):
        self.calls.append((ref, amount_minor, refund_reference))
        return {'gateway': 'stripe', 'refund_id': refund_reference, 'stripe_refund_id': 're_test_1',
                'status': 'SUCCEEDED'}


@pytest.fixture
def card_refund(india, monkeypatch):
    trip = make_trip(india.hyd, india.rider, india.driver, fare='150', method='online')
    pay = make_payment(trip, 'pi_test_218')
    pay.payment_gateway = 'stripe'
    pay.save(update_fields=['payment_gateway'])
    row = TripMoneyAction.objects.create(trip=trip, kind='refund', amount_minor=2958, currency=trip.currency,
                                         method='gateway', status='pending_gateway', reason='QA test refund')
    fake = FakeStripe()
    monkeypatch.setattr('servers.payments.payment_gateways.factory.get_payment_gateway', lambda name=None, country=None: fake)
    return SimpleNamespace(trip=trip, pay=pay, row=row, fake=fake, rider=india.rider)


def test_sent_once_with_the_money_action_as_idempotency_key(card_refund):
    assert gateway_refunds.send_pending() == {card_refund.row.id: 'sent'}
    card_refund.row.refresh_from_db()
    assert (card_refund.row.status, card_refund.row.gateway_refund_id) == ('completed', 're_test_1')
    assert card_refund.fake.calls == [('pi_test_218', 2958, f'trip_refund_{card_refund.row.id}')]
    assert gateway_refunds.send(card_refund.row.id) == 'skipped'      # never twice
    assert len(card_refund.fake.calls) == 1
    assert AdminAuditLog.objects.filter(action='trip_refund_sent', target_id=str(card_refund.trip.id)).exists()


def test_an_untested_provider_stays_pending_for_finance(card_refund):
    card_refund.pay.payment_gateway = 'razorpay'
    card_refund.pay.save(update_fields=['payment_gateway'])
    assert gateway_refunds.send(card_refund.row.id) == 'not_supported'
    card_refund.row.refresh_from_db()
    assert card_refund.row.status == 'pending_gateway' and card_refund.fake.calls == []


def test_the_riders_receipt_lists_the_refund(card_refund):
    request = SimpleNamespace(user=card_refund.rider)
    data = TripDetailSerializer(card_refund.trip, context={'request': request}).data
    assert [(r['amount'], r['to'], r['status']) for r in data['refunds']] == [('29.58', 'card', 'processing')]
    gateway_refunds.send(card_refund.row.id)
    data = TripDetailSerializer(card_refund.trip, context={'request': request}).data
    assert data['refunds'][0]['status'] == 'sent'
    stranger = SimpleNamespace(user=SimpleNamespace(id=-1, is_staff=False))
    assert TripDetailSerializer(card_refund.trip, context={'request': stranger}).data['refunds'] == []
