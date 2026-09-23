"""Lifecycle commands must survive sustained GPS traffic.

A QA rehearsal reported that `complete` stopped landing once a driver socket had
carried roughly twenty location frames: the trip stayed `in_progress` while the
client believed it had completed. This file reproduces the *mechanism* against the
real consumers, the real Redis channel layer and real PostgreSQL, and measures
where the boundary actually is rather than assuming twenty is meaningful.

What it actually was
--------------------
Twenty was not a threshold, and the database was never reached. Two distinct defects
came out of this, and only the first explains the reported symptom:

1. **The driver received an echo of its own GPS on its command socket.**
   `group_send('trip_<id>', driver_location_update)` reaches the whole trip group,
   so the assigned driver got one copy of every position it had just sent, on the
   socket carrying `complete`. The reference `websockets` client stops reading
   frames at sixteen queued messages -- *including Daphne's keepalive pings* -- so
   it stopped answering pongs, Daphne's 20s/30s ping timeout elapsed, and **the
   server closed the connection**. `complete` then went into a dead socket while the
   client's `send()` still succeeded. Needs a full buffer *and* ~50 more seconds of
   ride, which is why it looked like a frame count. Fixed by not sending a driver
   its own position.

2. **Location broadcasts could block the dispatch loop.** Channels feeds websocket
   frames and channel-layer events through one sequential `await_many_dispatch`
   loop, and the broadcasts were delivered with a blocking `await self.send(...)`.
   A client that genuinely applies backpressure stalls the loop, so `complete` is
   never dequeued. Fixed by coalescing location frames onto a depth-1 queue drained
   by a dedicated task. The same coupling silently ended GPS ingestion: 17 of 200
   frames reached the stream against an undrained client.

Ruled out by measurement, and recorded so it is not re-investigated: the
`database_sync_to_async` single-thread executor does **not** starve one driver's
lifecycle commands, because `receive` awaits its hops sequentially and can only ever
have one job queued. It remains a multi-driver scaling concern.

Harness notes, because both cost a day
--------------------------------------
* `WebsocketCommunicator.receive_from(timeout=...)` **cancels the application task**
  when it times out (asgiref's `ApplicationCommunicator.receive_output` does). Using
  it to poll "is there anything there?" silently kills the consumer under test. Read
  `output_queue` directly instead -- see `_pending`.
* The Redis active-trip key is written by `_accept_trip`, not by creating a `Trip`.
  A trip created directly in the database looks *inactive* to
  `redis_client.add_driver_location`, so no GPS frame fans out to the trip group and
  coupling (2) is absent. `_make_trip` sets it explicitly.

These tests are marked `postgres` because the thing measured is contention over real
database work; SQLite in-memory would hide it.
"""

import asyncio
import json
import time
from decimal import Decimal

import pytest
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model

from base.asgi import application
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus

User = get_user_model()

LAT, LNG = 17.4450000, 78.3800000
STEP = 0.0005

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio,
              pytest.mark.django_db(transaction=True)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _make_driver(suffix):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    u = User.objects.create_user(phone_number=f'+9198000{suffix:05d}', role='driver')
    d = Driver.objects.create(user_id=u, approved=True, status='active')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number=f'TS09LD{suffix:04d}')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


def _make_rider(suffix):
    return User.objects.create_user(phone_number=f'+9197001{suffix:05d}', role='rider')


def _make_trip(rider, driver, status='in_progress'):
    """An in-progress trip in the same state `accept` + `start` would leave.

    Including the Redis active-trip key, which is what makes every subsequent
    location frame fan out to the trip group. Without it this test measures
    nothing.
    """
    from django.utils import timezone

    from servers.redis_client import set_driver_active_trip

    t = Trip.objects.create(
        user_id=rider, status_id=_status(status),
        pickup_lat=Decimal(str(LAT)), pickup_long=Decimal(str(LNG)),
        destination_lat=Decimal(str(LAT + 0.01)), destination_long=Decimal(str(LNG)),
        estimated_fare=Decimal('120.00'), payment_method='cash', otp='123456',
    )
    t.driver_id = driver
    t.accepted_at = timezone.now()
    t.reached_at = timezone.now()
    t.started_at = timezone.now()
    t.save(update_fields=['driver_id', 'accepted_at', 'reached_at', 'started_at'])
    set_driver_active_trip(driver.id, t.id)
    return t


def _trip_status(trip_id):
    return (Trip.objects.filter(id=trip_id)
            .values_list('status_id__status_code', flat=True).first())


def _token(user):
    from rest_framework_simplejwt.tokens import AccessToken

    return str(AccessToken.for_user(user))


