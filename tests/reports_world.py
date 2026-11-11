"""One shared world for the Reports tests (a helper, not a test file).

A fixture month (October 2026) in two markets that must never be added together:

  * Hyderabad (IN, INR, Asia/Kolkata) and Austin (US, USD, America/Chicago), each with a driver,
    riders, completed trips on both sides of midnight LOCAL time, cash and online payments, two
    vehicle classes, a promo discount, a platform-funded trip end, a refund, incentive and
    referral rewards, a reward recovery and (Austin) the regulatory-fee expense.
  * Cancellations by the rider, the driver, an admin and the system (with and without a driver).
  * Sessions, tickets, an SOS with an acknowledgement, a safety incident and fraud flags.

Fixture passwords: nobody signs in here; tokens are minted directly.
"""

from datetime import datetime, timedelta, timezone as dt_utc
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model

from finance_world import make_driver, make_rider, settle, status_obj, zone
from servers.driver.models import DriverSession, VehicleType
from servers.fraud.models import RiskFlag
from servers.payments.models import PlatformExpense, TripSettlement
from servers.promotions.models import Campaign, LedgerEntry
from servers.ride.models import Trip, TripMoneyAction
from servers.rider.models import WalletTransaction
from servers.safety.models import SafetyIncident
from servers.sos.models import SOSEvent, SOSEventUpdate
from servers.support.models import SupportTicket

User = get_user_model()

IST = ZoneInfo('Asia/Kolkata')
CDT = ZoneInfo('America/Chicago')
UTC = dt_utc.utc
_seq = iter(range(1, 99999))


