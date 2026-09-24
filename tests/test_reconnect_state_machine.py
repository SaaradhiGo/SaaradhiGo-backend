"""On reconnect, the server must hand back durable truth -- not a guess.

The case that matters
---------------------
A driver sends `complete`, the transaction commits, and the acknowledgement is lost
(socket dropped, channel over capacity, app killed). On reconnect the client must be
able to tell which of two worlds it is in:

  * the completion committed  -> discover `completed`, stop retrying;
  * the completion never ran   -> discover `in_progress`, retry safely.

Guessing wrong in the first direction is the resolved pilot blocker. Guessing wrong
in the second means a ride nobody ever finishes.

PostgreSQL wins
---------------
Every assertion here reads the trip through the consumer's own greeting, and the
Redis active-trip cache is deliberately corrupted in one test to prove the greeting
does not come from it. A stale cache is exactly how a completed ride would look
unfinished.
"""

import asyncio
import json
from decimal import Decimal

import pytest
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model

from base.asgi import application
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus

User = get_user_model()

LAT, LNG = 17.4450000, 78.3800000

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio,
              pytest.mark.django_db(transaction=True)]


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _make_driver(suffix):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    u = User.objects.create_user(phone_number=f'+9198200{suffix:05d}', role='driver')
    d = Driver.objects.create(user_id=u, approved=True, status='active')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number=f'TS09RC{suffix:04d}')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


def _make_rider(suffix):
    return User.objects.create_user(phone_number=f'+9197200{suffix:05d}', role='rider')


def _make_trip(rider, driver, status):
    """A trip in `status`, with the timestamps that status implies.

    `requested` deliberately has no driver assigned, because a requested trip has
    no driver by definition and pretending otherwise would test a state the system
    cannot be in.
    """
    from django.utils import timezone

    t = Trip.objects.create(
        user_id=rider, status_id=_status(status),
        pickup_lat=Decimal(str(LAT)), pickup_long=Decimal(str(LNG)),
        destination_lat=Decimal(str(LAT + 0.01)), destination_long=Decimal(str(LNG)),
        estimated_fare=Decimal('120.00'), payment_method='cash', otp='123456',
    )
    now = timezone.now()
    if status == 'requested':
        return t

    t.driver_id = driver
    t.accepted_at = now
    if status in ('reached', 'in_progress', 'completed'):
        t.reached_at = now
    if status in ('in_progress', 'completed'):
        t.started_at = now
    if status == 'completed':
        t.completed_at = now
    if status == 'cancelled':
        t.cancelled_at = now
        t.cancelled_by = 'rider'
    t.save()
    return t


def _token(user):
    from rest_framework_simplejwt.tokens import AccessToken

    return str(AccessToken.for_user(user))


async def _connect(user, trip_id, expect_ok=True):
    """Open the trip socket and return its greeting (or None if refused)."""
    tok = await asyncio.to_thread(_token, user)
    comm = WebsocketCommunicator(application, f'/ws/ride/trip/{trip_id}/?token={tok}')
    connected, code = await comm.connect(timeout=20)
    if not connected:
        return comm, None, code
    greeting = json.loads(await comm.receive_from(timeout=10))
    assert not expect_ok or greeting['type'] == 'connection_established', greeting
    return comm, greeting, None


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


async def _command(comm, action, timeout=15, **extra):
    import uuid

    cid = str(uuid.uuid4())
    await comm.send_to(text_data=json.dumps(
        {'action': action, 'command_id': cid, **extra}))
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for msg in _pending(comm):
            if (msg.get('type') == 'command_ack'
                    and msg.get('command_id') == cid):
                return msg
        await asyncio.sleep(0.02)
    return None


def _trip_status(trip_id):
    return (Trip.objects.filter(id=trip_id)
            .values_list('status_id__status_code', flat=True).first())


async def _shutdown(*comms):
    for c in comms:
        try:
            await c.disconnect(timeout=5)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Reconnect in every lifecycle state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('status', [
    'accepted', 'reached', 'in_progress', 'completed', 'cancelled',
])
async def test_the_driver_reconnects_into_the_durable_status(status):
    """A driver reconnecting mid-ride is told the committed status, whatever it is.

    Terminal states included: a driver whose app restarted after a ride finished
    must learn that it finished, not be left to infer it from silence.
    """
    suffix = 100 + abs(hash(status)) % 800
    rider = await asyncio.to_thread(_make_rider, suffix)
    driver = await asyncio.to_thread(_make_driver, suffix)
    trip = await asyncio.to_thread(_make_trip, rider, driver, status)

    comm, greeting, _ = await _connect(driver.user_id, trip.id)
    print(f'driver reconnect in {status}: greeting={greeting}')
    await _shutdown(comm)

    assert greeting['trip_status'] == status, (
        f'reconnected into {greeting.get("trip_status")!r} but the database says '
        f'{status!r}'
    )
    assert greeting['subscribed'] is True


@pytest.mark.parametrize('status', [
    'requested', 'accepted', 'reached', 'in_progress', 'completed', 'cancelled',
])
async def test_the_rider_reconnects_into_the_durable_status(status):
    """The rider gets the same answer, including before any driver exists."""
    suffix = 900 + abs(hash(status)) % 800
    rider = await asyncio.to_thread(_make_rider, suffix)
    driver = await asyncio.to_thread(_make_driver, suffix)
    trip = await asyncio.to_thread(_make_trip, rider, driver, status)

    comm, greeting, _ = await _connect(rider, trip.id)
    print(f'rider reconnect in {status}: greeting={greeting}')
    await _shutdown(comm)

    assert greeting['trip_status'] == status


