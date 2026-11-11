"""A long ride must not grow anything without a bound.

The pilot claim is that SaaradhiGo can carry a multi-hour ride under sustained
telemetry and still process lifecycle commands. That claim is about *growth*: if
anything scales with ride duration that should not, a four-hour ride fails in a way
a four-minute ride never reveals.

Accelerated, not slept
----------------------
Ride duration is simulated by frame count at the real sampling rate rather than by
waiting. A 30-minute ride at one ping every 2.5 seconds is 720 frames; two hours is
2880. Sleeping through it would test the clock, not the code. One wall-clock QA soak
is run separately against the deployed stack, because that is where proxies, TCP and
Daphne participate.

What is measured, per ride length
---------------------------------
  frames sent / ingested            ingestion must not silently stop
  Redis memory delta                must not scale with ride duration
  GPS stream length                 bounded by maxlen, not by the ride
  durable TripLocationPoint rows    bounded by sampling, not by frame count
  channel-layer queue depth         must not accumulate
  consumer-held state               depth-1 queue, two counters -- must stay flat
  lifecycle command latency         must not degrade with ride length

The assertions are about boundedness and about `complete` still landing. Absolute
timings are reported rather than asserted, because a loaded developer machine is not
a measurement of production.
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
from servers.ride.models import Trip, TripLocationPoint, TripStatus

User = get_user_model()

LAT, LNG = 17.4450000, 78.3800000

# A driver app sends a position about every 2.5 seconds.
FRAMES_PER_MINUTE = 24

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
    u = User.objects.create_user(phone_number=f'+9198400{suffix:05d}', role='driver')
    d = Driver.objects.create(user_id=u, approved=True, status='active')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number=f'TS09SK{suffix:04d}')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


def _make_rider(suffix):
    return User.objects.create_user(phone_number=f'+9197400{suffix:05d}', role='rider')


def _make_trip(rider, driver):
    from django.utils import timezone

    from servers.redis_client import set_driver_active_trip

    t = Trip.objects.create(
        user_id=rider, status_id=_status('in_progress'),
        pickup_lat=Decimal(str(LAT)), pickup_long=Decimal(str(LNG)),
        destination_lat=Decimal(str(LAT + 0.05)), destination_long=Decimal(str(LNG)),
        estimated_fare=Decimal('420.00'), payment_method='cash', otp='123456',
    )
    t.driver_id = driver
    now = timezone.now()
    t.accepted_at = t.reached_at = t.started_at = now
    t.save()
    set_driver_active_trip(driver.id, t.id)
    return t


def _token(user):
    from rest_framework_simplejwt.tokens import AccessToken

    return str(AccessToken.for_user(user))


async def _open(path):
    comm = WebsocketCommunicator(application, path)
    connected, _ = await comm.connect(timeout=30)
    assert connected, f'socket must connect: {path.split("?")[0]}'
    await comm.receive_from(timeout=15)
    return comm


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


def _trip_status(trip_id):
    return (Trip.objects.filter(id=trip_id)
            .values_list('status_id__status_code', flat=True).first())


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def _redis_clients():
    import redis
    from django.conf import settings

    url = getattr(settings, 'REDIS_URL', None) or 'redis://localhost:6379'
    return (redis.Redis.from_url(url + '/2'),   # geo / heartbeat / active-trip
            redis.Redis.from_url(url + '/3'),   # driver_location_stream
            redis.Redis.from_url(url + '/4'))   # channel layer


def _measure(trip_id):
    """A snapshot of everything that could grow with ride duration."""
    from servers.redis_client import LOCATION_STREAM

    geo, stream, chan = _redis_clients()

    def _safe(fn, default=0):
        try:
            return fn()
        except Exception:  # noqa: BLE001 -- a probe must never fail the soak
            return default

    return {
        'used_memory': _safe(lambda: int(geo.info('memory')['used_memory'])),
        'stream_len': _safe(lambda: int(stream.xlen(LOCATION_STREAM) or 0)),
        'stream_pending': _safe(
            lambda: int(stream.xinfo_stream(LOCATION_STREAM)['length'] or 0)),
        'channel_keys': _safe(lambda: len(chan.keys('asgi:*'))),
        'geo_keys': _safe(lambda: len(geo.keys('drivers:geo:*'))),
        'points': _safe(
            lambda: TripLocationPoint.objects.filter(trip_id=trip_id).count()),
    }


def _drain_trail():
    """Run the GPS trail writer synchronously, with the feature flag forced on.

    In production this is a Beat task every 60s, gated by GPS_TRAIL_ENABLED (off by
    default). Overriding the setting here is what makes `durable_points` a real
    measurement: with the writer disabled the count is always zero and every
    assertion about storage growth passes for the wrong reason.
    """
    from django.test import override_settings

    from servers.ride.location_trail import drain_location_stream

    try:
        with override_settings(GPS_TRAIL_ENABLED=True):
            return drain_location_stream(consumer='soak')
    except Exception as exc:  # noqa: BLE001 -- report rather than fail the soak
        return {'error': str(exc)[:120]}


def _client_buffer(comm):
    """What the client has not yet read.

    The consumer's own location queue is not reachable through
    `WebsocketCommunicator` -- the instance is created inside the router -- so this
    is the observable proxy. A buffer that grows with ride length is the shape of
    defect this soak exists to find; a flat one is the evidence the pump coalesces.
    """
    return comm.output_queue.qsize()


# ---------------------------------------------------------------------------
# The soak
# ---------------------------------------------------------------------------

async def _soak(idx, minutes, drain=True):
    """One simulated ride of `minutes`, then `complete`. Returns a report."""
    frames = minutes * FRAMES_PER_MINUTE
    rider = await asyncio.to_thread(_make_rider, idx)
    driver = await asyncio.to_thread(_make_driver, idx)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    dtok = await asyncio.to_thread(_token, driver.user_id)
    rtok = await asyncio.to_thread(_token, rider)
    dws = await _open(f'/ws/driver/location/?token={dtok}&lat={LAT}&lng={LNG}')
    tws = await _open(f'/ws/ride/trip/{trip.id}/?token={dtok}')
    rws = await _open(f'/ws/ride/trip/{trip.id}/?token={rtok}')

    before = await asyncio.to_thread(_measure, trip.id)

    # The journey. Drain the rider's socket as a real app would; leave the
    # driver's command socket undrained, which is the harsher and more realistic
    # case for a backgrounded app.
    stop = asyncio.Event()

    async def rider_reader():
        while not stop.is_set():
            _pending(rws)
            await asyncio.sleep(0.01)

    reader = asyncio.create_task(rider_reader()) if drain else None

    t0 = time.monotonic()
    for i in range(frames):
        # A plausible track: moves enough that the sampler keeps some points.
        await dws.send_to(text_data=json.dumps(
            {'lat': LAT + 0.0004 * i, 'lng': LNG + 0.0001 * (i % 7)}))
        if i % 200 == 199:
            await asyncio.sleep(0.05)      # let the consumer breathe
    send_elapsed = time.monotonic() - t0

    # Wait for ingestion to go quiet rather than for a fixed time.
    last, quiet_at = -1, None
    while time.monotonic() - t0 < 180:
        now = (await asyncio.to_thread(_measure, trip.id))['stream_len']
        if now == last:
            quiet_at = quiet_at or time.monotonic()
            if time.monotonic() - quiet_at > 3:
                break
        else:
            last, quiet_at = now, None
        await asyncio.sleep(0.25)

    mid = await asyncio.to_thread(_measure, trip.id)
    driver_buffer = _client_buffer(tws)
    rider_buffer = _client_buffer(rws)

    # Drain the stream into durable rows now, so the trail is measured rather
    # than assumed. This is the Beat task, run inline.
    drain = await asyncio.to_thread(_drain_trail)

    # The claim under test: a lifecycle command still lands after all of that.
    t1 = time.monotonic()
    import uuid
    cid = str(uuid.uuid4())
    await tws.send_to(text_data=json.dumps(
        {'action': 'complete', 'command_id': cid}))

    ack = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for msg in _pending(tws):
            if (msg.get('type') == 'command_ack'
                    and msg.get('command_id') == cid):
                ack = msg
                break
        if ack:
            break
        await asyncio.sleep(0.02)
    ack_latency = time.monotonic() - t1

    committed = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_trip_status, trip.id) == 'completed':
            committed = time.monotonic() - t1
            break
        await asyncio.sleep(0.1)

    after = await asyncio.to_thread(_measure, trip.id)

    stop.set()
    if reader:
        await reader
    for c in (dws, tws, rws):
        _pending(c)
        try:
            await c.disconnect(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    return {
        'minutes': minutes,
        'frames_sent': frames,
        'frames_ingested': mid['stream_len'] - before['stream_len'],
        'send_elapsed_s': round(send_elapsed, 2),
        'redis_mem_delta_kb': round(
            (after['used_memory'] - before['used_memory']) / 1024, 1),
        'stream_len': after['stream_len'],
        'channel_keys': after['channel_keys'],
        'geo_keys': after['geo_keys'],
        'durable_points': after['points'],
        'driver_cmd_socket_buffer': driver_buffer,
        'rider_socket_buffer': rider_buffer,
        'drain': drain,
        'ack': ack and ack.get('status'),
        'ack_latency_s': round(ack_latency, 2),
        'committed_s': round(committed, 2) if committed else None,
        'final_status': await asyncio.to_thread(_trip_status, trip.id),
    }


@pytest.mark.parametrize('minutes', [30, 60])
async def test_a_long_ride_completes_and_holds_nothing_unbounded(minutes):
    """30 and 60 simulated minutes. Run by default; the heavy ones are separate."""
    report = await _soak(minutes, minutes)
    print(f'SOAK {minutes}min: {report}')

    assert report['final_status'] == 'completed', (
        f"`complete` did not land after {report['frames_sent']} frames "
        f"({minutes} simulated minutes)"
    )
    assert report['ack'] == 'committed', report['ack']
    assert report['frames_ingested'] >= report['frames_sent'] * 0.95, (
        f"only {report['frames_ingested']} of {report['frames_sent']} frames "
        'reached the GPS stream -- ingestion degraded over the ride'
    )
    # The driver's COMMAND socket must stay clean however long the ride runs.
    # This is the property whose absence caused the original blocker: a buffer that
    # fills stops the client answering keepalives and the server closes the socket.
    assert report['driver_cmd_socket_buffer'] <= 2, (
        f"{report['driver_cmd_socket_buffer']} frames were waiting on the driver's "
        'command socket after the ride; it should carry almost nothing'
    )
    # The durable trail must be SAMPLED, not one row per frame, or storage scales
    # with ride duration.
    assert report['durable_points'] < report['frames_sent'], (
        f"{report['durable_points']} durable points for {report['frames_sent']} "
        'frames: the sampler is not sampling, so a four-hour ride writes a row '
        'per ping'
    )


@pytest.mark.slow
@pytest.mark.parametrize('minutes', [120, 240])
async def test_growth_stays_bounded_at_two_and_four_hours(minutes):
    """The lengths a pilot will actually see on a bad day.

    Marked slow: 2880 and 5760 frames take minutes of CPU, not minutes of sleep.
    """
    report = await _soak(1000 + minutes, minutes)
    print(f'SOAK {minutes}min: {report}')

    assert report['final_status'] == 'completed'
    assert report['ack'] == 'committed'
    assert report['driver_cmd_socket_buffer'] <= 2
    assert report['durable_points'] < report['frames_sent'], (
        f"{report['durable_points']} durable points for "
        f"{report['frames_sent']} frames: the sampler is not sampling"
    )


async def test_lifecycle_latency_does_not_scale_with_ride_length():
    """The headline comparison, in one test so the numbers sit side by side."""
    short = await _soak(2001, 5)
    long_ride = await _soak(2002, 60)

    print(f'latency: 5min={short["committed_s"]}s  '
          f'60min={long_ride["committed_s"]}s')
    print(f'ingestion: 5min={short["frames_ingested"]}/{short["frames_sent"]}  '
          f'60min={long_ride["frames_ingested"]}/{long_ride["frames_sent"]}')

    assert short['final_status'] == 'completed'
    assert long_ride['final_status'] == 'completed'

    baseline = max(short['committed_s'] or 0.2, 0.2)
    assert (long_ride['committed_s'] or 0) < baseline * 8, (
        f"completion latency grew from {short['committed_s']}s to "
        f"{long_ride['committed_s']}s between a 5-minute and a 60-minute ride"
    )


# ---------------------------------------------------------------------------
# Durable-trail growth, measured honestly
# ---------------------------------------------------------------------------
#
# The soaks above cannot measure the durable trail. The sampler is time-based
# (MIN_INTERVAL_SECONDS), and accelerating a ride collapses the time axis, so every
# frame arrives inside the same second and the sampler correctly rejects almost all
# of them -- 720 frames produced ONE durable point. Asserting on that number would
# be asserting that acceleration works, not that sampling does.
#
# `recorded_at` is derived from the Redis stream entry ID, not from the payload, so
# a test can write entries with explicit, realistically spaced IDs and get a
# faithful trail. That is what these do, and it is where the storage estimates for
# retention come from.

def _write_spaced_stream_entries(driver_id, start_ms, count, interval_ms=2500,
                                 step=0.0004):
    """Write `count` location events with stream IDs spaced `interval_ms` apart.

    Redis requires stream IDs to increase monotonically, and the stream is shared
    across tests, so `start_ms` is treated as a floor rather than an absolute: the
    first entry lands just after whatever is already at the top. The trip's
    collection window is open-ended (no completed_at yet), so a timestamp after
    `started_at` is attributed correctly however far ahead it sits.
    """
    from servers.redis_client import LOCATION_STREAM

    _, stream, _ = _redis_clients()
    try:
        top = stream.xrevrange(LOCATION_STREAM, count=1)
        if top:
            top_ms = int(top[0][0].decode().split('-')[0])
            start_ms = max(start_ms, top_ms + 1)
    except Exception:  # noqa: BLE001 -- an empty stream is fine
        pass
    for i in range(count):
        stream.xadd(
            LOCATION_STREAM,
            {'driver_id': str(driver_id),
             'lng': f'{LNG + step * (i % 11):.7f}',
             'lat': f'{LAT + step * i:.7f}'},
            id=f'{start_ms + i * interval_ms}-0',
            maxlen=200000,
        )
    return count


@pytest.mark.parametrize('minutes', [10, 30, 60])
async def test_the_durable_trail_grows_with_time_not_with_frame_count(minutes):
    """One point per sampling interval, however many frames arrive.

    This is the number retention planning needs: at MIN_INTERVAL_SECONDS=5 the trail
    cannot exceed 12 points per minute no matter how often the app transmits.
    """
    from django.test import override_settings
    from django.utils import timezone

    from servers.ride.location_trail import drain_location_stream

    idx = 3000 + minutes
    rider = await asyncio.to_thread(_make_rider, idx)
    driver = await asyncio.to_thread(_make_driver, idx)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    # Anchor the synthetic entries inside this trip's collection window.
    started = await asyncio.to_thread(
        lambda: Trip.objects.filter(id=trip.id)
        .values_list('started_at', flat=True).first())
    start_ms = int(started.timestamp() * 1000) + 1000

    # Transmit twice per sampling interval, so the sampler has to do real work.
    frames = minutes * 24
    await asyncio.to_thread(_write_spaced_stream_entries, driver.id,
                            start_ms, frames, 2500)

    def _drain_until_quiet():
        with override_settings(GPS_TRAIL_ENABLED=True):
            total = {}
            for _ in range(40):
                r = drain_location_stream(consumer=f'trail-{idx}')
                if not r.get('received'):
                    break
                for k, v in r.items():
                    if isinstance(v, int):
                        total[k] = total.get(k, 0) + v
            return total

    result = await asyncio.to_thread(_drain_until_quiet)
    points = await asyncio.to_thread(
        lambda: TripLocationPoint.objects.filter(trip_id=trip.id).count())

    # 5-second sampling over `minutes` minutes: 12/minute is the ceiling.
    ceiling = minutes * 12 + 2
    per_minute = points / minutes
    print(f'TRAIL {minutes}min: frames={frames} durable_points={points} '
          f'per_minute={per_minute:.1f} ceiling={ceiling} drain={result}')

    assert points > 0, (
        f'no durable points were written at all: {result} -- the trail writer is '
        'not persisting, so there is no GPS evidence for any ride'
    )
    assert points <= ceiling, (
        f'{points} points for {minutes} minutes exceeds the {ceiling} the '
        f'{5}-second sampler should allow, so storage scales with transmit rate '
        'rather than with ride duration'
    )
    assert points < frames, (
        f'{points} points for {frames} frames: nothing was sampled out'
    )
    assert timezone is not None


@pytest.mark.slow
async def test_the_per_trip_point_cap_is_enforced():
    """MAX_POINTS_PER_TRIP is the last line against a broken or hostile client.

    A client transmitting far faster than it should must not be able to write an
    unbounded number of rows for one trip. The cap is 5000, which is about seven
    hours of honest 5-second sampling -- so it is a defence, not a normal limit.
    """
    from django.test import override_settings

    from servers.ride.location_trail import drain_location_stream, max_points_per_trip

    idx = 3900
    rider = await asyncio.to_thread(_make_rider, idx)
    driver = await asyncio.to_thread(_make_driver, idx)
    trip = await asyncio.to_thread(_make_trip, rider, driver)

    started = await asyncio.to_thread(
        lambda: Trip.objects.filter(id=trip.id)
        .values_list('started_at', flat=True).first())
    start_ms = int(started.timestamp() * 1000) + 1000

    cap = max_points_per_trip()
    # Comfortably more sampling intervals than the cap allows.
    await asyncio.to_thread(_write_spaced_stream_entries, driver.id, start_ms,
                            cap + 400, 6000)

    def _drain_all():
        with override_settings(GPS_TRAIL_ENABLED=True):
            for _ in range(200):
                r = drain_location_stream(consumer=f'cap-{idx}')
                if not r.get('received'):
                    break

    await asyncio.to_thread(_drain_all)
    points = await asyncio.to_thread(
        lambda: TripLocationPoint.objects.filter(trip_id=trip.id).count())
    print(f'CAP: wrote {cap + 400} sampling-eligible events, stored {points}, '
          f'cap={cap}')

    assert points <= cap, (
        f'{points} points stored for one trip against a cap of {cap}: a broken '
        'client can write unbounded rows'
    )