async def _open_driver_socket(driver_user):
    tok = await asyncio.to_thread(_token, driver_user)
    comm = WebsocketCommunicator(
        application, f'/ws/driver/location/?token={tok}&lat={LAT}&lng={LNG}')
    connected, _ = await comm.connect(timeout=20)
    assert connected, 'driver location socket must connect'
    await comm.receive_from(timeout=10)          # connection_established
    return comm


async def _open_trip_socket(user, trip_id):
    tok = await asyncio.to_thread(_token, user)
    comm = WebsocketCommunicator(application, f'/ws/ride/trip/{trip_id}/?token={tok}')
    connected, _ = await comm.connect(timeout=20)
    assert connected, 'trip socket must connect'
    await comm.receive_from(timeout=10)          # connection_established
    return comm


def _pending(comm):
    """Everything already queued, without cancelling the application.

    Deliberately not `receive_from`: that cancels the app on timeout.
    """
    out = []
    while True:
        try:
            out.append(comm.output_queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


async def _await_frame(comm, predicate, seconds):
    """Wait for a frame satisfying `predicate`, non-destructively.

    Returns (frame_or_None, seconds_waited, other_frames_seen).
    """
    seen = []
    deadline = time.monotonic() + seconds
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        for raw in _pending(comm):
            if raw.get('type') != 'websocket.send':
                continue
            try:
                msg = json.loads(raw.get('text') or '{}')
            except ValueError:
                continue
            if predicate(msg):
                return msg, time.monotonic() - t0, seen
            seen.append(msg)
        await asyncio.sleep(0.02)
    return None, time.monotonic() - t0, seen


def _is_status(msg, status):
    return (msg.get('type') == 'trip_status_update'
            and msg.get('status') == status)


async def _shutdown(*comms):
    for comm in comms:
        _pending(comm)
        try:
            await comm.disconnect(timeout=5)
        except Exception:  # noqa: BLE001 -- teardown must not mask the assertion
            pass


async def _send_gps(dws, frames):
    for i in range(frames):
        await dws.send_to(text_data=json.dumps({'lat': LAT + STEP * i, 'lng': LNG}))


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

FRAME_COUNTS = [0, 1, 5, 10, 19, 20, 21, 25, 50, 100, 250, 500]

# How long a lifecycle command may take to land. A driver tapping "complete" gets
# an unresponsive app well before this; it is a ceiling, not a target.
LIFECYCLE_BUDGET_SECONDS = 15.0


@pytest.mark.parametrize('frames', FRAME_COUNTS)
async def test_complete_still_lands_after_n_location_frames(frames):
    """The headline matrix: `complete` must land at every frame count.

    Each case records the full observation list the investigation asks for --
    acknowledgement frame, wall-clock latency, committed DB status -- so a failure
    says *which* of them broke rather than only that something did.
    """
    rider = await asyncio.to_thread(_make_rider, frames)
    driver = await asyncio.to_thread(_make_driver, 10000 + frames)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dws = await _open_driver_socket(driver.user_id)
    tws = await _open_trip_socket(driver.user_id, trip.id)
    rws = await _open_trip_socket(rider, trip.id)   # the rider is in the group too

    # Sustained location traffic, sent as fast as the client can: the worst
    # realistic case is a reconnecting app flushing a buffer.
    t0 = time.monotonic()
    await _send_gps(dws, frames)
    gps_send = time.monotonic() - t0

    # Then the ordinary lifecycle command, on its own socket.
    t1 = time.monotonic()
    await tws.send_to(text_data=json.dumps({'action': 'complete'}))
    ack, ack_latency, before_ack = await _await_frame(
        tws, lambda m: _is_status(m, 'complete'), LIFECYCLE_BUDGET_SECONDS)

    # The acknowledgement is not the claim that matters. The committed row is.
    committed = None
    deadline = time.monotonic() + LIFECYCLE_BUDGET_SECONDS
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_trip_status, trip.id) == 'completed':
            committed = time.monotonic() - t1
            break
        await asyncio.sleep(0.1)
    final_status = await asyncio.to_thread(_trip_status, trip.id)

    socket_alive = not any(r.get('type') == 'websocket.close' for r in before_ack)
    print(f'frames={frames:4d} gps_send={gps_send:6.2f}s ack={ack is not None} '
          f'ack_latency={ack_latency:6.2f}s '
          f'committed={"%.2fs" % committed if committed else "NO"} '
          f'final={final_status} loc_frames_seen={len(before_ack)} '
          f'socket_alive={socket_alive}')

    await _shutdown(dws, tws, rws)

    assert final_status == 'completed', (
        f'trip stayed {final_status!r} after {frames} location frames: '
        f'acknowledgement={ack!r}, {len(before_ack)} other frames seen first, '
        f'socket_alive={socket_alive}'
    )
    assert ack is not None, (
        f'completion committed but the driver was never told, after {frames} '
        'location frames -- the client will retry or report failure'
    )


