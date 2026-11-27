"""Trip charges step 2 (27 Nov): the rider's breakdown and receipt. Pass-through lines say whom they are
collected for; waiting is the one rider-caused change to the locked total, and it is the driver's."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from servers.pricing import trip_charges
from servers.ride import invoice_lines
from servers.ride.waiting import apply_waiting
from tests.test_money_minor_units_parity import HYD, _people, _quote, market  # noqa: F401

pytestmark = pytest.mark.django_db
D = Decimal
TERMS = {'item_code': 'wait', 'free_minutes': 3, 'per_minute_minor': 200, 'words': '', 'sample': True}
ORR = {'code': 'trip_extra', 'item_code': 'orr', 'kind': 'tolled_road', 'label': 'Toll (estimated): ORR',
       'amount_minor': 4800, 'who_pays': 'rider', 'collection': 'driver_at_gate', 'remit_to': ''}
BOI = {'code': 'trip_extra', 'item_code': 'boi', 'kind': 'airport', 'label': 'Boise Airport fee',
       'amount_minor': 300, 'who_pays': 'rider', 'collection': 'platform_remits',
       'remit_to': 'Boise Airport (ground transportation permit)'}


class TestWords:
    def test_each_pass_through_says_whom_it_is_collected_for(self):
        assert trip_charges.on_behalf_note(ORR) == ('Collected on behalf of the toll operator. '
                                                    'Your driver pays it at the plaza.')
        assert trip_charges.on_behalf_note(BOI) == ('Collected on behalf of Boise Airport (ground transportation '
                                                    'permit). SaaradhiGo pays it.')
        assert 'at the gate' in trip_charges.on_behalf_note({'kind': 'airport'})
        assert trip_charges.on_behalf_note({'kind': 'waiting'}) == ''

    def test_the_quote_groups_them_under_collected_on_behalf_of_others(self):
        inv = invoice_lines.from_quote({
            'base_fare': '100.00', 'distance_fare': '150.00', 'time_fare': '0', 'total_fare': '298.00',
            'tax_amount': '0', 'tax_mode': 'inclusive', 'tax_percent': '0', 'currency': 'INR',
            'fare_adjustments': [{'code': 'trip_extra', 'label': ORR['label'], 'amount': '48.00', **{
                k: ORR[k] for k in ('kind', 'collection', 'remit_to')}}]})
        assert invoice_lines.adds_up(inv)
        (x,) = inv['extras']
        assert x['on_behalf'] and x['note'].startswith('Collected on behalf of the toll operator')
        assert inv['on_behalf_title'] == 'Collected on behalf of others'


class TestWaiting:
    def test_whole_minutes_past_the_free_ones_at_the_locked_rate(self):
        t0 = timezone.now()
        assert trip_charges.waiting_charge(TERMS, t0, t0 + timedelta(minutes=7, seconds=50), 'INR') == (4, 800)
        assert trip_charges.waiting_charge(TERMS, t0, t0 + timedelta(minutes=2), 'INR') == (0, 0)
        assert trip_charges.waiting_charge(None, t0, t0 + timedelta(minutes=9), 'INR') == (0, 0)
        # ₹2.50/min for 3 min is ₹7.50: India charges whole rupees.
        assert trip_charges.waiting_charge({**TERMS, 'per_minute_minor': 250}, t0, t0 + timedelta(minutes=6),
                                           'INR') == (3, 800)

    def test_it_raises_the_locked_total_once_goes_to_the_driver_and_is_on_the_receipt(self, market, settings):  # noqa: F811
        from servers.driver.utils import credit_driver_wallet
        from servers.payments.models import TripSettlement
        from servers.ride.models import FarePricing, Trip, TripStatus
        from servers.ride.receipts import issue_receipt, receipt_invoice

        rider, driver = _people()
        q = _quote('IN', '12.0', '30', '1.00', False)
        extras = D('48.00')
        total = D(q['total_fare']) + extras
        t0 = timezone.now() - timedelta(minutes=40)
        trip = Trip.objects.create(
            user_id=rider, driver_id=driver, zone=market['IN'], pickup_lat=HYD[0], pickup_long=HYD[1],
            destination_lat=HYD[0], destination_long=HYD[1], estimated_fare=total,
            estimated_distance_km=D('12.0'), estimated_duration_min=D('30'), surge_multiplier=q['surge_multiplier'],
            requested_vehicle_type=market['vt'], payment_method='cash', trip_extras_minor=4800,
            reached_at=t0, started_at=t0 + timedelta(minutes=9))
        FarePricing.objects.create(trip_id=trip, base_fare=q['base_fare'], distance_fare=q['distance_fare'],
                                   time_fare=q['time_fare'], surge_multiplier=q['surge_multiplier'],
                                   total_fare=total, trip_extras=[ORR], trip_extras_minor=4800, waiting_terms=TERMS)
        assert apply_waiting(trip) == 1200                                   # 9 min, 3 free: 6 x ₹2
        assert apply_waiting(trip) == 0                                      # once
        trip.status_id, _ = TripStatus.objects.get_or_create(status_code='completed')
        trip.completed_at = timezone.now()
        trip.save()
        assert trip.final_fare == total + D('12.00') and trip.waiting_minutes == 6

        credit_driver_wallet(trip)
        st = TripSettlement.objects.get(trip=trip)
        assert D(st.gross_fare) == total + D('12.00') - extras               # the toll is not the driver's fare
        receipt = issue_receipt(trip)
        inv = receipt_invoice(trip, total=receipt.total_fare, gst_amount=receipt.gst_amount, currency='INR')
        assert invoice_lines.adds_up(inv)
        assert {'code': 'waiting', 'label': 'Waiting (6 min after 3 free)', 'amount': '12.00'} in inv['lines'] or \
            any(x['code'] == 'waiting' and x['label'] == 'Waiting (6 min after 3 free)' for x in inv['lines'])
        (toll,) = [x for x in inv['extras'] if x['code'] == 'trip_extra']
        assert toll['amount'] == '48.00' and toll['on_behalf']
