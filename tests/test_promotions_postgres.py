"""Module 12 on real PostgreSQL: the budget lock, and a booking over the WebSocket with a promo.

Marked `postgres` and excluded from the default run (`pytest -m postgres`): SQLite has no row
locks, so "two riders racing for the last budget" can only be PROVED here. The sequential logic
(the second claim is refused once the first took the budget) is in test_promotions_settlement.py
and runs everywhere.

What is being proven
--------------------
`budget.reserve` takes the campaign row with SELECT ... FOR UPDATE. Two threads reserve the LAST
discount at once; the first holds the lock (the test makes it linger inside the critical section)
while the second runs. The second therefore BLOCKS on the row, and when it gets it the budget is
gone: exactly one reservation exists, the campaign counters are exact, and the wall-clock of the
second proves it genuinely waited (a green result cannot come from scheduling luck).
"""

import asyncio
import json
import threading
import time
from decimal import Decimal
from unittest import mock

import pytest
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.utils import timezone

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]

User = get_user_model()
LOCK_HOLD_SECONDS = 1.5
JOIN_TIMEOUT_SECONDS = 30


def _require_postgres():
    if connection.vendor != 'postgresql':
        pytest.skip(f'needs PostgreSQL, got {connection.vendor!r}')


def _world(budget_minor, discount_minor=5_000, per_user=1):
    from servers.promotions.models import Campaign
    from servers.ride.models import PromoCode

    now = timezone.now()
    promo = PromoCode.objects.create(
        code='RACE', discount_type='flat', discount_flat_minor=discount_minor, currency='INR',
        valid_from=now - timezone.timedelta(days=1), valid_to=now + timezone.timedelta(days=5),
        max_per_user_redemptions=per_user)
    return Campaign.objects.create(
        kind='rider_promo', name='Race', state='live', country='IN', currency='INR',
        budget_minor=budget_minor, starts_at=now - timezone.timedelta(days=1),
        ends_at=now + timezone.timedelta(days=5), promo_code=promo,
        config={'code': 'RACE', 'discount_type': 'flat', 'discount_flat_minor': discount_minor,
                'max_per_user': per_user})


def _two_riders_and_trips():
    from finance_world import make_driver, make_rider, make_trip

    driver = make_driver(None, 'IN-TG-HYD', 'T', currency='INR', calling='91', city_node=False)
    riders = [make_rider(f'RACE{i}', '91') for i in range(2)]
    return riders, [make_trip(None, r, driver, fare='200.00') for r in riders]


def _race(fn, count=2):
    """Run `fn(i)` on `count` threads, the first one lingering inside the lock. Returns
    `(results, durations)`."""
    results, durations, errors = [None] * count, [0.0] * count, []
    started = threading.Event()

    def runner(i):
        try:
            if i > 0:
                started.wait(5)
                time.sleep(0.2)                       # the first thread is already inside
            t0 = time.monotonic()
            results[i] = fn(i, started)
            durations[i] = time.monotonic() - t0
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT_SECONDS)
    assert not any(t.is_alive() for t in threads), 'a thread hung: a lock was never released'
    assert not errors, errors
    return results, durations


def test_two_reservations_for_the_last_budget_one_wins_and_the_other_waits_then_loses():
    _require_postgres()
    from servers.promotions import budget
    from servers.promotions.models import Campaign

    campaign = _world(budget_minor=5_000)
    real = budget._after_change

    def lingering(c):
        if not lingering.done:
            lingering.done = True
            lingering.entered.set()
            time.sleep(LOCK_HOLD_SECONDS)             # hold the row lock inside the section
        return real(c)
    lingering.done, lingering.entered = False, threading.Event()

    def reserve(i, started):
        if i == 0:
            lingering.entered = started
        try:
            budget.reserve(campaign.pk, 5_000)
            return 'reserved'
        except budget.Refused as exc:
            return exc.code

    with mock.patch.object(budget, '_after_change', lingering):
        results, durations = _race(reserve)
    assert sorted(results) == [budget.NOT_LIVE, 'reserved'] or sorted(results) == [
        budget.BUDGET_EXHAUSTED, 'reserved'], results
    loser = durations[results.index(next(r for r in results if r != 'reserved'))]
    assert loser >= LOCK_HOLD_SECONDS * 0.6, f'the loser did not wait for the lock ({loser:.2f}s)'
    campaign = Campaign.objects.get(pk=campaign.pk)
    assert campaign.committed_minor == 5_000 and campaign.state == 'exhausted'


def test_two_riders_redeeming_the_last_discount_one_gets_it():
    _require_postgres()
    from servers.promotions import budget
    from servers.promotions.models import Campaign
    from servers.ride.models import PromoRedemption
    from servers.ride.promos import redeem_promo

    campaign = _world(budget_minor=5_000)
    riders, trips = _two_riders_and_trips()
    real = budget._after_change
    gate = {'first': True}

    def lingering(c):
        if gate['first']:
            gate['first'] = False
            time.sleep(LOCK_HOLD_SECONDS)
        return real(c)

    def redeem(i, started):
        started.set()
        return redeem_promo('RACE', riders[i], trips[i], Decimal('200.00')).ok

    with mock.patch.object(budget, '_after_change', lingering):
        results, _ = _race(redeem)
    assert sorted(results) == [False, True], results
    assert PromoRedemption.objects.count() == 1
    campaign = Campaign.objects.get(pk=campaign.pk)
    assert (campaign.committed_minor, campaign.spent_minor) == (5_000, 0)


