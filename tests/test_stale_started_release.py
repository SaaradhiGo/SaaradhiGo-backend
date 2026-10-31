"""Releasing a driver from a stale STARTED trip (case C), per the owner's ruling.

THE RULING (2026-10-31)
-----------------------
Ops ends a started trip: the rider pays the fare for the distance actually driven,
capped at the upfront price; the driver earns normally on that, with commission as
usual. Exception: when the reason is "safety incident" or "platform/app fault" the
rider pays 0 and the platform pays the driver their earnings for the distance driven
(a platform-funded ledger entry). Reason codes are required and decide the rule.

WHAT THESE PROVE
----------------
  * each reason code applies the right rule; a missing or unknown code is a 400;
  * STANDARD: the fare is the trip's quoted fare card on the distance driven (GPS trail,
    jumps dropped) and the time to the driver's LAST ACTIVITY (the silent period is not
    charged), capped at the upfront price; commission and the driver's ledger are the
    normal ones; an online payment is created for the capped amount; cash is recorded as a
    normal cash completion records it; a prepaid amount above the fare is returned;
  * PLATFORM-FUNDED: the rider is charged 0 (nothing to capture, a prepaid amount
    refunded), the driver is credited fare minus the would-be commission as
    `platform_funded_trip_end`, no commission revenue is recorded, Finance gets an alert,
    the Finance overview lists the trip, the rider's receipt says "Not charged";
  * no trail means distance 0; a recorded `actual_distance_km` is the fallback;
  * gates: 403, 404 out of scope, a not-stale trip is refused, a second release is 409;
  * the preview returns exactly the numbers the release then applies;
  * the old `started_trip_needs_policy` refusal is gone.
"""

import threading
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import connection, connections
from django.utils import timezone

from conftest import seed_actor
from servers.admin_audit.models import AdminAuditLog
from servers.command_centre.models import (
    FINANCE_ALERT_KINDS, FINANCE_PLATFORM_FUNDED_END, OperatorAlert,
)
from servers.driver.models import VehicleType
from servers.notifications.models import NotificationMessage
from servers.payments.models import Payment, TripSettlement
from servers.pricing.models import RateCard
from servers.redis_client import (
    clear_driver_active_trip, get_driver_active_trip, set_driver_active_trip,
)
from servers.ride import operator_end
from servers.ride.models import FarePricing, Trip, TripLocationPoint, TripMoneyAction
from servers.rider.models import Wallet, WalletTransaction
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    _client_for, _driver, _rider, _zone, geo, operator,
)
from test_stale_trip_actions import world  # noqa: F401 - fixture
from test_trips_module import _trip

pytestmark = pytest.mark.django_db

LIST = '/api/v1/ride/admin/trips/'
REASON = 'Driver phone off since the trip began, rider safe'
COMMISSION = Decimal('20.00')

# base 30 + 8/km + 1/min, minimum 20: 2 km and 3 min is 49.00; 20 km is capped at the quote.
CARD = dict(base_fare=Decimal('30.00'), per_km_fare=Decimal('8.00'),
            per_min_fare=Decimal('1.00'), min_fare=Decimal('20.00'))


def url(trip, action='release-driver'):
    return f'{LIST}{trip.id}/stale/{action}/'


def release(client, trip, code='other', **extra):
    body = {'reason': REASON}
    if code is not None:
        body['reason_code'] = code
    body.update(extra)
    return client.post(url(trip), body, format='json')


def preview(client, trip, code='other'):
    query = f'?reason_code={code}' if code is not None else ''
    return client.get(url(trip, 'release-preview') + query)


@pytest.fixture
def card(world):  # noqa: F811
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')
    RateCard.objects.create(zone=world['hyd'], vehicle_type=sedan, version=1, is_active=True,
                            effective_from=timezone.now() - timedelta(days=30), **CARD)
    return sedan


def started(world, card, *, km=2, method='cash', points=True, **extra):  # noqa: F811
    """A started trip, flagged stale; the driver was last seen 3 minutes after it began.

    The trail covers `km` kilometres in 3 minutes (so well under any speed limit for the
    short trips used here: 2 km in 3 minutes is 40 km/h; 20 km uses a longer window)."""
    extra.setdefault('commission_percent_snapshot', COMMISSION)
    trip = world['ghost'](status='in_progress', method=method, vehicle_type=card, **extra)
    if points:
        trail(trip, km)
    return trip