# ---------------------------------------------------------------------------
# The coupling, measured directly rather than only via its worst symptom
# ---------------------------------------------------------------------------

async def test_lifecycle_latency_does_not_grow_with_frame_count():
    """Latency for `complete` must not scale with queued location frames.

    This is the actual defect shape. A trip that completes in 0.2s on a quiet
    socket and 20s behind a burst of location frames is "working" in a test suite
    and broken on a real twenty-minute ride.
    """
    timings = {}
    for idx, frames in enumerate((0, 50, 250, 500)):
        rider = await asyncio.to_thread(_make_rider, 500 + idx)
        driver = await asyncio.to_thread(_make_driver, 20000 + idx)
        trip = await asyncio.to_thread(_make_trip, rider, driver)

        dws = await _open_driver_socket(driver.user_id)
        tws = await _open_trip_socket(driver.user_id, trip.id)

        await _send_gps(dws, frames)

        t0 = time.monotonic()
        await tws.send_to(text_data=json.dumps({'action': 'complete'}))
        while time.monotonic() - t0 < 120:
            if await asyncio.to_thread(_trip_status, trip.id) == 'completed':
                break
            await asyncio.sleep(0.05)
        timings[frames] = time.monotonic() - t0

        await _shutdown(dws, tws)

    print('complete latency by queued location frames:',
          {k: round(v, 2) for k, v in timings.items()})

    baseline = max(timings[0], 0.25)
    assert timings[500] < baseline * 4, (
        f'`complete` latency grew from {timings[0]:.2f}s to {timings[500]:.2f}s as '
        'location frames queued ahead of it: location traffic and lifecycle '
        'commands are coupled'
    )


# ---------------------------------------------------------------------------
# The actual defect: a client that does not drain fast enough
# ---------------------------------------------------------------------------

def _bound_client_buffer(comm, maxsize):
    """Give the test client a bounded receive buffer, like every real client has.

    Nothing about the product is mocked here. `WebsocketCommunicator` hands the
    application `output_queue.put` as its ASGI `send`, and an unbounded queue makes
    the test client infinitely fast at draining -- which no real client is. The
    reference Python `websockets` client buffers 16 messages by default; a Flutter
    client on a weak mobile link is slower still. Once the buffer is full, `await
    send(...)` inside the consumer blocks, which is exactly the real condition:
    TCP backpressure from a client that is not keeping up.

    `_maxsize` rather than a fresh Queue because the application task captured the
    bound `put` in `ApplicationCommunicator.__init__` and cannot be re-pointed.
    """
    comm.output_queue._maxsize = maxsize
    return comm


CLIENT_BUFFER = 16      # the `websockets` library default, and a generous one


async def test_complete_is_lost_when_the_client_buffer_fills():
    """The reproduction. A trip socket that is not drained cannot be completed.

    Every location frame fans out to the trip group, so the driver's own trip
    socket receives one `driver_location_update` per GPS ping it sends. Those
    broadcasts and the `complete` command share `TripStatusConsumer`'s single
    `await_many_dispatch` loop, and the broadcasts are delivered with a blocking
    `await self.send(...)`. Once the client's buffer is full that send blocks, the
    loop stops, and `complete` sits in the incoming queue forever -- received by
    the kernel, never routed to a handler.

    This is failure mode "handler never entered", not "client never sent" and not
    "database transaction failed", and it is why the QA harness believed the trip
    had completed: the send succeeded.
    """
    rider = await asyncio.to_thread(_make_rider, 900)
    driver = await asyncio.to_thread(_make_driver, 30900)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dws = await _open_driver_socket(driver.user_id)
    tws = _bound_client_buffer(await _open_trip_socket(driver.user_id, trip.id),
                              CLIENT_BUFFER)

    # Comfortably more location frames than the client buffer can hold, which on a
    # real ride is about forty seconds of ordinary driving.
    await _send_gps(dws, CLIENT_BUFFER * 3)
    await asyncio.sleep(3)          # let the fan-out fill the buffer

    await tws.send_to(text_data=json.dumps({'action': 'complete'}))
    await asyncio.sleep(8)          # far longer than a healthy completion needs

    final_status = await asyncio.to_thread(_trip_status, trip.id)
    queued_for_client = tws.output_queue.qsize()
    print(f'bounded client buffer={CLIENT_BUFFER}: final={final_status} '
          f'client_queue={queued_for_client}')

    await _shutdown(dws, tws)

    assert final_status == 'completed', (
        f'REPRODUCED: trip stayed {final_status!r}. The client buffer held '
        f'{queued_for_client} undelivered location broadcasts, so '
        "TripStatusConsumer's dispatch loop was blocked in send() and never "
        'routed the `complete` command. Location traffic and lifecycle commands '
        'must not share one dispatch loop with blocking sends.'
    )


