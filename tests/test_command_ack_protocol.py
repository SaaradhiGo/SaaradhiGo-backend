"""Lifecycle commands must be acknowledged, and a retry must be safe.

Why this exists
---------------
A successful WebSocket `send()` proves only that bytes left the client. The
resolved pilot blocker turned on exactly that: a driver app reported a finished
ride whose trip was still `in_progress`, because its socket had been closed before
the command arrived and `send()` succeeded anyway.

Fixing the socket was necessary but not sufficient, because the protocol had a
second, independent hole that needs no transport failure at all. Before
`command_ack`, the *only* success signal for every lifecycle command was the
`trip_status_update` broadcast, delivered with `channel_layer.group_send` --
and channels_redis **silently drops** messages to a channel that is over capacity
(100 by default), logging only "N of M channels over capacity in group G". A
committed completion could therefore go unacknowledged by design, and the driver
app, which sets `isBusy` and waits with no timeout, would hang forever.

`test_the_old_success_signal_travels_on_a_droppable_path` is the negative control
for that claim: it demonstrates the drop against the real Redis channel layer.

The three ack outcomes
----------------------
`committed`     this call performed the transition, and it is durable.
`already_done`  the durable state already satisfies the command -- a retry whose
                original attempt succeeded. Nothing ran twice.
`rejected`      genuinely refused; nothing changed.

The trip's own status is the idempotency record for lifecycle commands, so no
command-ID table is needed: the target state *is* the dedup key. `command_id` is
for correlation only. (Trip *creation* is different -- there the row does not yet
exist, so it needs a durable idempotency key of its own.)
"""

import asyncio
import json
import uuid
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _make_driver(suffix):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    u = User.objects.create_user(phone_number=f'+9198100{suffix:05d}', role='driver')
    d = Driver.objects.create(user_id=u, approved=True, status='active')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number=f'TS09AK{suffix:04d}')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


def _make_rider(suffix):
    return User.objects.create_user(phone_number=f'+9197100{suffix:05d}', role='rider')


def _make_trip(rider, driver, status='in_progress'):
    from django.utils import timezone

    t = Trip.objects.create(
        user_id=rider, status_id=_status(status),
        pickup_lat=Decimal(str(LAT)), pickup_long=Decimal(str(LNG)),
        destination_lat=Decimal(str(LAT + 0.01)), destination_long=Decimal(str(LNG)),
        estimated_fare=Decimal('120.00'), payment_method='cash', otp='123456',
    )
    t.driver_id = driver
    now = timezone.now()
    t.accepted_at = now
    if status in ('reached', 'in_progress'):
        t.reached_at = now
    if status == 'in_progress':
        t.started_at = now
    t.save()
    return t


def _trip_status(trip_id):
    return (Trip.objects.filter(id=trip_id)
            .values_list('status_id__status_code', flat=True).first())


def _token(user):
    from rest_framework_simplejwt.tokens import AccessToken

    return str(AccessToken.for_user(user))


async def _open_trip_socket(user, trip_id):
    tok = await asyncio.to_thread(_token, user)
    comm = WebsocketCommunicator(application, f'/ws/ride/trip/{trip_id}/?token={tok}')
    connected, _ = await comm.connect(timeout=20)
    assert connected, 'trip socket must connect'
    # The greeting is always sent, so a plain read cannot time out here.
    greeting = json.loads(await comm.receive_from(timeout=10))
    return comm, greeting


def _pending(comm):
    """Every frame already queued, without cancelling the application.

    Deliberately not `receive_from(timeout=...)`: asgiref's
    `ApplicationCommunicator.receive_output` **cancels the application task** when
    it times out, and the resulting `CancelledError` is a `BaseException`, so it
    sails straight through `except Exception`. Polling a timeout to ask "is there
    anything there?" therefore kills the consumer under test and reports it as a
    product failure. This cost a day during the pilot-blocker investigation; it is
    not repeated here.
    """
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


async def _collect(comm, predicate, seconds=15):
    """Wait for the first frame matching `predicate`, non-destructively.

    Returns (frame_or_None, every_other_frame_seen).
    """
    others = []
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        for msg in _pending(comm):
            if predicate(msg):
                return msg, others
            others.append(msg)
        await asyncio.sleep(0.02)
    return None, others


async def _command(comm, action, timeout=15, **extra):
    """Send one command and return its `command_ack`, correlated by id."""
    cid = str(uuid.uuid4())
    await comm.send_to(text_data=json.dumps(
        {'action': action, 'command_id': cid, **extra}))
    return await _collect(
        comm,
        lambda m: (m.get('type') == 'command_ack'
                   and m.get('command_id') == cid),
        timeout,
    )


