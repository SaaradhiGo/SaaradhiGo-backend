"""A retried booking must return the original trip, not create a second one.

A rider who double-taps Book, or whose app retries after a timeout or a reconnect,
used to get two trips. That is not a cosmetic duplicate: each trip starts its own
dispatch chain competing for the same drivers, registers its own surge demand,
and carries its own auto-cancel deadline.

The mechanism is a client-generated `client_request_id` with a **partial unique
index in PostgreSQL**, scoped per rider. PostgreSQL is the arbiter deliberately:
a Redis check can be lost or raced, and the concurrency test below is the case
that distinguishes the two.

`test_without_a_key_two_identical_requests_still_create_two_trips` is the negative
control -- it pins the old behaviour, so these tests demonstrate a real change
rather than only passing.
"""

import asyncio
import json
import uuid
from decimal import Decimal

import pytest
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model

from base.asgi import application
from servers.ride.models import Trip

User = get_user_model()

# Inside the Hyderabad service polygon, and far enough apart to pass distance
# validation.
P_LAT, P_LNG = 17.4450000, 78.3800000
D_LAT, D_LNG = 17.4550000, 78.3800000

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio,
              pytest.mark.django_db(transaction=True)]


def _make_rider(suffix):
    return User.objects.create_user(phone_number=f'+9196000{suffix:05d}', role='rider')


def _token(user):
    from rest_framework_simplejwt.tokens import AccessToken

    return str(AccessToken.for_user(user))


async def _open_rider_socket(rider):
    tok = await asyncio.to_thread(_token, rider)
    comm = WebsocketCommunicator(application, f'/ws/ride/request/?token={tok}')
    connected, _ = await comm.connect(timeout=20)
    assert connected, 'rider socket must connect'
    await comm.receive_from(timeout=10)      # connection_established
    return comm


def _pending(comm):
    """Read what is queued without cancelling the application.

    `receive_from(timeout=...)` cancels the app task on timeout and raises
    `CancelledError`, which is a BaseException and therefore escapes
    `except Exception`. Not repeating that here.
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


def _booking(client_request_id=None):
    payload = {
        'pickup_lat': P_LAT, 'pickup_lng': P_LNG,
        'destination_lat': D_LAT, 'destination_lng': D_LNG,
        'pickup_address': 'Idempotency test pickup',
        'destination_address': 'Idempotency test drop',
        'distance_km': 1.2, 'duration_min': 4,
        'vehicle_type': 'sedan', 'payment_method': 'cash',
    }
    if client_request_id is not None:
        payload['client_request_id'] = client_request_id
    return json.dumps(payload)


async def _book(comm, client_request_id=None, seconds=25):
    """Send one booking and wait for its `trip_created`."""
    await comm.send_to(text_data=_booking(client_request_id))
    deadline = asyncio.get_running_loop().time() + seconds
    others = []
    while asyncio.get_running_loop().time() < deadline:
        for msg in _pending(comm):
            if msg.get('type') == 'trip_created':
                return msg, others
            others.append(msg)
        await asyncio.sleep(0.05)
    return None, others


def _trip_count(rider_id):
    return Trip.objects.filter(user_id_id=rider_id).count()


def _trips(rider_id):
    return list(Trip.objects.filter(user_id_id=rider_id)
                .order_by('id').values_list('id', 'client_request_id'))


async def _shutdown(*comms):
    for c in comms:
        try:
            await c.disconnect(timeout=5)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# The negative control: the old behaviour, pinned
# ---------------------------------------------------------------------------

async def test_without_a_key_two_identical_requests_still_create_two_trips():
    """Old clients are unchanged -- and this is the defect, demonstrated.

    A client that sends no `client_request_id` behaves exactly as before: two
    bookings, two trips. That is what backward compatibility means here, and it is
    also the measurement that makes the next test meaningful.
    """
    rider = await asyncio.to_thread(_make_rider, 1)
    comm = await _open_rider_socket(rider)

    first, _ = await _book(comm)
    second, _ = await _book(comm)
    trips = await asyncio.to_thread(_trips, rider.id)
    print(f'no key: first={first and first.get("trip_id")} '
          f'second={second and second.get("trip_id")} trips={trips}')
    await _shutdown(comm)

    assert first is not None and second is not None, 'both bookings must be answered'
    assert first['trip_id'] != second['trip_id'], (
        'without an idempotency key the server cannot tell a retry from a new '
        'booking -- if this ever passes, the compatibility story has changed'
    )
    assert len(trips) == 2


# ---------------------------------------------------------------------------
# With a key
# ---------------------------------------------------------------------------

async def test_the_same_key_returns_the_same_trip():
    """Request #1 creates trip X; the retry returns trip X, not trip Y."""
    rider = await asyncio.to_thread(_make_rider, 2)
    comm = await _open_rider_socket(rider)
    key = str(uuid.uuid4())

    first, _ = await _book(comm, key)
    second, _ = await _book(comm, key)
    trips = await asyncio.to_thread(_trips, rider.id)
    print(f'same key: first={first} second={second} trips={trips}')
    await _shutdown(comm)

    assert first is not None and second is not None
    assert first['trip_id'] == second['trip_id'], (
        f'a retried booking created a second trip: {first["trip_id"]} then '
        f'{second["trip_id"]}'
    )
    assert len(trips) == 1, f'exactly one trip must exist, got {trips}'
    assert first.get('reused') is False
    assert second.get('reused') is True, (
        'the retry should be marked reused so the app knows it was recognised')