async def test_a_drained_client_completes_at_the_same_frame_count():
    """The control that makes the test above mean something.

    Same frame count, same bounded buffer, only difference: someone is reading.
    If this passes while the test above fails, the cause is client drain rate --
    not frame count, not elapsed time, not the database.
    """
    rider = await asyncio.to_thread(_make_rider, 901)
    driver = await asyncio.to_thread(_make_driver, 30901)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dws = await _open_driver_socket(driver.user_id)
    tws = _bound_client_buffer(await _open_trip_socket(driver.user_id, trip.id),
                              CLIENT_BUFFER)

    stop = asyncio.Event()

    async def reader():
        while not stop.is_set():
            _pending(tws)
            await asyncio.sleep(0.01)

    task = asyncio.create_task(reader())
    try:
        await _send_gps(dws, CLIENT_BUFFER * 3)
        await asyncio.sleep(3)
        await tws.send_to(text_data=json.dumps({'action': 'complete'}))

        completed = False
        deadline = time.monotonic() + LIFECYCLE_BUDGET_SECONDS
        while time.monotonic() < deadline:
            if await asyncio.to_thread(_trip_status, trip.id) == 'completed':
                completed = True
                break
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        await task

    final_status = await asyncio.to_thread(_trip_status, trip.id)
    print(f'drained client, same {CLIENT_BUFFER * 3} frames: final={final_status}')

    await _shutdown(dws, tws)

    assert completed, (
        f'the control failed too (status {final_status!r}), so the reproduction '
        'above is not isolating client drain rate -- fix the harness first'
    )


# ---------------------------------------------------------------------------
# Regression: every lifecycle command must survive a long ride's worth of GPS
# ---------------------------------------------------------------------------

LONG_RIDE_FRAMES = 500          # about twenty minutes of ordinary 2.5s sampling


def _payment_rows(trip_id):
    from servers.payments.models import Payment

    return list(Payment.objects.filter(trip_id_id=trip_id)
                .values_list('id', 'amount', 'status'))


def _txn_rows(trip_id):
    from servers.payments.models import TransactionHistory

    return TransactionHistory.objects.filter(trip_id_id=trip_id).count()


def _wallet_rows(trip_id):
    from servers.rider.models import WalletTransaction

    # The ledger is idempotent on a TRIP_<id>_EARNING reference; counting rows
    # that carry it is how a duplicate credit would show up.
    return WalletTransaction.objects.filter(
        reference_id__icontains=f'TRIP_{trip_id}_').count()


def _receipt_rows(trip_id):
    from servers.ride.models import Receipt

    return Receipt.objects.filter(trip_id_id=trip_id).count()


def _trip_money(trip_id):
    """Everything completion is allowed to touch, as one comparable snapshot."""
    return {
        'status': _trip_status(trip_id),
        'payments': _payment_rows(trip_id),
        'transaction_history': _txn_rows(trip_id),
        'wallet': _wallet_rows(trip_id),
        'receipts': _receipt_rows(trip_id),
        'final_fare': Trip.objects.filter(id=trip_id)
                      .values_list('final_fare', flat=True).first(),
        'completed_at': Trip.objects.filter(id=trip_id)
                        .values_list('completed_at', flat=True).first(),
    }


async def _long_ride(idx, action, otp=None):
    """500 location frames into an UNDRAINED bounded client, then one command.

    Undrained on purpose: that is the condition under which the command used to be
    lost, and the condition a backgrounded mobile app is actually in.
    """
    rider = await asyncio.to_thread(_make_rider, 700 + idx)
    driver = await asyncio.to_thread(_make_driver, 40000 + idx)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dws = await _open_driver_socket(driver.user_id)
    tws = _bound_client_buffer(await _open_trip_socket(driver.user_id, trip.id),
                               CLIENT_BUFFER)

    await _send_gps(dws, LONG_RIDE_FRAMES)
    await asyncio.sleep(6)          # let the fan-out fill and stay full

    payload = {'action': action}
    if otp is not None:
        payload['otp'] = otp
    t0 = time.monotonic()
    await tws.send_to(text_data=json.dumps(payload))

    landed = None
    expected = {'complete': 'completed', 'cancel': 'cancelled'}[action]
    deadline = time.monotonic() + LIFECYCLE_BUDGET_SECONDS
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_trip_status, trip.id) == expected:
            landed = time.monotonic() - t0
            break
        await asyncio.sleep(0.1)

    return trip, dws, tws, landed