async def _shutdown(*comms):
    for c in comms:
        try:
            await c.disconnect(timeout=5)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# The negative control: the signal this protocol replaces really is droppable
# ---------------------------------------------------------------------------

async def test_the_old_success_signal_travels_on_a_droppable_path():
    """`group_send` drops to an over-capacity channel. Demonstrated, not asserted.

    This is why a `command_ack` had to be a direct send. If the only success signal
    is a group broadcast, then a busy or slow client loses its acknowledgement for
    a command that *did* commit -- with no error anywhere, because dropping is the
    documented behaviour of the channel layer, not a fault.
    """
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    group = f'cap_probe_{uuid.uuid4().hex[:8]}'
    channel = await layer.new_channel()
    await layer.group_add(group, channel)

    capacity = getattr(layer, 'capacity', 100)
    # Fill past capacity without ever reading.
    for i in range(capacity + 25):
        await layer.group_send(group, {'type': 'probe', 'i': i})

    received = 0
    while True:
        try:
            await asyncio.wait_for(layer.receive(channel), timeout=0.3)
            received += 1
        except (TimeoutError, asyncio.TimeoutError):
            break

    print(f'group_send: sent {capacity + 25}, delivered {received}, '
          f'capacity {capacity}')
    assert received < capacity + 25, (
        'group_send delivered everything, so this environment does not drop at '
        'capacity and the premise of command_ack needs re-checking here'
    )


# ---------------------------------------------------------------------------
# committed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('start_status,action,expected_status', [
    ('accepted', 'reached', 'reached'),
    ('reached', 'start', 'in_progress'),
    ('in_progress', 'complete', 'completed'),
    ('in_progress', 'cancel', 'cancelled'),
])
async def test_a_command_is_acked_committed_with_the_durable_status(
        start_status, action, expected_status):
    """Every lifecycle command answers directly, and reports the DURABLE status.

    Note `trip_status` is the stored value ('completed'), not the verb the client
    sent ('complete'), so a client can compare an ack against what the API returns
    without translating.
    """
    suffix = abs(hash((start_status, action))) % 9000
    rider = await asyncio.to_thread(_make_rider, suffix)
    driver = await asyncio.to_thread(_make_driver, suffix)
    trip = await asyncio.to_thread(_make_trip, rider, driver, start_status)

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)
    extra = {'otp': '123456'} if action == 'start' else {}
    ack, others = await _command(tws, action, **extra)
    committed = await asyncio.to_thread(_trip_status, trip.id)
    print(f'{start_status} --{action}--> ack={ack} db={committed}')
    await _shutdown(tws)

    assert ack is not None, f'`{action}` produced no command_ack; others={others}'
    assert ack['status'] == 'committed', ack
    assert ack['command'] == action
    assert ack['trip_status'] == expected_status, ack
    # The ack must not be able to claim more than the database holds.
    assert committed == expected_status


async def test_the_ack_only_arrives_after_the_transition_is_durable():
    """The ack must mean committed, not parsed.

    Asserted the only way that is meaningful: by the time the ack is in the
    client's hands, an independent database connection can already see the new
    status. A `to_thread` read uses its own connection, so it cannot see
    uncommitted work.
    """
    rider = await asyncio.to_thread(_make_rider, 9101)
    driver = await asyncio.to_thread(_make_driver, 9101)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)
    ack, _ = await _command(tws, 'complete')
    # Read immediately, with no polling and no sleep.
    seen_right_away = await asyncio.to_thread(_trip_status, trip.id)
    await _shutdown(tws)

    assert ack is not None and ack['status'] == 'committed'
    assert seen_right_away == 'completed', (
        'the ack claimed `committed` but another connection could not yet see the '
        'completion, so the ack is being sent before the transaction commits'
    )


# ---------------------------------------------------------------------------
# already_done -- the retry case, which is the whole point
# ---------------------------------------------------------------------------

async def test_a_retry_after_a_lost_ack_is_told_already_done_not_rejected():
    """The defect this protocol exists to remove.

    A driver whose acknowledgement was lost retries the same command. The durable
    state already satisfies it. Before this, the state machine answered "Invalid
    status transition: cannot change from completed to completed" -- correct in
    that nothing ran twice, and useless to the client, which could not tell it from
    a real failure and would surface an error on a ride that had finished.
    """
    rider = await asyncio.to_thread(_make_rider, 9102)
    driver = await asyncio.to_thread(_make_driver, 9102)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)

    first, _ = await _command(tws, 'complete')
    assert first['status'] == 'committed', first

    retry, _ = await _command(tws, 'complete')
    print(f'first={first["status"]} retry={retry}')
    await _shutdown(tws)

    assert retry is not None, 'a retry must be answered, not ignored'
    assert retry['status'] == 'already_done', (
        f'a retry of a command that already committed must be reported as '
        f'already_done, got {retry!r}'
    )
    assert retry['reason'] == 'already_in_target_state'
    assert retry['trip_status'] == 'completed'