async def test_different_keys_create_different_trips():
    """The key must not collapse genuinely separate bookings."""
    rider = await asyncio.to_thread(_make_rider, 3)
    comm = await _open_rider_socket(rider)

    first, _ = await _book(comm, str(uuid.uuid4()))
    second, _ = await _book(comm, str(uuid.uuid4()))
    await _shutdown(comm)

    assert first['trip_id'] != second['trip_id']
    assert await asyncio.to_thread(_trip_count, rider.id) == 2


async def test_the_key_is_scoped_to_the_rider():
    """Two riders using the same id must not collide.

    Client-generated ids are not globally coordinated, so a global unique index
    would let one rider's booking block another's.
    """
    rider_a = await asyncio.to_thread(_make_rider, 4)
    rider_b = await asyncio.to_thread(_make_rider, 5)
    shared = 'collision-' + uuid.uuid4().hex[:8]

    comm_a = await _open_rider_socket(rider_a)
    comm_b = await _open_rider_socket(rider_b)
    a, _ = await _book(comm_a, shared)
    b, _ = await _book(comm_b, shared)
    print(f'scoped key: rider_a trip={a and a.get("trip_id")} '
          f'rider_b trip={b and b.get("trip_id")}')
    await _shutdown(comm_a, comm_b)

    assert a is not None, 'rider A must be able to book'
    assert b is not None, (
        "rider B's booking was refused because rider A used the same client id -- "
        'the constraint is not scoped per rider'
    )
    assert a['trip_id'] != b['trip_id']


def _ride_request_stream_len():
    """Length of the `ride_requests` Redis stream.

    A direct, countable side effect of booking, unlike watching for
    `dispatch_progress` frames -- which never appear when no driver is online and
    would make this assertion pass for the wrong reason.
    """
    import redis
    from django.conf import settings

    url = getattr(settings, 'REDIS_URL', None) or 'redis://localhost:6379'
    client = redis.Redis.from_url(url + '/2')
    try:
        return int(client.xlen('ride_requests') or 0)
    except Exception:  # noqa: BLE001 -- absent stream counts as zero
        return 0


async def test_a_retry_performs_none_of_the_side_effects_of_booking():
    """The duplicate that actually hurts.

    Two trips is the visible symptom. Two dispatch chains competing for the same
    drivers, a second surge-demand record and a second ride-request stream event
    are the damage. Measured on the stream, which is countable.
    """
    rider = await asyncio.to_thread(_make_rider, 6)
    comm = await _open_rider_socket(rider)
    key = str(uuid.uuid4())

    before = await asyncio.to_thread(_ride_request_stream_len)
    first, _ = await _book(comm, key)
    await asyncio.sleep(1.5)
    after_first = await asyncio.to_thread(_ride_request_stream_len)

    second, _ = await _book(comm, key)
    await asyncio.sleep(1.5)
    after_retry = await asyncio.to_thread(_ride_request_stream_len)

    print(f'ride_requests stream: before={before} after_first={after_first} '
          f'after_retry={after_retry}')
    await _shutdown(comm)

    assert second['trip_id'] == first['trip_id']
    assert after_first == before + 1, (
        'the first booking should publish exactly one ride-request event')
    assert after_retry == after_first, (
        f'the retry published another ride-request event ({after_first} -> '
        f'{after_retry}), so it re-ran the side effects of booking'
    )