async def test_500_frames_then_complete_lands_exactly_once():
    """The headline regression, at a realistic long-ride frame count."""
    trip, dws, tws, landed = await _long_ride(0, 'complete')
    money = await asyncio.to_thread(_trip_money, trip.id)
    print(f'500 frames -> complete: landed={landed} money={money}')
    await _shutdown(dws, tws)

    assert landed is not None, (
        f'`complete` did not land after {LONG_RIDE_FRAMES} location frames into an '
        'undrained client'
    )
    assert money['status'] == 'completed'
    assert len(money['payments']) == 1, (
        f'completion must create exactly one Payment, got {money["payments"]}')
    assert money['final_fare'] is None, 'completion must not write final_fare'


async def test_500_frames_then_cancel_lands():
    """Cancellation is the command a rider reaches for when something is wrong.

    It must not be the command that sustained GPS traffic makes unavailable.
    """
    trip, dws, tws, landed = await _long_ride(1, 'cancel')
    status = await asyncio.to_thread(_trip_status, trip.id)
    print(f'500 frames -> cancel: landed={landed} status={status}')
    await _shutdown(dws, tws)

    assert landed is not None, (
        f'`cancel` did not land after {LONG_RIDE_FRAMES} location frames')


async def test_500_frames_then_sos_over_http():
    """SOS does not traverse the consumer, and this records that it does not.

    `raise_sos` is an HTTP endpoint, so the dispatch-loop starvation never applied
    to it. It still shares the process and the single thread that
    `database_sync_to_async` falls back to, so it is worth proving that a flood of
    location frames does not delay it either.
    """
    from rest_framework.test import APIClient

    rider = await asyncio.to_thread(_make_rider, 702)
    driver = await asyncio.to_thread(_make_driver, 40002)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dws = await _open_driver_socket(driver.user_id)
    tws = _bound_client_buffer(await _open_trip_socket(driver.user_id, trip.id),
                               CLIENT_BUFFER)
    await _send_gps(dws, LONG_RIDE_FRAMES)
    await asyncio.sleep(6)

    def raise_sos():
        api = APIClient()
        api.force_authenticate(user=rider)
        t0 = time.monotonic()
        resp = api.post('/api/v1/sos/', {'trip_id': trip.id, 'event_type': 'panic',
                                         'raised_for': 'rider'}, format='json')
        return resp.status_code, time.monotonic() - t0

    code, latency = await asyncio.to_thread(raise_sos)
    print(f'500 frames -> SOS over HTTP: status={code} latency={latency:.2f}s')
    await _shutdown(dws, tws)

    assert code < 500, f'SOS failed under sustained GPS load: HTTP {code}'
    assert latency < LIFECYCLE_BUDGET_SECONDS, (
        f'SOS took {latency:.1f}s under sustained GPS load')


async def test_interleaved_location_and_lifecycle_frames_keep_their_order():
    """GPS, GPS, complete, GPS -- with the ordering contract made explicit.

    After the fix, status frames are never delayed behind location frames, so the
    determined expectation is: `complete` is processed promptly, and the location
    frames that follow it are simply refused collection because the trip is
    terminal. Location frames may be coalesced away; the status frame may not.
    """
    rider = await asyncio.to_thread(_make_rider, 703)
    driver = await asyncio.to_thread(_make_driver, 40003)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dws = await _open_driver_socket(driver.user_id)
    tws = _bound_client_buffer(await _open_trip_socket(driver.user_id, trip.id),
                               CLIENT_BUFFER)

    # Fill the client buffer first, so `complete` is genuinely interleaved with a
    # backlog rather than arriving on a quiet socket.
    await _send_gps(dws, 40)
    await asyncio.sleep(2)

    await dws.send_to(text_data=json.dumps({'lat': LAT, 'lng': LNG}))
    await dws.send_to(text_data=json.dumps({'lat': LAT + STEP, 'lng': LNG}))
    await tws.send_to(text_data=json.dumps({'action': 'complete'}))
    await dws.send_to(text_data=json.dumps({'lat': LAT + 2 * STEP, 'lng': LNG}))

    completed = False
    deadline = time.monotonic() + LIFECYCLE_BUDGET_SECONDS
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_trip_status, trip.id) == 'completed':
            completed = True
            break
        await asyncio.sleep(0.1)

    # Frames sent after completion must not resurrect the trip.
    await dws.send_to(text_data=json.dumps({'lat': LAT + 3 * STEP, 'lng': LNG}))
    await asyncio.sleep(1)
    final_status = await asyncio.to_thread(_trip_status, trip.id)
    print(f'interleaved GPS/GPS/complete/GPS: completed={completed} '
          f'final={final_status}')

    await _shutdown(dws, tws)

    assert completed, 'interleaved `complete` was not processed'
    assert final_status == 'completed', (
        f'a location frame after completion changed the trip to {final_status!r}')