def test_many_threads_never_overspend_a_hard_cap():
    _require_postgres()
    from servers.promotions import budget
    from servers.promotions.models import Campaign

    campaign = _world(budget_minor=20_000)            # room for exactly four 5,000 discounts

    def reserve(i, started):
        started.set()
        try:
            budget.reserve(campaign.pk, 5_000)
            return True
        except budget.Refused:
            return False

    results, _ = _race(reserve, count=8)
    assert sum(results) == 4
    campaign = Campaign.objects.get(pk=campaign.pk)
    assert campaign.committed_minor == 20_000 and campaign.overshoot_minor == 0


# ---------------------------------------------------------------------------
# A real booking over the WebSocket
# ---------------------------------------------------------------------------

P_LAT, P_LNG = 17.4450000, 78.3800000
D_LAT, D_LNG = 17.4550000, 78.3800000


def _token(user):
    from rest_framework_simplejwt.tokens import AccessToken

    return str(AccessToken.for_user(user))


def _pending(comm):
    out = []
    while True:
        try:
            raw = comm.output_queue.get_nowait()
        except asyncio.QueueEmpty:
            return out
        if raw.get('type') != 'websocket.send':
            continue
        try:
            out.append(json.loads(raw.get('text') or '{}'))
        except ValueError:
            continue


async def _book(rider, promo_code):
    from base.asgi import application

    tok = await asyncio.to_thread(_token, rider)
    comm = WebsocketCommunicator(application, f'/ws/ride/request/?token={tok}')
    connected, _ = await comm.connect(timeout=20)
    assert connected
    await comm.receive_from(timeout=10)
    body = {'pickup_lat': P_LAT, 'pickup_lng': P_LNG, 'destination_lat': D_LAT,
            'destination_lng': D_LNG, 'pickup_address': 'Promo test pickup',
            'destination_address': 'Promo test drop', 'distance_km': 1.2, 'duration_min': 4,
            'vehicle_type': 'sedan', 'payment_method': 'cash'}
    if promo_code:
        body['promo_code'] = promo_code
    await comm.send_to(text_data=json.dumps(body))
    deadline = asyncio.get_running_loop().time() + 25
    try:
        while asyncio.get_running_loop().time() < deadline:
            for msg in _pending(comm):
                if msg.get('type') in ('trip_created', 'error'):
                    return msg
            await asyncio.sleep(0.05)
    finally:
        try:
            await comm.disconnect()
        except BaseException:  # noqa: BLE001
            pass
    return None


def _ready():
    from conftest import ensure_zone_covering
    from servers.driver.models import VehicleType
    from servers.pricing.models import RateCard

    zone = ensure_zone_covering(P_LAT, P_LNG)
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    if not RateCard.objects.filter(zone=zone, vehicle_type=vt).exists():
        RateCard.objects.create(zone=zone, vehicle_type=vt, base_fare=30, per_km_fare=12,
                                per_min_fare=2, min_fare=50)


@pytest.mark.asyncio
async def test_a_booking_with_a_promo_reserves_the_discount_and_keeps_the_full_fare():
    _require_postgres()
    from servers.promotions.models import Campaign
    from servers.ride.models import FarePricing, PromoRedemption, Trip

    await asyncio.to_thread(_ready)
    campaign = await asyncio.to_thread(_world, 100_000, 1_000)
    rider = await asyncio.to_thread(User.objects.create_user, phone_number='+919600099101',
                                    role='rider')
    created = await _book(rider, 'race')
    assert created and created['type'] == 'trip_created', created
    assert created['promo_code'] == 'RACE' and created['discount_amount'] == '10.00'

    def read():
        trip = Trip.objects.get(user_id_id=rider.id)
        redemption = PromoRedemption.objects.get(trip=trip)
        fp = FarePricing.objects.get(trip_id=trip)
        return trip, redemption, fp, Campaign.objects.get(pk=campaign.pk)

    trip, redemption, fp, campaign = await asyncio.to_thread(read)
    assert redemption.state == 'committed' and redemption.discount_amount_minor == 1_000
    assert campaign.committed_minor == 1_000
    # The trip carries the FULL fare; the rider is shown what they will pay.
    assert Decimal(created['estimated_fare']) == trip.estimated_fare
    assert Decimal(created['rider_payable']) == trip.estimated_fare - Decimal('10.00')
    assert fp.gross_fare_minor == trip.estimated_fare_minor
    assert fp.discount_amount_minor == 1_000 and fp.promo_code == 'RACE'
    assert fp.rider_payable_minor == trip.estimated_fare_minor - 1_000


@pytest.mark.asyncio
async def test_a_booking_with_a_code_that_cannot_apply_is_refused_and_creates_no_trip():
    _require_postgres()
    from servers.promotions.models import Campaign
    from servers.ride.models import Trip

    await asyncio.to_thread(_ready)
    campaign = await asyncio.to_thread(_world, 100_000, 1_000)
    await asyncio.to_thread(Campaign.objects.filter(pk=campaign.pk).update, state='paused')
    rider = await asyncio.to_thread(User.objects.create_user, phone_number='+919600099102',
                                    role='rider')
    reply = await _book(rider, 'RACE')
    assert reply and reply['type'] == 'error' and reply['code'] == 'PROMO_NOT_FOUND', reply
    assert not await asyncio.to_thread(Trip.objects.filter(user_id_id=rider.id).exists)


@pytest.mark.asyncio
async def test_a_booking_with_no_code_is_unchanged():
    _require_postgres()
    from servers.ride.models import PromoRedemption, Trip

    await asyncio.to_thread(_ready)
    rider = await asyncio.to_thread(User.objects.create_user, phone_number='+919600099103',
                                    role='rider')
    created = await _book(rider, '')
    assert created and created['type'] == 'trip_created' and 'promo_code' not in created
    assert await asyncio.to_thread(Trip.objects.filter(user_id_id=rider.id).exists)
    assert not await asyncio.to_thread(PromoRedemption.objects.exists)