async def test_an_empty_or_whitespace_key_is_treated_as_absent():
    """A blank key must not become a shared idempotency key for every booking."""
    rider = await asyncio.to_thread(_make_rider, 7)
    comm = await _open_rider_socket(rider)

    first, _ = await _book(comm, '   ')
    second, _ = await _book(comm, '')
    trips = await asyncio.to_thread(_trips, rider.id)
    print(f'blank keys: trips={trips}')
    await _shutdown(comm)

    assert first['trip_id'] != second['trip_id'], (
        'a blank key collapsed two distinct bookings into one')
    assert all(key is None for _, key in trips), (
        f'a blank key must be stored as NULL, not as an empty string: {trips}')


async def test_an_over_long_key_is_truncated_rather_than_rejected():
    """The field is CharField(64); a longer key must not 500 the socket."""
    rider = await asyncio.to_thread(_make_rider, 8)
    comm = await _open_rider_socket(rider)

    long_key = 'x' * 300
    first, _ = await _book(comm, long_key)
    second, _ = await _book(comm, long_key)
    trips = await asyncio.to_thread(_trips, rider.id)
    await _shutdown(comm)

    assert first is not None, 'an over-long key must not break the booking'
    assert first['trip_id'] == second['trip_id'], (
        'truncation must be consistent, or a retry stops being recognised')
    assert len(trips) == 1
    assert len(trips[0][1]) <= 64


# ---------------------------------------------------------------------------
# Concurrency, against real PostgreSQL
# ---------------------------------------------------------------------------

async def test_two_simultaneous_identical_bookings_produce_one_trip():
    """The race a Redis check cannot win.

    Both requests check for an existing trip, both find none, both insert. The
    partial unique index refuses the second, and the loser re-reads the winner's
    row instead of guessing. This needs real PostgreSQL: SQLite would not exercise
    the same constraint behaviour under concurrent writes.
    """
    rider = await asyncio.to_thread(_make_rider, 9)
    key = str(uuid.uuid4())

    # Two independent sockets, as two retries from a reconnecting app would be.
    comm_a = await _open_rider_socket(rider)
    comm_b = await _open_rider_socket(rider)

    await comm_a.send_to(text_data=_booking(key))
    await comm_b.send_to(text_data=_booking(key))

    created = []
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline and len(created) < 2:
        for comm in (comm_a, comm_b):
            for msg in _pending(comm):
                if msg.get('type') == 'trip_created':
                    created.append(msg)
        await asyncio.sleep(0.05)

    trips = await asyncio.to_thread(_trips, rider.id)
    print(f'concurrent: created={[(m["trip_id"], m.get("reused")) for m in created]} '
          f'trips={trips}')
    await _shutdown(comm_a, comm_b)

    assert len(trips) == 1, (
        f'two simultaneous identical bookings created {len(trips)} trips: {trips}'
    )
    assert created, 'at least one booking must have been answered'
    assert {m['trip_id'] for m in created} == {trips[0][0]}, (
        'every answer must point at the single trip that exists')


def test_the_database_itself_refuses_a_duplicate_key(django_db_setup,
                                                     django_db_blocker):
    """The constraint is real, not just application logic.

    Asserted at the database level, because an application-level check is exactly
    what gets bypassed by a management command, a data migration or a second
    process.
    """
    from django.db import IntegrityError, transaction

    with django_db_blocker.unblock():
        rider = _make_rider(10)
        key = 'db-level-' + uuid.uuid4().hex[:8]

        def make():
            return Trip.objects.create(
                user_id=rider, client_request_id=key,
                pickup_lat=Decimal(str(P_LAT)), pickup_long=Decimal(str(P_LNG)),
                destination_lat=Decimal(str(D_LAT)),
                destination_long=Decimal(str(D_LNG)),
                estimated_fare=Decimal('100.00'), payment_method='cash',
            )

        first = make()
        assert first.pk is not None

        with pytest.raises(IntegrityError), transaction.atomic():
            make()

        # NULL keys must remain unconstrained, or old clients break.
        with transaction.atomic():
            a = Trip.objects.create(
                user_id=rider, client_request_id=None,
                pickup_lat=Decimal(str(P_LAT)), pickup_long=Decimal(str(P_LNG)),
                destination_lat=Decimal(str(D_LAT)),
                destination_long=Decimal(str(D_LNG)),
                estimated_fare=Decimal('100.00'), payment_method='cash',
            )
            b = Trip.objects.create(
                user_id=rider, client_request_id=None,
                pickup_lat=Decimal(str(P_LAT)), pickup_long=Decimal(str(P_LNG)),
                destination_lat=Decimal(str(D_LAT)),
                destination_long=Decimal(str(D_LNG)),
                estimated_fare=Decimal('100.00'), payment_method='cash',
            )
        assert a.pk != b.pk, 'multiple NULL keys must be allowed'