# ---------------------------------------------------------------------------
# The case this exists for
# ---------------------------------------------------------------------------

async def test_a_completion_whose_ack_was_lost_is_discovered_on_reconnect():
    """Disconnect after COMPLETE commits but before the ack is read.

    The client cannot distinguish "my command was lost" from "my acknowledgement
    was lost" at the moment of failure. On reconnect it must be able to.
    """
    rider = await asyncio.to_thread(_make_rider, 1801)
    driver = await asyncio.to_thread(_make_driver, 1801)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    first, _, _ = await _connect(driver.user_id, trip.id)
    # Send complete and drop the socket without reading the answer at all.
    await first.send_to(text_data=json.dumps({'action': 'complete'}))
    await asyncio.sleep(2)
    await _shutdown(first)

    committed = await asyncio.to_thread(_trip_status, trip.id)
    again, greeting, _ = await _connect(driver.user_id, trip.id)
    print(f'lost-ack reconnect: db={committed} greeting_status={greeting["trip_status"]}')
    await _shutdown(again)

    assert committed == 'completed', 'the completion should have committed'
    assert greeting['trip_status'] == 'completed', (
        'the reconnecting client was not told the completion committed, so it '
        'cannot tell a lost ack from a lost command'
    )


async def test_retrying_after_a_reconnect_is_told_already_done():
    """And having reconnected, a retry is safe and correctly reported."""
    rider = await asyncio.to_thread(_make_rider, 1802)
    driver = await asyncio.to_thread(_make_driver, 1802)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    first, _, _ = await _connect(driver.user_id, trip.id)
    ack = await _command(first, 'complete')
    assert ack['status'] == 'committed'
    await _shutdown(first)

    again, greeting, _ = await _connect(driver.user_id, trip.id)
    retry = await _command(again, 'complete')
    print(f'retry after reconnect: greeting={greeting["trip_status"]} retry={retry}')
    await _shutdown(again)

    assert retry is not None, 'the retry must be answered'
    assert retry['status'] == 'already_done', retry
    assert retry['trip_status'] == 'completed'


async def test_a_command_lost_before_commit_can_still_be_retried():
    """The other direction. A command that never ran must remain retryable.

    Reconnecting must not leave the app believing something happened that did not,
    or a ride ends up with nobody willing to finish it.
    """
    rider = await asyncio.to_thread(_make_rider, 1803)
    driver = await asyncio.to_thread(_make_driver, 1803)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    # Connect and drop WITHOUT sending anything -- the command never reached us.
    first, _, _ = await _connect(driver.user_id, trip.id)
    await _shutdown(first)

    again, greeting, _ = await _connect(driver.user_id, trip.id)
    assert greeting['trip_status'] == 'in_progress', greeting
    ack = await _command(again, 'complete')
    final = await asyncio.to_thread(_trip_status, trip.id)
    print(f'retry after a lost command: ack={ack and ack["status"]} db={final}')
    await _shutdown(again)

    assert ack['status'] == 'committed', (
        'a command that never committed must still be performable')
    assert final == 'completed'


# ---------------------------------------------------------------------------
# PostgreSQL wins over Redis
# ---------------------------------------------------------------------------

async def test_the_greeting_ignores_a_stale_redis_active_trip_cache():
    """A stale cache must not be able to misreport a finished ride.

    The Redis active-trip key is written at accept and cleared at completion. If the
    greeting read from Redis, a key left behind by a crash would tell a reconnecting
    driver their completed ride was still running -- and they would retry a
    completion that had already happened, or worse, believe the fare was unsettled.
    """
    from servers.redis_client import get_driver_active_trip, set_driver_active_trip

    rider = await asyncio.to_thread(_make_rider, 1804)
    driver = await asyncio.to_thread(_make_driver, 1804)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'completed')

    # Deliberately lie in Redis: claim this completed trip is still active.
    await asyncio.to_thread(set_driver_active_trip, driver.id, trip.id)
    cached = await asyncio.to_thread(get_driver_active_trip, driver.id)

    comm, greeting, _ = await _connect(driver.user_id, trip.id)
    print(f'stale cache says active_trip={cached}; greeting says '
          f'{greeting["trip_status"]}')
    await _shutdown(comm)

    assert greeting['trip_status'] == 'completed', (
        'the greeting followed the stale Redis cache instead of PostgreSQL'
    )


# ---------------------------------------------------------------------------
# Reconnect must not widen access
# ---------------------------------------------------------------------------

async def test_an_unrelated_driver_still_cannot_reconnect_into_a_trip():
    """The greeting must not become an information leak.

    It now carries trip state, so the participation gate matters more than before:
    a driver who is not assigned must be refused the socket entirely rather than
    shown the status of somebody else's ride.
    """
    rider = await asyncio.to_thread(_make_rider, 1805)
    driver = await asyncio.to_thread(_make_driver, 1805)
    stranger = await asyncio.to_thread(_make_driver, 1905)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    comm, greeting, code = await _connect(stranger.user_id, trip.id,
                                          expect_ok=False)
    print(f'stranger connect: greeting={greeting} close_code={code}')
    await _shutdown(comm)

    assert greeting is None, (
        f'an unassigned driver was given trip state: {greeting!r}')
    assert code == 4003


async def test_an_unrelated_rider_cannot_reconnect_into_a_trip():
    rider = await asyncio.to_thread(_make_rider, 1806)
    other_rider = await asyncio.to_thread(_make_rider, 1906)
    driver = await asyncio.to_thread(_make_driver, 1806)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    comm, greeting, code = await _connect(other_rider, trip.id, expect_ok=False)
    await _shutdown(comm)

    assert greeting is None, f'another rider saw this trip: {greeting!r}'
    assert code == 4003