async def test_repeated_retries_never_transition_twice():
    """Idempotency, stated in terms of money and timestamps rather than status."""
    rider = await asyncio.to_thread(_make_rider, 9103)
    driver = await asyncio.to_thread(_make_driver, 9103)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    def money():
        from servers.payments.models import Payment, TransactionHistory
        return {
            'payments': Payment.objects.filter(trip_id_id=trip.id).count(),
            'history': TransactionHistory.objects.filter(trip_id_id=trip.id).count(),
            'completed_at': Trip.objects.filter(id=trip.id)
                            .values_list('completed_at', flat=True).first(),
            'final_fare': Trip.objects.filter(id=trip.id)
                          .values_list('final_fare', flat=True).first(),
        }

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)
    first, _ = await _command(tws, 'complete')
    assert first['status'] == 'committed'
    after_first = await asyncio.to_thread(money)

    acks = [first['status']]
    for _ in range(5):
        ack, _ = await _command(tws, 'complete', timeout=8)
        acks.append(ack['status'] if ack else None)
    after_retries = await asyncio.to_thread(money)

    print(f'acks={acks}')
    print(f'money first={after_first} retries={after_retries}')
    await _shutdown(tws)

    assert acks == ['committed'] + ['already_done'] * 5, acks
    assert after_retries == after_first, 'a retry changed money or timestamps'
    assert after_first['payments'] == 1
    assert after_retries['final_fare'] is None


async def test_a_cancel_retry_is_also_already_done():
    rider = await asyncio.to_thread(_make_rider, 9104)
    driver = await asyncio.to_thread(_make_driver, 9104)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)
    first, _ = await _command(tws, 'cancel', reason='rider changed plans')
    retry, _ = await _command(tws, 'cancel', reason='rider changed plans')
    await _shutdown(tws)

    assert first['status'] == 'committed', first
    assert retry['status'] == 'already_done', retry
    assert retry['trip_status'] == 'cancelled'


# ---------------------------------------------------------------------------
# rejected -- and the distinction from already_done
# ---------------------------------------------------------------------------

async def test_completing_a_cancelled_trip_is_rejected_not_already_done():
    """The two must not be conflated.

    `already_done` means "what you asked for is true". Completing a cancelled trip
    is not true and never will be, so it is a rejection -- and a client that
    retried it would be retrying forever.
    """
    rider = await asyncio.to_thread(_make_rider, 9105)
    driver = await asyncio.to_thread(_make_driver, 9105)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)
    await _command(tws, 'cancel')
    ack, _ = await _command(tws, 'complete')
    print(f'complete-after-cancel ack={ack}')
    await _shutdown(tws)

    assert ack['status'] == 'rejected', ack
    assert ack['reason'] == 'invalid_transition'
    assert ack['trip_status'] == 'cancelled', (
        'a rejection should still tell the client the durable state, so it can '
        'stop guessing'
    )


async def test_a_bad_otp_is_rejected_with_a_reason_code():
    rider = await asyncio.to_thread(_make_rider, 9106)
    driver = await asyncio.to_thread(_make_driver, 9106)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'reached')

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)
    ack, _ = await _command(tws, 'start', otp='000000')
    status = await asyncio.to_thread(_trip_status, trip.id)
    await _shutdown(tws)

    assert ack['status'] == 'rejected', ack
    assert ack['reason'] == 'invalid_otp', ack
    assert status == 'reached', 'a bad OTP must not start the ride'


async def test_an_unauthorised_command_is_acked_rather_than_left_hanging():
    """A client waiting for an ack must not hang on a refusal.

    Otherwise every authorisation failure becomes a retry loop against a command
    that can never succeed.
    """
    rider = await asyncio.to_thread(_make_rider, 9107)
    driver = await asyncio.to_thread(_make_driver, 9107)
    other = await asyncio.to_thread(_make_driver, 9207)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    # The rider may cancel but must not be able to complete.
    tws, _ = await _open_trip_socket(rider, trip.id)
    ack, _ = await _command(tws, 'complete')
    status = await asyncio.to_thread(_trip_status, trip.id)
    await _shutdown(tws)

    assert ack is not None, 'an unauthorised command must still be acknowledged'
    assert ack['status'] == 'rejected', ack
    assert ack['reason'] == 'not_assigned_driver', ack
    assert status == 'in_progress'
    assert other is not None