def trail(trip, km, minutes=None):
    """Points one kilometre apart, one per `minutes`/`km` interval, inside the window."""
    minutes = minutes if minutes is not None else max(3, km)
    start = trip.started_at
    step = Decimal('0.009')  # about a kilometre of latitude
    for i in range(km + 1):
        TripLocationPoint.objects.create(
            trip=trip, driver=trip.driver_id, latitude=Decimal('17.4') + step * i,
            longitude=Decimal('78.4'),
            recorded_at=start + timedelta(seconds=int(minutes * 60 * i / max(km, 1))),
            sequence=i)


def driver_ledger(driver):
    return WalletTransaction.objects.filter(user_id=driver.user_id)


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

class TestReasonCodes:
    @pytest.mark.parametrize('code,rule', [
        ('driver_unresponsive', 'standard'), ('trip_abandoned', 'standard'),
        ('other', 'standard'), ('safety_incident', 'platform_funded'),
        ('platform_fault', 'platform_funded'),
    ])
    def test_each_code_applies_the_right_rule(self, geo, world, card, code, rule):  # noqa: F811
        trip = started(world, card)
        client, user = operator('city_ops_manager', geo)
        response = release(client, trip, code)
        assert response.status_code == 200, response.content
        data = response.json()['data']
        assert data['rule'] == rule and data['reason_code'] == code
        trip.refresh_from_db()
        assert trip.end_rule == rule and trip.end_reason_code == code
        assert trip.ended_by_id == user.id
        assert trip.status_id.status_code == 'completed'

    def test_the_codes_live_in_one_place(self):
        assert set(operator_end.REASON_CODES) == {
            'driver_unresponsive', 'trip_abandoned', 'other', 'safety_incident',
            'platform_fault'}
        assert {c for c, (_, r) in operator_end.REASON_CODES.items()
                if r == operator_end.PLATFORM_FUNDED} == {'safety_incident', 'platform_fault'}

    def test_a_missing_code_is_a_400_and_nothing_changes(self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        response = release(client, trip, None)
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'REASON_CODE_REQUIRED'
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress' and not trip.end_rule
        assert not WalletTransaction.objects.filter(user_id=world['driver'].user_id).exists()

    def test_an_unknown_code_is_a_400(self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        response = release(client, trip, 'because')
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'INVALID_REASON_CODE'
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress'

    def test_the_free_text_reason_is_still_required(self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        response = client.post(url(trip), {'reason_code': 'other', 'reason': 'no'},
                               format='json')
        assert response.status_code == 400

    def test_the_old_policy_refusal_is_gone(self):
        import servers.ride.admin_stale as module

        assert not hasattr(module, 'STARTED_POLICY_MESSAGE')
        assert not hasattr(module, 'STARTED_ROUTES')

    def test_a_pre_pickup_release_does_not_need_a_code(self, geo, world):  # noqa: F811
        trip = world['ghost']()
        client, _ = operator('city_ops_manager', geo)
        assert release(client, trip, None).status_code == 200


# ---------------------------------------------------------------------------
# STANDARD rule
# ---------------------------------------------------------------------------

class TestStandardRule:
    def test_the_fare_is_the_quoted_card_on_the_distance_and_time_driven(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card, km=2)
        client, _ = operator('city_ops_manager', geo)
        response = release(client, trip, 'driver_unresponsive')
        data = response.json()['data']
        # 30 base + 2 km * 8 + 3 min * 1 = 49.00
        assert data['distance_km'] == '2.00' and data['elapsed_minutes'] == '3.00'
        assert data['uncapped_fare_minor'] == 4_900 and data['capped_fare_minor'] == 4_900
        assert data['cap_applied'] is False
        assert data['rider_pays_minor'] == 4_900 and data['funded_by'] == 'rider'
        trip.refresh_from_db()
        assert trip.final_fare_minor == 4_900
        assert trip.completed_at is not None and trip.stale_flagged_at is None
        assert trip.actual_distance_km == Decimal('2.00')

    def test_the_fare_is_capped_at_the_upfront_price(self, geo, world, card):  # noqa: F811
        now = world['now']
        trip = started(world, card, points=False,
                       requested_at=now - timedelta(minutes=95),
                       accepted_at=now - timedelta(minutes=93),
                       reached_at=now - timedelta(minutes=91),
                       started_at=now - timedelta(minutes=90),
                       last_driver_activity_at=now - timedelta(minutes=30))
        trail(trip, 20, minutes=60)   # 20 km over the hour driven: 20 km/h
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        # 30 + 20 km * 8 + 60 min: about 250.00, far above the 53.00 quote
        expected = int((Decimal('90') + 8 * Decimal(data['distance_km'])) * 100)
        assert data['uncapped_fare_minor'] == expected and expected > 25_000
        assert data['upfront_price_minor'] == 5_300 and data['capped_fare_minor'] == 5_300
        assert data['cap_applied'] is True and data['rider_pays_minor'] == 5_300
        assert data['commission_minor'] == 1_060 and data['driver_earnings_minor'] == 4_240
        trip.refresh_from_db()
        assert trip.final_fare_minor == 5_300
        snapshot = FarePricing.objects.get(trip_id=trip, snapshot_type='ops_end')
        assert snapshot.total_fare_minor == expected and snapshot.gross_fare_minor == 5_300

    def test_the_silent_period_is_not_charged(self, geo, world, card):  # noqa: F811
        # Started 33 minutes ago, last seen 30 minutes ago: 3 minutes charged, not 33.
        trip = started(world, card, km=2)
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        assert data['elapsed_minutes'] == '3.00'
        snapshot = FarePricing.objects.get(trip_id=trip, snapshot_type='ops_end')
        assert snapshot.actual_duration_min == Decimal('3.00')
        assert snapshot.time_fare_minor == 300

    def test_a_gps_jump_is_dropped(self, geo, world, card):  # noqa: F811
        trip = started(world, card, points=False)
        start = trip.started_at
        for i, (lat, lng, sec) in enumerate([
                ('17.4000', '78.4000', 0), ('17.4090', '78.4000', 60),   # 1 km in a minute
                ('27.4000', '78.4000', 70),                               # ~1100 km in 10 s
                ('17.4180', '78.4000', 120)]):                            # back near the road
            TripLocationPoint.objects.create(
                trip=trip, driver=trip.driver_id, latitude=Decimal(lat),
                longitude=Decimal(lng), recorded_at=start + timedelta(seconds=sec), sequence=i)
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        # Only the two plausible segments count (1 km + 0.0 for the dropped pair + the
        # segment from the jump to the return is also dropped): one clean kilometre.
        assert Decimal(data['distance_km']) < Decimal('1.20')
        assert Decimal(data['distance_km']) >= Decimal('0.90')

    def test_no_trail_means_zero_distance(self, geo, world, card):  # noqa: F811
        trip = started(world, card, points=False)
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        assert data['distance_km'] == '0.00' and data['distance_source'] == 'none'
        # 30 base + 3 min = 33.00
        assert data['capped_fare_minor'] == 3_300

    def test_a_recorded_actual_distance_is_the_fallback(self, geo, world, card):  # noqa: F811
        trip = started(world, card, points=False, actual_distance_km=Decimal('1.50'))
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        assert data['distance_km'] == '1.50' and data['distance_source'] == 'recorded'
        assert data['capped_fare_minor'] == 4_500  # 30 + 12 + 3

    def test_the_minimum_fare_of_the_card_applies(self, geo, world, card):  # noqa: F811
        RateCard.objects.create(
            zone=world['hyd'], vehicle_type=card, version=2, is_active=True,
            effective_from=timezone.now() - timedelta(days=10),
            **{**CARD, 'min_fare': Decimal('40.00')})
        trip = started(world, card, points=False)
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        assert data['uncapped_fare_minor'] == 4_000  # 33.00 raised to the 40.00 minimum
        assert FarePricing.objects.get(trip_id=trip, snapshot_type='ops_end').min_fare_applied

    def test_the_surge_the_quote_was_booked_with_is_applied_not_recomputed(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card, km=2, surge_multiplier=Decimal('1.20'))
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        assert data['uncapped_fare_minor'] == 5_880  # 49.00 * 1.20
        assert data['capped_fare_minor'] == 5_300     # still never above the quote

    def test_the_fare_snapshot_records_everything(self, geo, world, card):  # noqa: F811
        trip = started(world, card, km=2)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip)
        snapshot = FarePricing.objects.get(trip_id=trip, snapshot_type='ops_end')
        assert snapshot.actual_distance_km == Decimal('2.00')
        assert snapshot.actual_duration_min == Decimal('3.00')
        assert snapshot.total_fare_minor == 4_900       # uncapped
        assert snapshot.gross_fare_minor == 4_900       # capped
        assert snapshot.rider_payable_minor == 4_900    # what the rider pays
        assert snapshot.rate_card_version == 1 and snapshot.finalized_at is not None
        assert snapshot.fare_basis == 'metered'
        assert snapshot.currency == 'INR'

    def test_commission_and_the_driver_ledger_are_the_normal_ones_for_cash(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card, method='cash')
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        assert data['commission_minor'] == 980 and data['driver_earnings_minor'] == 3_920
        settlement = TripSettlement.objects.get(trip=trip)
        assert (settlement.gross_fare_minor, settlement.commission_amount_minor,
                settlement.driver_net_minor) == (4_900, 980, 3_920)
        assert settlement.commission_percent == COMMISSION
        # Cash, exactly as a normal cash completion records it: the driver holds the fare, so
        # the ledger carries the commission as a debit, the Payment stays pending until the
        # driver confirms the cash, and no platform-funded entry exists.
        txn = driver_ledger(world['driver']).get()
        assert (txn.purpose, txn.txn_type, txn.amount_minor) == ('trip_commission', 'debit', 980)
        payment = Payment.objects.get(trip_id=trip)
        assert (payment.method, payment.status, payment.amount_minor) == ('cash', 'pending', 4_900)
        assert not driver_ledger(world['driver']).filter(
            purpose='platform_funded_trip_end').exists()

    def test_an_online_trip_creates_the_payment_for_the_capped_amount(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card, method='online')
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        payment = Payment.objects.get(trip_id=trip)
        assert (payment.method, payment.status) == ('online', 'pending')
        assert payment.amount_minor == data['capped_fare_minor'] == 4_900
        # Settled the way every online trip is: when the payment lands.
        assert not TripSettlement.objects.filter(trip=trip).exists()
        from servers.driver.utils import credit_driver_wallet

        trip.refresh_from_db()
        credit_driver_wallet(trip)
        settlement = TripSettlement.objects.get(trip=trip)
        assert (settlement.gross_fare_minor, settlement.commission_amount_minor,
                settlement.driver_net_minor) == (4_900, 980, 3_920)
        txn = driver_ledger(world['driver']).get()
        assert (txn.purpose, txn.txn_type, txn.amount_minor) == ('trip_earnings', 'credit', 3_920)

    def test_a_prepaid_online_amount_above_the_fare_is_returned(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card, method='online')
        Payment.objects.create(trip_id=trip, user_id=world['rider'], amount_minor=5_300,
                               method='online', status='completed', gateway_payment_id='pay_1')
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip).json()['data']
        assert data['capped_fare_minor'] == 4_900
        refund = TripMoneyAction.objects.get(trip=trip, kind='refund')
        assert refund.amount_minor == 400 and refund.status == 'pending_gateway'
        # Paid up: the driver is settled now, like a normal online trip whose payment landed.
        assert TripSettlement.objects.filter(trip=trip).exists()

    def test_a_trip_without_a_driver_marker_is_released_and_the_driver_is_free(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        set_driver_active_trip(world['driver'].id, trip.id)
        client, _ = operator('city_ops_manager', geo)
        assert release(client, trip).status_code == 200
        assert get_driver_active_trip(world['driver'].id) in (None, '', b'')
        from servers.ride.models import driver_active_trip_ids

        assert driver_active_trip_ids(world['driver']) == []


# ---------------------------------------------------------------------------
# PLATFORM-FUNDED rule
# ---------------------------------------------------------------------------

class TestPlatformFundedRule:
    @pytest.mark.parametrize('code', ['safety_incident', 'platform_fault'])
    def test_the_rider_pays_nothing_and_the_driver_is_credited(
            self, geo, world, card, code):  # noqa: F811
        trip = started(world, card, method='online')
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip, code).json()['data']
        assert data['rider_pays_minor'] == 0 and data['funded_by'] == 'platform'
        assert data['capped_fare_minor'] == 4_900 and data['driver_earnings_minor'] == 3_920
        trip.refresh_from_db()
        assert trip.final_fare_minor == 0 and trip.platform_funded_minor == 3_920
        assert trip.payment_status == 'waived'
        # Nothing to capture: no payment row at all.
        assert not Payment.objects.filter(trip_id=trip).exists()
        txn = driver_ledger(world['driver']).get()
        assert (txn.purpose, txn.txn_type, txn.amount_minor, txn.status) == (
            'platform_funded_trip_end', 'credit', 3_920, 'completed')
        assert txn.reference_id == f'TRIP_{trip.id}'
        wallet = Wallet.objects.get(user_id=world['driver'].user_id, scope='driver')
        assert wallet.balance_minor == 3_920

    def test_no_commission_revenue_is_recorded(self, geo, world, card):  # noqa: F811
        trip = started(world, card, method='cash')
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'safety_incident')
        assert not TripSettlement.objects.filter(trip=trip).exists()
        assert not driver_ledger(world['driver']).filter(purpose='trip_commission').exists()
        from servers.payments.models import TransactionHistory

        assert not TransactionHistory.objects.filter(trip_id=trip).exists()

    def test_a_prepaid_amount_is_refunded_through_the_existing_helper(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card, method='online')
        Payment.objects.create(trip_id=trip, user_id=world['rider'], amount_minor=5_300,
                               method='online', status='completed', gateway_payment_id='pay_1')
        client, _ = operator('city_ops_manager', geo)
        with mock.patch('servers.ride.utils.process_refund_on_cancel',
                        return_value={'refunded': True, 'amount': Decimal('53.00')}) as helper:
            data = release(client, trip, 'platform_fault').json()['data']
        assert helper.call_count == 1
        assert data['prepaid_refunded_minor'] == 5_300
        assert not TripMoneyAction.objects.filter(trip=trip).exists()

    def test_a_refund_the_gateway_did_not_take_is_recorded_as_owed(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card, method='online')
        Payment.objects.create(trip_id=trip, user_id=world['rider'], amount_minor=5_300,
                               method='online', status='completed', gateway_payment_id='pay_1')
        client, _ = operator('city_ops_manager', geo)
        with mock.patch('servers.ride.utils.process_refund_on_cancel',
                        return_value={'refunded': False, 'amount': None}):
            release(client, trip, 'platform_fault')
        owed = TripMoneyAction.objects.get(trip=trip, kind='refund')
        assert owed.amount_minor == 5_300 and owed.status == 'pending_gateway'

    def test_the_rider_cannot_be_asked_to_pay_afterwards(self, geo, world, card):  # noqa: F811
        trip = started(world, card, method='online')
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'safety_incident')
        rider_client = _client_for(world['rider'])
        response = rider_client.post('/api/v1/payments/create-order/', {'trip_id': trip.id},
                                     format='json')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'NOT_CHARGED'

    def test_finance_gets_a_warning_alert_per_trip(self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'safety_incident')
        alert = OperatorAlert.objects.get(kind=FINANCE_PLATFORM_FUNDED_END)
        assert alert.severity == 'warning' and alert.source_id == str(trip.id)
        assert alert.payload['driver_earnings_minor'] == 3_920
        assert alert.payload['reason_code'] == 'safety_incident'
        assert alert.city is not None
        assert FINANCE_PLATFORM_FUNDED_END in FINANCE_ALERT_KINDS
        # No personal detail in the alert.
        assert 'Kiran' not in str(alert.payload) and 'Lakshmi' not in str(alert.payload)

    def test_a_standard_release_raises_no_finance_alert(self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'other')
        assert not OperatorAlert.objects.filter(kind=FINANCE_PLATFORM_FUNDED_END).exists()

    def test_the_finance_overview_lists_the_trip_and_the_week_total(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'platform_fault')
        finance, _ = operator('finance', geo)
        today = timezone.now().date().isoformat()
        response = finance.get(f'/api/v1/payments/admin/overview/?from={today}&to={today}')
        assert response.status_code == 200, response.content
        block = response.json()['data']['platform_funded_trip_ends']
        assert block['count'] == 1
        assert block['totals'] == [{'currency': 'INR', 'count': 1,
                                    'driver_earnings_minor': 3_920}]
        row = block['rows'][0]
        assert row['trip_id'] == trip.id and row['reason_code'] == 'platform_fault'
        assert row['driver_earnings_minor'] == 3_920 and row['city']['code'] == 'IN-TG-HYD'
        assert 'rider' not in str(row).lower() and 'driver_name' not in row

    def test_the_overview_block_is_scoped_to_the_finance_holders_city(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'platform_fault')
        austin_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        today = timezone.now().date().isoformat()
        block = austin_finance.get(
            f'/api/v1/payments/admin/overview/?from={today}&to={today}'
        ).json()['data']['platform_funded_trip_ends']
        assert block['count'] == 0 and block['rows'] == []

    def test_the_driver_credit_shows_as_its_own_ledger_line(self, geo, world, card):  # noqa: F811
        from servers.payments import ledger

        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'safety_incident')
        driver = world['driver']
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=1)
        statement = ledger.build_statement(driver, start, end)
        assert statement['credits_minor'] == 3_920 and statement['closing_minor'] == 3_920
        assert statement['unexplained_minor'] == 0
        entries = ledger.entry_dicts(driver, statement['rows'], statement['opening_minor'], 0, 10)
        assert [(e['kind'], e['amount_minor'], e['trip_id']) for e in entries] == [
            ('platform_funded_trip_end', 3_920, trip.id)]

    def test_the_credit_goes_into_the_weekly_payout_balance(self, geo, world, card):  # noqa: F811
        from servers.driver.models import Driver  # noqa: F401
        from servers.driver.services import calculate_available_balance

        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'safety_incident')
        assert calculate_available_balance(world['driver']) == Decimal('39.20')

    def test_the_receipt_says_not_charged(self, geo, world, card):  # noqa: F811
        from servers.ride.receipts import issue_receipt

        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'safety_incident')
        trip.refresh_from_db()
        receipt = issue_receipt(trip)
        assert receipt.total_fare == Decimal('0.00')
        assert 'Not charged: ended by SaaradhiGo' in receipt.html_body

    def test_the_trip_is_not_a_reconciliation_gap(self, geo, world, card):  # noqa: F811
        from servers.payments.reconciliation import _check_settlements

        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'safety_incident')
        raised = []

        class Run:
            now = timezone.now() + timedelta(days=1)
            checks = {}

            def raise_item(self, kind, key, **kwargs):
                raised.append((kind, key))

        _check_settlements(Run(), 'IN', 'INR', timezone.now() - timedelta(days=1),
                           timezone.now() + timedelta(days=2))
        assert raised == []


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------