# ---------------------------------------------------------------------------
# Financial safety: a retried completion must stay idempotent
# ---------------------------------------------------------------------------

async def test_repeated_complete_cannot_duplicate_money():
    """A client that retries `complete` must not be billed or paid twice.

    This is the risk the fix creates the *opportunity* for: commands that used to
    be silently swallowed now all arrive. A driver app that retried a completion it
    thought had failed will now land every attempt.
    """
    rider = await asyncio.to_thread(_make_rider, 704)
    driver = await asyncio.to_thread(_make_driver, 40004)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dws = await _open_driver_socket(driver.user_id)
    tws = await _open_trip_socket(driver.user_id, trip.id)

    await _send_gps(dws, 30)
    await tws.send_to(text_data=json.dumps({'action': 'complete'}))

    completed = False
    deadline = time.monotonic() + LIFECYCLE_BUDGET_SECONDS
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_trip_status, trip.id) == 'completed':
            completed = True
            break
        await asyncio.sleep(0.1)
    assert completed, 'the first completion must land before retries are meaningful'

    after_first = await asyncio.to_thread(_trip_money, trip.id)

    # Five more attempts, as a retrying client would send them.
    for _ in range(5):
        await tws.send_to(text_data=json.dumps({'action': 'complete'}))
    await asyncio.sleep(4)

    after_retries = await asyncio.to_thread(_trip_money, trip.id)
    print('money after 1 complete: ', after_first)
    print('money after 6 completes:', after_retries)

    await _shutdown(dws, tws)

    assert after_retries == after_first, (
        'a repeated completion changed money or state. Differences: '
        + repr({k: (after_first[k], after_retries[k])
                for k in after_first if after_first[k] != after_retries[k]})
    )
    assert len(after_first['payments']) == 1, (
        f'expected exactly one Payment, got {after_first["payments"]}')
    assert after_retries['final_fare'] is None, 'final_fare must stay untouched'


# ---------------------------------------------------------------------------
# Cost per GPS frame, and what an undrained client does to ingestion
# ---------------------------------------------------------------------------

def _counters():
    """Real server-side counters, so the cost numbers are not estimates.

    Redis commands come from the server's own `total_commands_processed` (one
    counter for the whole instance, so it includes the channel layer), PostgreSQL
    transactions from `pg_stat_database`, and Celery enqueues from the broker list.
    """
    import redis
    from django.conf import settings
    from django.db import connection

    from servers.redis_client import LOCATION_STREAM

    url = settings.REDIS_URL if hasattr(settings, 'REDIS_URL') else None
    r0 = redis.Redis.from_url((url or 'redis://localhost:6379') + '/0')
    r3 = redis.Redis.from_url((url or 'redis://localhost:6379') + '/3')

    with connection.cursor() as cur:
        cur.execute('SELECT xact_commit FROM pg_stat_database WHERE datname = '
                    'current_database()')
        xact = cur.fetchone()[0]

    return {
        'redis_commands': int(r0.info('stats')['total_commands_processed']),
        'gps_stream_len': int(r3.xlen(LOCATION_STREAM) or 0),
        'celery_queued': int(r0.llen('celery') or 0),
        'pg_xact': int(xact),
    }