async def test_an_unknown_command_is_acked_rejected():
    rider = await asyncio.to_thread(_make_rider, 9108)
    driver = await asyncio.to_thread(_make_driver, 9108)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)
    ack, _ = await _command(tws, 'teleport')
    await _shutdown(tws)

    assert ack['status'] == 'rejected', ack
    assert ack['reason'] == 'unknown_command', ack


# ---------------------------------------------------------------------------
# Correlation and backward compatibility
# ---------------------------------------------------------------------------

async def test_acks_are_correlated_so_interleaved_commands_cannot_be_confused():
    """Two commands in flight must be distinguishable by their ids."""
    rider = await asyncio.to_thread(_make_rider, 9109)
    driver = await asyncio.to_thread(_make_driver, 9109)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'reached')

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)

    id_bad = str(uuid.uuid4())
    id_good = str(uuid.uuid4())
    await tws.send_to(text_data=json.dumps(
        {'action': 'start', 'command_id': id_bad, 'otp': '000000'}))
    await tws.send_to(text_data=json.dumps(
        {'action': 'start', 'command_id': id_good, 'otp': '123456'}))

    acks = {}
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline and len(acks) < 2:
        for msg in _pending(tws):
            if msg.get('type') == 'command_ack':
                acks[msg.get('command_id')] = msg
        await asyncio.sleep(0.02)
    print(f'correlated acks: {[(k[:8], v["status"]) for k, v in acks.items()]}')
    await _shutdown(tws)

    assert id_bad in acks and id_good in acks, acks
    assert acks[id_bad]['status'] == 'rejected'
    assert acks[id_bad]['reason'] == 'invalid_otp'
    assert acks[id_good]['status'] == 'committed'


async def test_a_client_that_sends_no_command_id_still_works():
    """Backward compatibility. Old clients must be unaffected.

    They get an ack with no `command_id` (which they ignore, being an unknown
    frame type) and the `trip_status_update` broadcast they already rely on.
    """
    rider = await asyncio.to_thread(_make_rider, 9110)
    driver = await asyncio.to_thread(_make_driver, 9110)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    tws, _ = await _open_trip_socket(driver.user_id, trip.id)
    await tws.send_to(text_data=json.dumps({'action': 'complete'}))

    # Read for a fixed window rather than until a timeout, so nothing cancels
    # the application out from under the assertions below.
    seen = []
    deadline = asyncio.get_running_loop().time() + 8
    while asyncio.get_running_loop().time() < deadline:
        seen.extend(_pending(tws))
        await asyncio.sleep(0.05)
    status = await asyncio.to_thread(_trip_status, trip.id)
    types = [m.get('type') for m in seen]
    print(f'no command_id -> frames={types} db={status}')
    await _shutdown(tws)

    assert status == 'completed', 'the command must work without a command_id'
    assert 'trip_status_update' in types, (
        'the pre-existing broadcast must still be sent, or old clients break')
    ack = next((m for m in seen if m.get('type') == 'command_ack'), None)
    assert ack is not None and 'command_id' not in ack, (
        'the server must not invent a correlation id the client did not choose')


# ---------------------------------------------------------------------------
# Reconnect recovery (Priority 2's foundation)
# ---------------------------------------------------------------------------

async def test_the_greeting_carries_the_durable_trip_status():
    """A reconnecting client must be able to discover truth, not infer it.

    This is what lets a driver that lost its ack decide between "already done" and
    "retry": reconnect, read `trip_status`, and act on durable state.
    """
    rider = await asyncio.to_thread(_make_rider, 9111)
    driver = await asyncio.to_thread(_make_driver, 9111)
    trip = await asyncio.to_thread(_make_trip, rider, driver, 'in_progress')

    tws, greeting = await _open_trip_socket(driver.user_id, trip.id)
    assert greeting['trip_status'] == 'in_progress', greeting

    ack, _ = await _command(tws, 'complete')
    assert ack['status'] == 'committed'
    # Drop the socket as if the ack had been lost in transit.
    await _shutdown(tws)

    again, greeting2 = await _open_trip_socket(driver.user_id, trip.id)
    print(f'greeting after reconnect: {greeting2}')
    await _shutdown(again)

    assert greeting2['trip_status'] == 'completed', (
        'after reconnecting, the client must be told the completion committed -- '
        'otherwise it cannot distinguish a lost ack from a lost command'
    )