class TestNotices:
    def test_rider_and_driver_are_told_through_notify(
            self, geo, world, card, django_capture_on_commit_callbacks):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        with django_capture_on_commit_callbacks(execute=True):
            assert release(client, trip, 'other').status_code == 200
        rider_msgs = NotificationMessage.objects.filter(
            template_key='trips.operator_ended_rider', recipient_user_id=world['rider'].id)
        driver_msgs = NotificationMessage.objects.filter(
            template_key='trips.operator_ended_driver',
            recipient_user_id=world['driver'].user_id_id)
        assert set(rider_msgs.values_list('channel', flat=True)) == {'in_app', 'push'}
        assert set(driver_msgs.values_list('channel', flat=True)) == {'in_app', 'push'}
        assert all('49.00' in m.body for m in rider_msgs if m.body)
        assert all('39.20' in m.body for m in driver_msgs if m.body)

    def test_the_rider_is_told_they_were_not_charged_on_the_platform_rule(
            self, geo, world, card, django_capture_on_commit_callbacks):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        with django_capture_on_commit_callbacks(execute=True):
            release(client, trip, 'safety_incident')
        rider = NotificationMessage.objects.get(
            template_key='trips.operator_ended_rider', recipient_user_id=world['rider'].id,
            channel='in_app')
        assert 'not been charged' in rider.body
        driver = NotificationMessage.objects.get(
            template_key='trips.operator_ended_driver',
            recipient_user_id=world['driver'].user_id_id, channel='in_app')
        assert 'SaaradhiGo pays you' in driver.body and '39.20' in driver.body


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