async def test_per_frame_cost_and_ingestion_under_an_undrained_client():
    """Reports the per-frame cost, and proves GPS ingestion no longer stops.

    The second half is the part that matters beyond latency. Before the fix, the
    driver's per-frame `location_updated` acknowledgement was a blocking send, so a
    driver app that never read it stopped being able to deliver GPS at all once its
    buffer filled -- silently ending the trip's distance evidence mid-ride. Nothing
    reported an error: `receive` was simply never reached again.
    """
    frames = 200

    rider = await asyncio.to_thread(_make_rider, 800)
    driver = await asyncio.to_thread(_make_driver, 50000)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    # Both sockets bounded and UNDRAINED: a backgrounded app, in other words.
    dws = _bound_client_buffer(await _open_driver_socket(driver.user_id),
                               CLIENT_BUFFER)
    tws = _bound_client_buffer(await _open_trip_socket(driver.user_id, trip.id),
                               CLIENT_BUFFER)

    before = await asyncio.to_thread(_counters)
    t0 = time.monotonic()
    await _send_gps(dws, frames)

    # Wait until ingestion goes quiet rather than for a fixed time, so a slow
    # machine does not read as a stalled one.
    last = -1
    quiet_since = None
    while time.monotonic() - t0 < 60:
        now = (await asyncio.to_thread(_counters))['gps_stream_len']
        if now == last:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since > 3:
                break
        else:
            last, quiet_since = now, None
        await asyncio.sleep(0.25)
    elapsed = time.monotonic() - t0
    after = await asyncio.to_thread(_counters)

    ingested = after['gps_stream_len'] - before['gps_stream_len']
    report = {
        'frames_sent': frames,
        'frames_ingested': ingested,
        'ingestion_ratio': round(ingested / frames, 3),
        'redis_cmds_per_frame': round(
            (after['redis_commands'] - before['redis_commands']) / max(ingested, 1), 2),
        'pg_xact_per_frame': round(
            (after['pg_xact'] - before['pg_xact']) / max(ingested, 1), 3),
        'celery_enqueues_per_frame': round(
            (after['celery_queued'] - before['celery_queued']) / max(ingested, 1), 3),
        'mean_frame_latency_ms': round(1000 * elapsed / max(ingested, 1), 1),
    }
    print('PER-FRAME COST:', report)

    await _shutdown(dws, tws)

    assert ingested >= frames * 0.95, (
        f'only {ingested} of {frames} location frames reached the GPS stream '
        'against a client that does not drain its socket. Ingestion is still '
        'coupled to client drain rate, so a backgrounded driver app silently '
        "stops producing the trip's distance evidence."
    )


# ---------------------------------------------------------------------------
# The driver must not receive an echo of its own GPS on its command socket
# ---------------------------------------------------------------------------

async def test_the_driver_does_not_receive_its_own_location_echo():
    """The fix for the QA failure, pinned as a test.

    `group_send('trip_<id>', ...)` reaches the whole trip group, so the assigned
    driver used to receive one copy of every position it had just sent, on the same
    socket that carries `complete`. That echo is what filled a slow client's receive
    buffer; once full, the reference `websockets` client stops reading frames at all
    -- including Daphne's keepalive pings -- so it stops answering pongs, Daphne's
    ping timeout elapses, and the server closes the socket. The app never notices,
    because its own `send()` still succeeds.

    The rider still needs the position. The driver never did.
    """
    rider = await asyncio.to_thread(_make_rider, 910)
    driver = await asyncio.to_thread(_make_driver, 30910)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dws = await _open_driver_socket(driver.user_id)
    driver_tws = await _open_trip_socket(driver.user_id, trip.id)
    rider_tws = await _open_trip_socket(rider, trip.id)

    await _send_gps(dws, 12)
    await asyncio.sleep(4)

    def locations(comm):
        out = []
        for raw in _pending(comm):
            if raw.get('type') != 'websocket.send':
                continue
            try:
                msg = json.loads(raw.get('text') or '{}')
            except ValueError:
                continue
            if msg.get('type') == 'driver_location_update':
                out.append(msg)
        return out

    to_driver = locations(driver_tws)
    to_rider = locations(rider_tws)
    print(f'location frames delivered -- driver:{len(to_driver)} rider:{len(to_rider)}')

    await _shutdown(dws, driver_tws, rider_tws)

    assert to_driver == [], (
        f'the assigned driver received {len(to_driver)} echoes of its own position '
        'on its command socket. That traffic is what kills the socket that carries '
        '`complete`.'
    )
    assert to_rider, (
        'the rider received no driver location at all -- the fix must not stop the '
        'rider seeing the car move'
    )


async def test_a_long_quiet_command_socket_still_completes():
    """A command socket that carries no traffic for minutes must still work.

    The local analogue of the QA 300-second journey. There is no proxy and no ping
    timeout here, so this cannot reproduce the QA close; what it does assert is that
    nothing in the consumer degrades over a long, quiet, low-rate ride -- and that
    the driver's socket stays clean of location traffic throughout, which is the
    property that makes the QA failure impossible.
    """
    rider = await asyncio.to_thread(_make_rider, 911)
    driver = await asyncio.to_thread(_make_driver, 30911)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dws = await _open_driver_socket(driver.user_id)
    tws = _bound_client_buffer(await _open_trip_socket(driver.user_id, trip.id),
                               CLIENT_BUFFER)

    # 30 pings at a realistic rate, compressed in time but not in count: the point
    # is that the command socket accumulates nothing across them.
    for i in range(30):
        await dws.send_to(text_data=json.dumps({'lat': LAT + STEP * i, 'lng': LNG}))
        await asyncio.sleep(0.1)
    await asyncio.sleep(2)

    queued_for_driver = tws.output_queue.qsize()

    await tws.send_to(text_data=json.dumps({'action': 'complete'}))
    completed = False
    deadline = time.monotonic() + LIFECYCLE_BUDGET_SECONDS
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_trip_status, trip.id) == 'completed':
            completed = True
            break
        await asyncio.sleep(0.1)
    print(f'30 pings, driver command socket queue={queued_for_driver}, '
          f'completed={completed}')

    await _shutdown(dws, tws)

    assert queued_for_driver == 0, (
        f'{queued_for_driver} frames were waiting on the driver command socket '
        'after 30 pings. A buffer that fills is a socket the server will close.'
    )
    assert completed