def at(tz, month, day, hour=12, minute=0):
    """A UTC instant for a local wall-clock time in October 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=tz).astimezone(UTC)


def make_trip(zone_obj, rider, driver, *, fare, method='online', requested, completed=None,
              status='completed', cancelled_by='', accepted=True, reached=True,
              vehicle='sedan', distance='5.00', minutes='12.00', **extra):
    vt, _ = VehicleType.objects.get_or_create(type=vehicle)
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver if accepted else None,
        vehicle_id=driver.active_vehicle if accepted else None, requested_vehicle_type=vt,
        zone=zone_obj, status_id=status_obj(status),
        pickup_lat=Decimal('17.43'), pickup_long=Decimal('78.40'),
        destination_lat=Decimal('17.24'), destination_long=Decimal('78.42'),
        estimated_fare=Decimal(fare), payment_method=method, payment_status='completed',
        cancelled_by=cancelled_by, actual_distance_km=Decimal(distance) if completed else None,
        actual_duration_min=Decimal(minutes) if completed else None, **extra)
    stamps = {'requested_at': requested}
    if accepted:
        stamps['accepted_at'] = requested + timedelta(seconds=30)
        if reached:
            stamps['reached_at'] = requested + timedelta(seconds=30 + 240)
    if completed:
        stamps['completed_at'] = completed
    Trip.objects.filter(pk=trip.pk).update(**stamps)
    trip.refresh_from_db()
    return trip


def settle_at(trip, when):
    settle(trip)
    TripSettlement.objects.filter(trip=trip).update(settled_at=when)


def wallet_row(user, purpose, amount, currency, when, txn_type='credit', reference=''):
    row = WalletTransaction.objects.create(
        user_id=user, amount_minor=amount, currency=currency, txn_type=txn_type,
        status='completed', purpose=purpose, reference_id=reference,
        idempotency_key=f'rpt-{purpose}-{next(_seq)}')
    WalletTransaction.objects.filter(pk=row.pk).update(created_at=when)
    return row


def build_month(geo):
    """The fixture month. Returns a namespace of everything the tests need to point at."""
    hyd = zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus = zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
               timezone_name='America/Chicago')
    in_driver = make_driver(geo, 'IN-TG-HYD', 'IN', currency='INR', calling='91')
    us_driver = make_driver(geo, 'US-TX-AUS', 'US', currency='USD', calling='1')
    in_riders = [make_rider(f'IN{i}', '91') for i in range(3)]
    us_riders = [make_rider(f'US{i}', '1') for i in range(2)]
    campaign = Campaign.objects.create(
        kind='rider_promo', name='Fixture promo', state='live', country='IN',
        geo_node=geo['IN-TG-HYD'], currency='INR', budget_minor=10_000_000)
    trips = SimpleNamespace(hyd=[], aus=[])

    # --- Hyderabad: completed trips, cash and online, two vehicle classes -------------------
    plan = [
        # (day, hour, minute, fare, method, vehicle)
        (2, 10, 0, '200.00', 'online', 'sedan'),
        (2, 11, 0, '300.00', 'cash', 'auto'),
        # Late evening IST: 19:00 UTC on Oct 4 is 00:30 IST on Oct 5 (the local day is the 5th).
        (4, 19, 0, '450.00', 'online', 'sedan'),
        (9, 9, 30, '120.00', 'cash', 'auto'),
        (15, 18, 0, '800.00', 'online', 'sedan'),
        (22, 8, 15, '250.00', 'online', 'auto'),
    ]
    for i, (day, hour, minute, fare, method, vehicle) in enumerate(plan):
        rider = in_riders[i % 3]
        if day == 4:
            requested = datetime(2026, 10, day, hour, 0, tzinfo=UTC)          # UTC instant
        else:
            requested = at(IST, 10, day, hour, minute)
        done = requested + timedelta(minutes=15)
        trip = make_trip(hyd, rider, in_driver, fare=fare, method=method, requested=requested,
                         completed=done, vehicle=vehicle)
        settle_at(trip, done + timedelta(minutes=1))
        trips.hyd.append(trip)

    # A promo trip: the rider paid 150, the full fare was 200, the platform funded 50.
    promo_trip = make_trip(hyd, in_riders[0], in_driver, fare='200.00', method='cash',
                           requested=at(IST, 10, 12, 10), completed=at(IST, 10, 12, 10) + timedelta(minutes=15),
                           final_fare=Decimal('150.00'))
    settle_at(promo_trip, promo_trip.completed_at)
    entry = LedgerEntry.objects.create(
        kind='promo_discount_platform_funded', campaign=campaign, user=in_riders[0].id and in_riders[0],
        trip=promo_trip, amount_minor=5000, currency='INR', idempotency_key='rpt-promo-1')
    LedgerEntry.objects.filter(pk=entry.pk).update(created_at=promo_trip.completed_at)
    trips.hyd.append(promo_trip)
    trips.promo = promo_trip

    # A platform-funded trip end: the rider paid nothing, the platform paid the driver 100.
    funded = make_trip(hyd, in_riders[1], in_driver, fare='0.00', method='online',
                       requested=at(IST, 10, 17, 9), completed=at(IST, 10, 17, 9) + timedelta(minutes=20),
                       end_rule='platform_funded', platform_funded_minor=10000)
    wallet_row(in_driver.user_id, 'platform_funded_trip_end', 10000, 'INR', funded.completed_at,
               reference=f'TRIP_{funded.id}')
    trips.hyd.append(funded)

    # A refund on the first trip.
    refund = TripMoneyAction.objects.create(
        trip=trips.hyd[0], kind='refund', amount_minor=2500, currency='INR', method='wallet_credit',
        status='completed', reason='fixture')
    TripMoneyAction.objects.filter(pk=refund.pk).update(created_at=at(IST, 10, 3, 10))

    # Cancellations, all requested in October.
    for who, accepted, day in (('rider', False, 6), ('rider', True, 7), ('driver', True, 8),
                               ('admin', True, 10), ('system', False, 11), ('system', True, 13),
                               ('', False, 14)):
        make_trip(hyd, in_riders[2], in_driver, fare='100.00', requested=at(IST, 10, day, 9),
                  status='cancelled', cancelled_by=who, accepted=accepted)
    trips.cancel_expected = {'rider': 2, 'driver': 1, 'ops': 2, 'no_driver': 2}
    # One that is still being requested (counted as requested only).
    make_trip(hyd, in_riders[2], in_driver, fare='100.00', requested=at(IST, 10, 16, 9),
              status='requested', accepted=False)

    # Rewards and a recovery (driver wallet ledger), and a RIDER referral reward placed by their
    # first trip (the rider has no home city).
    wallet_row(in_driver.user_id, 'incentive_reward', 30000, 'INR', at(IST, 10, 20, 9))
    wallet_row(in_driver.user_id, 'referral_reward', 10000, 'INR', at(IST, 10, 21, 9))
    wallet_row(in_driver.user_id, 'reward_recovery', 4000, 'INR', at(IST, 10, 25, 9), txn_type='debit')
    wallet_row(in_riders[0], 'referral_reward', 7500, 'INR', at(IST, 10, 18, 9))

    # --- Austin: completed trips either side of local midnight ------------------------------
    aplan = [
        # 22:30 CDT on Oct 4 is 03:30 UTC on Oct 5: the LOCAL day is the 4th.
        (at(CDT, 10, 4, 22, 30), '30.00', 'online', 'sedan'),
        (at(CDT, 10, 5, 0, 15), '18.00', 'cash', 'sedan'),
        (at(CDT, 10, 12, 8, 0), '45.00', 'online', 'suv'),
    ]
    for i, (requested, fare, method, vehicle) in enumerate(aplan):
        done = requested + timedelta(minutes=18)
        trip = make_trip(aus, us_riders[i % 2], us_driver, fare=fare, method=method,
                         requested=requested, completed=done, vehicle=vehicle)
        settle_at(trip, done + timedelta(minutes=1))
        trips.aus.append(trip)
    fee = PlatformExpense.objects.create(
        kind=PlatformExpense.REGULATORY_FEE, trip_id=trips.aus[0].id, amount_minor=150,
        currency='USD', zone_code='US-TX-AUS', idempotency_key='rpt-fee-1')
    PlatformExpense.objects.filter(pk=fee.pk).update(created_at=trips.aus[0].completed_at)
    wallet_row(us_driver.user_id, 'incentive_reward', 2000, 'USD', at(CDT, 10, 9, 9))
    make_trip(aus, us_riders[1], us_driver, fare='20.00', requested=at(CDT, 10, 6, 9),
              status='cancelled', cancelled_by='rider', accepted=False)

    # --- Supply, support, safety, fraud -------------------------------------------------------
    DriverSession.objects.create(driver=in_driver, started_at=at(IST, 10, 2, 9),
                                 ended_at=at(IST, 10, 2, 13), duration_seconds=4 * 3600)
    # A session across Hyderabad midnight: 2 h on Oct 2 local, 3 h on Oct 3 local.
    DriverSession.objects.create(driver=in_driver, started_at=at(IST, 10, 2, 22),
                                 ended_at=at(IST, 10, 3, 3), duration_seconds=5 * 3600)
    ticket = SupportTicket.objects.create(user_id=in_riders[0], trip_id=trips.hyd[0],
                                          issue_type='payment', status='OPEN', priority='normal')
    SupportTicket.objects.filter(pk=ticket.pk).update(
        created_at=at(IST, 10, 5, 10), first_response_due_at=at(IST, 10, 5, 14),
        first_responded_at=at(IST, 10, 5, 16), resolution_due_at=at(IST, 10, 7, 10))
    sos = SOSEvent.objects.create(user=in_riders[0], initiated_by='rider', trip=trips.hyd[0],
                                  event_type='panic', latitude=Decimal('17.4300000'),
                                  longitude=Decimal('78.4000000'))
    SOSEvent.objects.filter(pk=sos.pk).update(created_at=at(IST, 10, 8, 21))
    ack = SOSEventUpdate.objects.create(event=sos, new_status='acknowledged')
    SOSEventUpdate.objects.filter(pk=ack.pk).update(created_at=at(IST, 10, 8, 21, 3))
    # Raising an SOS opens its safety incident by itself (one per trip).
    sos.refresh_from_db()
    incident = sos.incident
    SafetyIncident.objects.filter(pk=incident.pk).update(opened_at=at(IST, 10, 8, 21, 1))
    incident.refresh_from_db()
    flag = RiskFlag.objects.create(subject=in_riders[1], kind='referral_abuse',
                                   anchor_node=geo['IN-TG-HYD'], source_ref='referral:rpt1',
                                   status='confirmed', decided_at=at(IST, 10, 9, 12))
    RiskFlag.objects.filter(pk=flag.pk).update(created_at=at(IST, 10, 8, 12))

    return SimpleNamespace(
        geo=geo, hyd=hyd, aus=aus, in_driver=in_driver, us_driver=us_driver,
        in_riders=in_riders, us_riders=us_riders, trips=trips, campaign=campaign,
        ticket=ticket, sos=sos, incident=incident, flag=flag,
        start=datetime(2026, 10, 1).date(), end=datetime(2026, 10, 31).date())