class TestGates:
    def test_a_role_without_the_capability_is_403(self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('support_agent', geo)
        assert release(client, trip).status_code == 403
        assert preview(client, trip).status_code == 403
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress'

    def test_a_trip_out_of_scope_is_404(self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo, scope_code='US-TX-AUS')
        assert release(client, trip).status_code == 404
        assert preview(client, trip).status_code == 404

    def test_a_trip_that_is_not_stale_is_refused_and_nothing_changes(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card, stale_flagged_at=None, stale_reason='',
                       last_driver_activity_at=world['now'] - timedelta(seconds=10))
        client, _ = operator('city_ops_manager', geo)
        response = release(client, trip)
        assert response.status_code == 409 and response.json()['error']['code'] == 'trip_active'
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress'
        assert not driver_ledger(world['driver']).exists()
        assert preview(client, trip).status_code == 400

    def test_a_second_release_is_409_and_moves_no_money_twice(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        assert release(client, trip, 'safety_incident').status_code == 200
        again = release(client, trip, 'safety_incident')
        assert again.status_code == 409
        assert again.json()['error']['code'] == 'already_ended'
        assert driver_ledger(world['driver']).count() == 1
        assert Wallet.objects.get(user_id=world['driver'].user_id,
                                  scope='driver').balance_minor == 3_920
        assert OperatorAlert.objects.filter(kind=FINANCE_PLATFORM_FUNDED_END).count() == 1
        assert FarePricing.objects.filter(trip_id=trip, snapshot_type='ops_end').count() == 1

    def test_every_release_and_refusal_is_audited_with_the_figures(
            self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, user = operator('city_ops_manager', geo)
        release(client, trip, 'safety_incident')
        release(client, trip, 'safety_incident')
        done = AdminAuditLog.objects.get(action='trip_stale_driver_released',
                                         target_id=str(trip.id))
        assert done.actor_id == user.id and done.reason == REASON
        after = done.after
        assert after['case'] == 'C' and after['reason_code'] == 'safety_incident'
        assert after['rule'] == 'platform_funded' and after['distance_km'] == '2.00'
        assert after['capped_fare_minor'] == 4_900 and after['rider_pays_minor'] == 0
        assert after['driver_earnings_minor'] == 3_920 and after['funded_by'] == 'platform'
        refused = AdminAuditLog.objects.get(action='trip_stale_driver_released_refused',
                                            target_id=str(trip.id))
        assert refused.after['code'] == 'already_ended'
        assert refused.after['reason_code'] == 'safety_incident'

    def test_no_approval_request_is_needed(self, geo, world, card):  # noqa: F811
        from servers.approvals.models import ApprovalRequest

        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        release(client, trip, 'platform_fault')
        assert not ApprovalRequest.objects.filter(target_id=str(trip.id)).exists()

    def test_a_missing_fare_card_refuses_and_changes_nothing(self, geo, world):  # noqa: F811
        trip = world['ghost'](status='in_progress', commission_percent_snapshot=COMMISSION)
        client, _ = operator('city_ops_manager', geo)
        response = release(client, trip)
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'fare_card_unavailable'
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress'

    def test_the_platform_rule_refuses_when_no_commission_rule_governs_the_trip(
            self, geo, world, card):  # noqa: F811
        from servers.pricing.commission_models import CommissionRule

        CommissionRule.objects.all().delete()
        trip = world['ghost'](status='in_progress', vehicle_type=card)
        Trip.objects.filter(pk=trip.pk).update(commission_percent_snapshot=None)
        client, _ = operator('city_ops_manager', geo)
        response = release(client, trip, 'safety_incident')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'commission_not_configured'


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

class TestPreview:
    @pytest.mark.parametrize('code', ['other', 'safety_incident'])
    def test_the_preview_is_exactly_what_the_release_applies(
            self, geo, world, card, code):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        shown = preview(client, trip, code)
        assert shown.status_code == 200, shown.content
        before = shown.json()['data']
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress'      # a preview writes nothing
        assert not driver_ledger(world['driver']).exists()
        applied = release(client, trip, code).json()['data']
        for key in ('rule', 'distance_km', 'elapsed_minutes', 'uncapped_fare_minor',
                    'capped_fare_minor', 'rider_pays_minor', 'commission_minor',
                    'driver_earnings_minor', 'funded_by', 'currency'):
            assert before[key] == applied[key], key
        assert [c['code'] for c in before['reason_codes']][0] == 'driver_unresponsive'

    def test_the_preview_needs_a_known_code(self, geo, world, card):  # noqa: F811
        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        assert preview(client, trip, None).status_code == 400
        assert preview(client, trip, 'nonsense').status_code == 400

    def test_the_preview_carries_no_contact_detail_or_coordinates(
            self, geo, world, card):  # noqa: F811
        import json

        trip = started(world, card)
        client, _ = operator('city_ops_manager', geo)
        text = json.dumps(preview(client, trip, 'other').json())
        for secret in ('+919876511101', '9876511101', '+919876511102', 'Lakshmi', 'Kiran',
                       '17.4', '78.4', 'Banjara'):
            assert secret not in text, secret

    def test_a_trip_that_is_not_a_stale_started_trip_has_no_preview(
            self, geo, world, card):  # noqa: F811
        trip = world['ghost']()
        client, _ = operator('city_ops_manager', geo)
        assert preview(client, trip).status_code == 400


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------

class TestStructural:
    def test_the_preview_route_is_gated_by_the_scoped_capability(self):
        from servers.admin_access.bridge import ScopedPermission
        from servers.ride.admin_stale import admin_stale_release_preview

        gates = [p for p in admin_stale_release_preview.cls.permission_classes
                 if isinstance(p, ScopedPermission)]
        assert gates, 'the preview must be behind ScopedPermission'

    def test_the_new_ledger_purpose_belongs_to_the_driver(self):
        from servers.payments.scope import DRIVER_LEDGER_PURPOSES

        assert 'platform_funded_trip_end' in DRIVER_LEDGER_PURPOSES

    def test_the_alert_kind_is_a_finance_kind_Finance_can_acknowledge(self):
        from servers.command_centre.models import ACK_PERMISSION

        assert FINANCE_PLATFORM_FUNDED_END == 'finance.platform_funded_trip_end'
        assert ACK_PERMISSION[FINANCE_PLATFORM_FUNDED_END] == 'finance.read'


# ---------------------------------------------------------------------------
# Two operators at once (PostgreSQL only: SELECT FOR UPDATE is a no-op on SQLite)
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_releases_end_the_started_trip_once(geo):  # noqa: F811
    if connection.vendor != 'postgresql':
        pytest.skip(f'needs PostgreSQL, got {connection.vendor!r}')
    now = timezone.now()
    hyd = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')
    RateCard.objects.create(zone=hyd, vehicle_type=sedan, version=1, is_active=True,
                            effective_from=now - timedelta(days=30), **CARD)
    rider = _rider('+919876511101', 'Lakshmi Devi')
    driver = _driver('+919876511102', 'Kiran Rao', geo['IN-TG-HYD'])
    trip = _trip(rider, hyd, status='in_progress', driver=driver, fare=5_300, vehicle_type=sedan,
                 requested_at=now - timedelta(minutes=45),
                 accepted_at=now - timedelta(minutes=40),
                 reached_at=now - timedelta(minutes=35), started_at=now - timedelta(minutes=33),
                 last_driver_activity_at=now - timedelta(minutes=30),
                 stale_flagged_at=now - timedelta(minutes=5), stale_reason='driver_silent_long',
                 commission_percent_snapshot=COMMISSION)
    _, first = operator('city_ops_manager', geo)
    _, second = operator('city_ops_manager', geo)
    barrier = threading.Barrier(2)
    statuses = []

    def go(user):
        try:
            client = _client_for(user)
            barrier.wait(timeout=10)
            response = client.post(url(trip), {'reason': REASON, 'reason_code': 'safety_incident'},
                                   format='json')
            statuses.append(response.status_code)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=go, args=(u,)) for u in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(statuses) == [200, 409], statuses
    assert WalletTransaction.objects.filter(
        purpose='platform_funded_trip_end', reference_id=f'TRIP_{trip.id}').count() == 1
    assert OperatorAlert.objects.filter(kind=FINANCE_PLATFORM_FUNDED_END).count() == 1
    assert FarePricing.objects.filter(trip_id=trip, snapshot_type='ops_end').count() == 1
    assert AdminAuditLog.objects.filter(
        action='trip_stale_driver_released', target_id=str(trip.id)).count() == 1
    clear_driver_active_trip(driver.id)