# ---------------------------------------------------------------------------
# Keepalive, and the signal whose absence made this take a day
# ---------------------------------------------------------------------------

async def test_ping_gets_a_pong_and_not_an_error():
    """The driver app pings every 25 seconds to keep this socket warm.

    It used to send an unknown action and use the `Invalid action` error frame as
    its liveness proof. That worked, but meant the socket carrying `complete` also
    carried an error frame every 25 seconds, indistinguishable from a real command
    failure by any client that surfaces errors to the user.
    """
    rider = await asyncio.to_thread(_make_rider, 920)
    driver = await asyncio.to_thread(_make_driver, 30920)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    tws = await _open_trip_socket(driver.user_id, trip.id)
    await tws.send_to(text_data=json.dumps({'action': 'ping'}))

    reply, _, _ = await _await_frame(tws, lambda m: m.get('type') in ('pong', 'error'),
                                     5)
    print(f'ping -> {reply}')
    await _shutdown(tws)

    assert reply is not None, 'ping got no reply at all'
    assert reply.get('type') == 'pong', (
        f'ping must be answered with a pong, got {reply!r}')


async def test_an_unknown_action_is_still_refused():
    """Adding `ping` must not open the action list up."""
    rider = await asyncio.to_thread(_make_rider, 921)
    driver = await asyncio.to_thread(_make_driver, 30921)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    tws = await _open_trip_socket(driver.user_id, trip.id)
    await tws.send_to(text_data=json.dumps({'action': 'teleport'}))

    reply, _, _ = await _await_frame(tws, lambda m: m.get('type') == 'error', 5)
    await _shutdown(tws)

    assert reply is not None, 'an unknown action must still be refused'
    assert 'Invalid action' in str(reply.get('message'))


async def test_losing_the_command_socket_mid_ride_is_logged(caplog):
    """The missing signal, now asserted.

    Nothing in the platform said anything when a driver lost the socket it issues
    `complete` on. That silence is why a completed-looking ride sat `in_progress`
    and why the diagnosis took a day rather than minutes. WARNING level, so it
    survives production's log configuration.
    """
    import logging

    rider = await asyncio.to_thread(_make_rider, 922)
    driver = await asyncio.to_thread(_make_driver, 30922)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    tws = await _open_trip_socket(driver.user_id, trip.id)
    with caplog.at_level(logging.WARNING, logger='servers.consumers'):
        await tws.disconnect(timeout=5)
        await asyncio.sleep(0.5)

    records = [r for r in caplog.records if r.msg == 'trip_command_socket_lost']
    print(f'mid-ride disconnect -> {len(records)} warning(s)')

    assert records, (
        'a trip socket vanishing while the ride is still in progress must be '
        'logged -- this is the signal whose absence hid the original defect'
    )
    logged = records[0].__dict__
    assert logged['trip_id'] == str(trip.id) or logged['trip_id'] == trip.id
    assert logged['trip_status'] == 'in_progress'
    assert logged['participation'] == 'assigned_driver'


async def test_a_normal_completed_ride_logs_no_lost_socket_warning(caplog):
    """A warning that fires on every healthy ride is noise, and would be ignored."""
    import logging

    rider = await asyncio.to_thread(_make_rider, 923)
    driver = await asyncio.to_thread(_make_driver, 30923)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    tws = await _open_trip_socket(driver.user_id, trip.id)
    await tws.send_to(text_data=json.dumps({'action': 'complete'}))

    deadline = time.monotonic() + LIFECYCLE_BUDGET_SECONDS
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_trip_status, trip.id) == 'completed':
            break
        await asyncio.sleep(0.1)

    with caplog.at_level(logging.WARNING, logger='servers.consumers'):
        await tws.disconnect(timeout=5)
        await asyncio.sleep(0.5)

    records = [r for r in caplog.records if r.msg == 'trip_command_socket_lost']
    print(f'completed ride disconnect -> {len(records)} warning(s)')

    assert not records, (
        'the ride completed normally, so closing its socket is not a lost command '
        'channel and must not warn'
    )
