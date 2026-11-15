"""Srini's ruling, 14 Nov: a US receipt (text only, no amount changes).

Titled "Receipt", never "Tax invoice"; no "Taxable value"; the lines Fare, Booking fee (the city's
flat per-trip fee, which read "Minimum fare adjustment"), Tip, Total charged; distance in miles.
India keeps its receipt or tax-invoice rules.
"""
from decimal import Decimal
from unittest import mock

import pytest

from servers.ride import invoice_lines
from tests.test_money_minor_units_parity import _people, _run_trip, market  # noqa: F401

pytestmark = pytest.mark.django_db
D = Decimal


def _us_trip(market):  # noqa: F811
    from servers.pricing.models import ServiceZone
    from servers.ride.models import Receipt

    rider, driver = _people()
    _run_trip(market, rider, driver, ('us-day', 'US', '7.3', '19', '1.00', False, 'card', None))
    receipt = Receipt.objects.select_related('trip_id').latest('id')
    return rider, driver, receipt


def test_a_us_receipt_reads_fare_booking_fee_tip_total_charged(market):  # noqa: F811
    from servers.payments.models import Tip
    from servers.ride.receipts import receipt_invoice
    from servers.ride.serializers import TripDetailSerializer

    rider, driver, receipt = _us_trip(market)
    trip = receipt.trip_id
    zone = trip.zone
    zone.per_trip_fee = D('1.50')
    zone.per_trip_fee_label = 'Booking fee (QA test)'
    zone.save(update_fields=['per_trip_fee', 'per_trip_fee_label'])
    Tip.objects.create(trip=trip, rider=rider, driver=driver, amount_minor=532, currency='USD', status='succeeded')

    inv = receipt_invoice(trip, total=receipt.total_fare, gst_amount=receipt.gst_amount, currency='USD')
    assert inv['title'] == 'Receipt' and inv['taxable'] is None
    assert [x['label'] for x in inv['lines']] == ['Fare']
    assert [(x['label'], x['amount']) for x in inv['extras']] == [('Booking fee', '1.50')]
    assert D(inv['lines'][0]['amount']) == receipt.total_fare - D('1.50') - receipt.gst_amount
    assert inv['tip'] == '5.32' and D(inv['total_charged']) == receipt.total_fare + D('5.32')
    assert invoice_lines.adds_up(inv)
    labels = ' '.join(x['label'] for x in inv['lines'] + inv['extras'])
    assert 'Minimum fare adjustment' not in labels

    shown = TripDetailSerializer(trip, context={'request': mock.Mock(user=rider)}).data['invoice']
    assert shown['title'] == 'Receipt' and shown['breakdown'] == inv


def test_india_keeps_its_receipt(market, settings):  # noqa: F811
    from servers.ride.models import Receipt
    from servers.ride.receipts import receipt_invoice

    settings.LEGAL_ENTITIES_JSON = '{"IN": {"gst_registered": true}}'
    rider, driver = _people()
    _run_trip(market, rider, driver, ('in-day', 'IN', '3.2', '11', '1.00', False, 'cash', None))
    receipt = Receipt.objects.select_related('trip_id').latest('id')
    inv = receipt_invoice(receipt.trip_id, total=receipt.total_fare, gst_amount=receipt.gst_amount, currency='INR')
    assert inv['title'] == 'Tax invoice' and inv['taxable'] is not None and 'market' not in inv
