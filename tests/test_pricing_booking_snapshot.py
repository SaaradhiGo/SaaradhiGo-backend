"""A real booking over the WebSocket writes the commission snapshot (module 8).

The unit tests in test_pricing_module.py prove the snapshot function and the
settlement; this proves the CONSUMER calls it: a rider books, the Trip row carries
the percent, rule id and rule version in force at that moment, and a rule changed
afterwards does not reach that trip's settlement.

PostgreSQL-marked like every other WebSocket booking test (`pytest -m postgres`).
"""

import asyncio
import json
from decimal import Decimal

import pytest
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.utils import timezone

from base.asgi import application
from servers.pricing.commission_models import ACTIVE, ARCHIVED, CommissionRule
from servers.ride.models import Trip

User = get_user_model()

# Inside the seeded Hyderabad polygon, far enough apart to pass distance validation.
P_LAT, P_LNG = 17.4450000, 78.3800000
D_LAT, D_LNG = 17.4550000, 78.3800000

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio,
              pytest.mark.django_db(transaction=True)]


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


async def _book_one(rider):
    tok = await asyncio.to_thread(_token, rider)
    comm = WebsocketCommunicator(application, f'/ws/ride/request/?token={tok}')
    connected, _ = await comm.connect(timeout=20)
    assert connected
    await comm.receive_from(timeout=10)
    await comm.send_to(text_data=json.dumps({
        'pickup_lat': P_LAT, 'pickup_lng': P_LNG, 'destination_lat': D_LAT,
        'destination_lng': D_LNG, 'pickup_address': 'Snapshot test pickup',
        'destination_address': 'Snapshot test drop', 'distance_km': 1.2, 'duration_min': 4,
        'vehicle_type': 'sedan', 'payment_method': 'cash'}))
    deadline = asyncio.get_running_loop().time() + 25
    try:
        while asyncio.get_running_loop().time() < deadline:
            for msg in _pending(comm):
                if msg.get('type') == 'trip_created':
                    return msg
            await asyncio.sleep(0.05)
    finally:
        try:
            await comm.disconnect()
        except BaseException:  # noqa: BLE001
            pass
    return None


async def test_a_booking_snapshots_the_rule_and_a_later_change_never_reaches_it():
    def _ready():
        from conftest import ensure_zone_covering
        from servers.driver.models import VehicleType
        from servers.pricing.models import RateCard, ServiceZone

        # Self-sufficient: a transactional flush removes the migration-seeded Hyderabad.
        zone = ensure_zone_covering(P_LAT, P_LNG)
        vt, _ = VehicleType.objects.get_or_create(type='sedan')
        if not RateCard.objects.filter(zone=zone, vehicle_type=vt).exists():
            RateCard.objects.create(zone=zone, vehicle_type=vt, base_fare=30, per_km_fare=12,
                                    per_min_fare=2, min_fare=50)
        assert ServiceZone.objects.filter(pk=zone.pk).exists()

    await asyncio.to_thread(_ready)
    rider = await asyncio.to_thread(
        User.objects.create_user, phone_number='+919600099001', role='rider')
    rule = await asyncio.to_thread(
        CommissionRule.objects.get, country='IN', status=ACTIVE, version=1)
    created = await _book_one(rider)
    assert created is not None, 'the booking must succeed'

    trip = await asyncio.to_thread(Trip.objects.get, user_id_id=rider.id)
    assert trip.commission_percent_snapshot == Decimal('15.00')
    assert trip.commission_rule_id == rule.id and trip.commission_rule_version == 1

    # An operator changes the rate after the booking...
    def change_rule():
        CommissionRule.objects.filter(pk=rule.pk).update(
            effective_to=timezone.now(), status=ARCHIVED)
        CommissionRule.objects.create(
            country='IN', percent=Decimal('35.00'), status=ACTIVE, version=2,
            effective_from=timezone.now(), reason='after the booking')
        from servers.pricing.services import commission_percent_for_trip
        return commission_percent_for_trip(Trip.objects.get(pk=trip.pk))

    # ... and the trip still resolves its own snapshot.
    assert await asyncio.to_thread(change_rule) == Decimal('15.00')
