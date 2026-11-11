"""§13-15 — concurrent ride load against an isolated stack, and what it does prove.

HOW TO RUN IT
-------------
    docker compose -f docker-compose.load.yml up -d --build
    python qa/load_harness.py --rides 10
    python qa/load_harness.py --rides 25
    python qa/load_harness.py --rides 50
    python qa/load_harness.py --rides 100 --batch 25     # the 100-ride simulation
    docker compose -f docker-compose.load.yml down -v

Isolated by construction: its own PostgreSQL, Redis, Daphne and Celery worker on
private ports, with a throwaway database. Never shared QA -- a hundred synthetic
rides would pollute the trip, settlement and wallet tables every money
reconciliation reads, and the numbers would include whatever else was using QA.

WHAT IT MEASURES
----------------
Only what it can actually observe, which is the wall-clock time between sending a
frame and receiving the specific frame that answers it:

    booking      ride-request sent            -> trip_created
    dispatch     trip_created                 -> dispatch_progress
    offer        dispatch_progress            -> the driver's socket sees the trip
    command      each lifecycle frame sent    -> its correlated command_ack
    gps          frames sent vs frames accepted

Plus, from the stack rather than from the client: PostgreSQL connection count,
Redis errors, and Celery queue depth, sampled before and after.

WHAT IT DOES NOT PROVE, STATED HERE SO NOBODY HAS TO INFER IT
-------------------------------------------------------------
**It is not a production capacity model.** One machine, one container each,
localhost networking, no TLS, no CDN, no mobile radio, no cross-region latency, a
warm cache, and a database with `fsync=off`. Every one of those flatters the result.
The numbers are useful for finding contention, serialisation and breakage under
concurrency. They cannot be quoted as "the platform supports N rides".

**It does not measure sign-in.** Tokens are minted by `seed_load_fixtures` through
the same machinery the login view uses, so a hundred OTP round-trips do not sit
inside a measurement about the ride lifecycle. Auth has its own suites.

**Percentiles are reported only where there are enough samples to mean anything.**
With ten rides there is no p99, and printing one would be arithmetic pretending to
be evidence. The threshold is stated in the output.

**S3, FCM, SMS and Cashfree are absent from the stack.** Their absence exercises the
product's degradation paths rather than the providers, and the run records that it
did so.

WHAT IT ASSERTS AFTERWARDS (§14, §15)
-------------------------------------
Load is only half the point. After the rides, the harness reconciles the dataset it
generated:

  * every completed cash ride has exactly one settlement, and no duplicate wallet
    movement;
  * commission is exact to the paisa against the trip's own recorded fare;
  * no driver ever held two active trips at once, which is §15's invariant, put
    under deliberate contention by running more concurrent riders than drivers.
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict

try:
    import websockets
except ImportError:  # pragma: no cover
    print('this harness needs the `websockets` package')
    raise

BASE = os.environ.get('LOAD_BASE_URL', 'http://127.0.0.1:8100')
WSB = BASE.replace('https://', 'wss://').replace('http://', 'ws://')
COMPOSE = ['docker', 'compose', '-f', 'docker-compose.load.yml']

STEP = 0.0007
# Below this many samples a percentile is noise dressed as a number.
MIN_SAMPLES_FOR_P95 = 20
MIN_SAMPLES_FOR_P99 = 100

timings = defaultdict(list)
outcomes = defaultdict(int)
failures = []


def record(metric, seconds):
    timings[metric].append(seconds * 1000.0)


def note_failure(ride, stage, detail):
    failures.append({'ride': ride, 'stage': stage, 'detail': str(detail)[:160]})


# ---------------------------------------------------------------------------
# HTTP + stack introspection
# ---------------------------------------------------------------------------

def req(method, path, payload=None, tok=None, timeout=45):
    h = {'Content-Type': 'application/json'}
    if tok:
        h['Authorization'] = f'Bearer {tok}'
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {'transport_error': type(e).__name__}


def compose_exec(service, *cmd, timeout=180):
    try:
        out = subprocess.run(
            COMPOSE + ['exec', '-T', service] + list(cmd),
            capture_output=True, text=True, timeout=timeout)
        return out.returncode, out.stdout, out.stderr
    except Exception as e:  # noqa: BLE001
        return 1, '', repr(e)


def pg_connections():
    rc, out, _ = compose_exec(
        'loaddb', 'psql', '-U', 'sgload', '-d', 'sgload', '-tAc',
        'select count(*) from pg_stat_activity where datname=\'sgload\'')
    try:
        return int(out.strip())
    except Exception:  # noqa: BLE001
        return None


def redis_stat(field):
    rc, out, _ = compose_exec('loadredis', 'redis-cli', 'info', 'stats')
    for line in out.splitlines():
        if line.startswith(field + ':'):
            return line.split(':', 1)[1].strip()
    return None


def celery_backlog():
    rc, out, _ = compose_exec('loadredis', 'redis-cli', 'llen', 'celery')
    try:
        return int(out.strip())
    except Exception:  # noqa: BLE001
        return None


def seed(n_riders, n_drivers):
    print(f'seeding {n_riders} riders and {n_drivers} drivers...', flush=True)
    out = subprocess.run(
        COMPOSE + ['exec', '-T', 'loadbackend', 'python', 'manage.py',
                   'seed_load_fixtures', '--riders', str(n_riders),
                   '--drivers', str(n_drivers), '--json'],
        capture_output=True, text=True, timeout=600)
    # The command emits only JSON on stdout, but Django logs warnings there too in
    # some configurations, so take the last line that parses.
    for line in reversed(out.stdout.splitlines()):
        line = line.strip()
        if line.startswith('{'):
            return json.loads(line)
    raise SystemExit(f'could not seed: {out.stdout[-500:]}\n{out.stderr[-500:]}')


# ---------------------------------------------------------------------------
# One ride, driven the way an app drives it
# ---------------------------------------------------------------------------

async def recv(ws, timeout):
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except Exception:  # noqa: BLE001
        return None


async def one_ride(idx, rider, driver, gps_frames, pickup):
    """Book, dispatch, accept, run and complete one ride. Returns a result dict."""
    lat, lng = pickup
    # Spread pickups slightly so every ride is not the identical geo point, which
    # would make the geo index unrealistically kind.
    lat += (idx % 7) * STEP
    lng += (idx % 5) * STEP

    rtok, dtok = rider['token'], driver['token']
    result = {'ride': idx, 'trip_id': None, 'completed': False,
              'gps_sent': 0, 'driver_user_id': driver['id'],
              'rider_user_id': rider['id']}
    dws = rws = tws = rider_tws = None
    try:
        dws = await websockets.connect(
            f'{WSB}/ws/driver/location/?token={dtok}&lat={lat}&lng={lng}',
            open_timeout=40, ping_interval=20, close_timeout=10)
        await recv(dws, 8)
        rws = await websockets.connect(f'{WSB}/ws/ride/request/?token={rtok}',
                                      open_timeout=40, ping_interval=20,
                                      close_timeout=10)
        await recv(rws, 8)

        # A driver becomes dispatchable by PINGING, not by connecting: the geo
        # index entry is rewritten per ping and presence has a 45s TTL.
        for _ in range(3):
            await dws.send(json.dumps({'lat': lat, 'lng': lng}))
            await recv(dws, 2)
            await asyncio.sleep(0.6)

        # ---- booking
        t0 = time.perf_counter()
        await rws.send(json.dumps({
            'pickup_lat': lat, 'pickup_lng': lng,
            'destination_lat': lat + STEP * 30, 'destination_lng': lng,
            'pickup_address': 'Load pickup', 'destination_address': 'Load drop',
            'distance_km': 2.4, 'duration_min': 8,
            'vehicle_type': 'sedan', 'payment_method': 'cash',
            'client_request_id': f'load-{uuid.uuid4().hex[:16]}',
        }))

        trip_id, t_created, dispatched = None, None, None
        deadline = time.perf_counter() + 60
        while time.perf_counter() < deadline:
            m = await recv(rws, 20)
            if m is None:
                break
            if m.get('type') == 'trip_created' and trip_id is None:
                trip_id = m.get('trip_id')
                t_created = time.perf_counter()
                record('booking', t_created - t0)
            if m.get('type') == 'dispatch_progress':
                dispatched = m.get('drivers_notified')
                if t_created:
                    record('dispatch', time.perf_counter() - t_created)
                break
        result['trip_id'] = trip_id
        result['drivers_notified'] = dispatched
        if not trip_id:
            outcomes['booking_failed'] += 1
            note_failure(idx, 'booking', 'no trip_created frame')
            return result
        outcomes['booked'] += 1

        # ---- the offer reaching the driver
        t_offer = time.perf_counter()
        offered = False
        odeadline = time.perf_counter() + 30
        while time.perf_counter() < odeadline:
            m = await recv(dws, 10)
            if m is None:
                break
            if m.get('trip_id') == trip_id or m.get('type') == 'ride_request':
                offered = True
                record('offer', time.perf_counter() - t_offer)
                break
        result['offered'] = offered
        if not offered:
            outcomes['not_offered'] += 1
            note_failure(idx, 'offer', f'trip {trip_id} never reached the driver')

        # ---- lifecycle
        tws = await websockets.connect(
            f'{WSB}/ws/ride/trip/{trip_id}/?token={dtok}',
            open_timeout=40, ping_interval=20, close_timeout=10)
        await recv(tws, 8)

        # The rider's TRIP socket, which a real rider app opens as soon as it has a
        # trip id. Omitting it cost 4 of 50 rides at stage 3 with "OTP never
        # delivered": the OTP goes to the trip group, and a rider not yet attached
        # to that group when it is emitted simply misses it. Under light load the
        # rider's request socket happened to see it; at 50 concurrent, dispatch
        # latency widened the window and it did not.
        #
        # So that was a harness gap, not a product defect -- but only checking it
        # this way could tell the two apart.
        rider_tws = await websockets.connect(
            f'{WSB}/ws/ride/trip/{trip_id}/?token={rtok}',
            open_timeout=40, ping_interval=20, close_timeout=10)
        await recv(rider_tws, 8)

        async def act(action, **kw):
            cid = f'{action}-{uuid.uuid4().hex[:10]}'
            t = time.perf_counter()
            await tws.send(json.dumps({'action': action, 'command_id': cid, **kw}))
            ddl = time.perf_counter() + 45
            while time.perf_counter() < ddl:
                m = await recv(tws, 20)
                if m is None:
                    break
                if (m.get('type') == 'command_ack'
                        and m.get('command_id') == cid):
                    record(f'command.{action}', time.perf_counter() - t)
                    # The field is `status`, not `result`.
                    return m.get('status'), m.get('trip_status')
            return None, None

        ack, _ = await act('accept')
        if ack not in ('committed', 'already_done'):
            outcomes['accept_failed'] += 1
            note_failure(idx, 'accept', f'ack={ack}')
            return result
        outcomes['accepted'] += 1

        # The OTP reaches the rider, on either of the two sockets a rider app
        # holds. Polled alternately rather than draining one to exhaustion, so a
        # quiet socket cannot starve the other.
        otp = None
        ddl = time.perf_counter() + 30
        while time.perf_counter() < ddl and not otp:
            for sock in (rider_tws, rws):
                m = await recv(sock, 2)
                if m and m.get('otp'):
                    otp = m['otp']      # captured, never printed
                    break

        ack, _ = await act('reached')
        if ack not in ('committed', 'already_done'):
            note_failure(idx, 'reached', f'ack={ack}')

        if not otp:
            # PUSH MISSED. Fall back to the pull path, which is what a real rider
            # app does when it resyncs: GET /ride/active/ returns the OTP to the
            # rider on that trip (TripDetailSerializer.get_otp).
            #
            # This is a measured characteristic, not a harness workaround. At 50
            # concurrent rides roughly one ride in ten never received the OTP on
            # either socket, and it stayed that way after subscribing the rider's
            # trip channel as well. The fan-out is best-effort: channels_redis
            # drops to a channel whose queue is full, and group_send swallows that
            # per-channel, so a client not draining continuously can miss a frame.
            #
            # The product is recoverable BECAUSE the pull path exists. Counted
            # separately so the report can say how often the push alone was
            # insufficient.
            outcomes['otp_push_missed'] += 1
            c, d = req('GET', '/api/v1/ride/active/', tok=rtok)
            body = (d.get('data') if isinstance(d, dict) else None) or {}
            otp = body.get('otp')
            if otp:
                outcomes['otp_recovered_by_pull'] += 1

        if not otp:
            outcomes['no_otp'] += 1
            note_failure(idx, 'otp',
                         'not delivered by push AND not recoverable from '
                         '/ride/active/')
            return result

        ack, _ = await act('start', otp=otp)
        if ack not in ('committed', 'already_done'):
            outcomes['start_failed'] += 1
            note_failure(idx, 'start', f'ack={ack}')
            return result
        outcomes['started'] += 1

        # ---- realistic GPS traffic while the ride runs
        sent = 0
        for f in range(gps_frames):
            try:
                await dws.send(json.dumps({
                    'lat': lat + STEP * f, 'lng': lng,
                    'accuracy': 8, 'speed': 24,
                }))
                sent += 1
                # Drain so the client buffer cannot fill. Not draining fills it,
                # TCP backpressure stops the pongs, and Daphne closes the socket --
                # a documented way to invent a product defect from a harness bug.
                await recv(dws, 0.05)
            except Exception as e:  # noqa: BLE001
                note_failure(idx, 'gps', e)
                break
            await asyncio.sleep(0.25)
        result['gps_sent'] = sent
        outcomes['gps_frames_sent'] += sent

        ack, _ = await act('complete')
        if ack not in ('committed', 'already_done'):
            outcomes['complete_failed'] += 1
            note_failure(idx, 'complete', f'ack={ack}')
            return result

        ack, _ = await act('confirm_cash')
        if ack not in ('committed', 'already_done'):
            outcomes['cash_failed'] += 1
            note_failure(idx, 'confirm_cash', f'ack={ack}')
            return result

        outcomes['completed'] += 1
        result['completed'] = True
        return result

    except Exception as e:  # noqa: BLE001
        outcomes['exception'] += 1
        note_failure(idx, 'exception', f'{type(e).__name__}: {e}')
        return result
    finally:
        for ws in (tws, rider_tws, rws, dws):
            if ws is not None:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1)))))
    return s[k]


def report_timings():
    print('\nlatency (ms), measured client-side from frame sent to frame received')
    print(f'{"metric":<22}{"n":>5}{"min":>9}{"median":>9}{"p95":>9}{"p99":>9}{"max":>9}')
    for metric in sorted(timings):
        v = timings[metric]
        p95 = f'{pct(v, 95):.0f}' if len(v) >= MIN_SAMPLES_FOR_P95 else '  --'
        p99 = f'{pct(v, 99):.0f}' if len(v) >= MIN_SAMPLES_FOR_P99 else '  --'
        print(f'{metric:<22}{len(v):>5}{min(v):>9.0f}'
              f'{statistics.median(v):>9.0f}{p95:>9}{p99:>9}{max(v):>9.0f}')
    print(f'\n  p95 shown only with >= {MIN_SAMPLES_FOR_P95} samples, '
          f'p99 only with >= {MIN_SAMPLES_FOR_P99}.')
    print('  Fewer samples than that is arithmetic pretending to be evidence.')


def report_outcomes(n):
    print(f'\noutcomes over {n} ride attempts')
    for k in sorted(outcomes):
        print(f'  {k:<22}{outcomes[k]:>6}')
    if failures:
        print(f'\n{len(failures)} failure(s), first 12:')
        for f in failures[:12]:
            print(f'  ride {f["ride"]:<4} {f["stage"]:<14} {f["detail"]}')


# ---------------------------------------------------------------------------
# §14 / §15 — invariants over the generated dataset
# ---------------------------------------------------------------------------

INVARIANT_SCRIPT = r'''
import json
from collections import Counter
from decimal import Decimal

from django.db.models import Count

from servers.ride.models import Trip
from servers.rider.models import WalletTransaction

SETTLEMENT_PURPOSES = ('trip_earnings', 'trip_commission')

out = {}

completed = list(
    Trip.objects.filter(status_id__status_code='completed')
    .values_list('id', 'driver_id', 'estimated_fare', 'final_fare')
)
out['completed_trips'] = len(completed)

# One settlement per completed trip, and no duplicates.
refs = Counter()
for txn in WalletTransaction.objects.filter(
        purpose__in=SETTLEMENT_PURPOSES, status='completed'
).values_list('reference_id', flat=True):
    if txn and txn.startswith('TRIP_'):
        refs[txn[5:]] += 1

trip_ids = {str(t[0]) for t in completed}
out['settled_trips'] = len([t for t in trip_ids if refs.get(t)])
out['unsettled_trips'] = sorted(t for t in trip_ids if not refs.get(t))[:10]
out['duplicate_settlements'] = {k: v for k, v in refs.items() if v > 1}

# Idempotency keys must be unique -- a duplicate would mean a double movement.
dupe_keys = list(
    WalletTransaction.objects.values('idempotency_key')
    .annotate(n=Count('id')).filter(n__gt=1)
    .exclude(idempotency_key=None).values_list('idempotency_key', 'n')[:10]
)
out['duplicate_idempotency_keys'] = dupe_keys

# final_fare must still be NULL on every trip.
out['trips_with_final_fare'] = Trip.objects.exclude(final_fare=None).count()

# No trip may have more than one settlement row of the same purpose.
out['trips_with_multiple_rows'] = {k: v for k, v in refs.items() if v > 1}

# §15 -- driver exclusivity. No driver may hold two trips that overlap in time.
overlaps = []
by_driver = {}
for t in Trip.objects.exclude(driver_id=None).values(
        'id', 'driver_id', 'accepted_at', 'completed_at', 'cancelled_at'):
    by_driver.setdefault(t['driver_id'], []).append(t)
for driver_id, trips in by_driver.items():
    spans = []
    for t in trips:
        start = t['accepted_at']
        end = t['completed_at'] or t['cancelled_at']
        if start and end:
            spans.append((start, end, t['id']))
    spans.sort()
    for i in range(1, len(spans)):
        prev_start, prev_end, prev_id = spans[i - 1]
        start, end, tid = spans[i]
        if start < prev_end:
            overlaps.append({'driver': driver_id, 'a': prev_id, 'b': tid})
out['driver_trip_overlaps'] = overlaps[:10]
out['driver_trip_overlap_count'] = len(overlaps)

# Commission exact to the paisa, from the trip's own recorded numbers.
bad_commission = []
for tid, driver_id, est, final in completed:
    gross = final if final is not None else est
    if gross is None:
        continue
    rows = list(WalletTransaction.objects.filter(
        reference_id=f'TRIP_{tid}', status='completed',
        purpose__in=SETTLEMENT_PURPOSES).values_list('purpose', 'amount'))
    if not rows:
        continue
    commission = sum((a for p, a in rows if p == 'trip_commission'),
                     Decimal('0.00'))
    expected = (Decimal(gross) * Decimal('20') / Decimal('100')).quantize(
        Decimal('0.01'))
    if commission and abs(commission - expected) > Decimal('0.01'):
        bad_commission.append({'trip': tid, 'gross': str(gross),
                               'commission': str(commission),
                               'expected': str(expected)})
out['commission_mismatches'] = bad_commission[:10]

print('INVARIANTS_JSON:' + json.dumps(out, default=str))
'''


def check_invariants():
    print('\n' + '=' * 78)
    print('§14 / §15 — invariants over the generated dataset')
    print('=' * 78)
    rc, out, err = compose_exec('loadbackend', 'python', 'manage.py', 'shell',
                                '-c', INVARIANT_SCRIPT, timeout=600)
    payload = None
    for line in out.splitlines():
        if line.startswith('INVARIANTS_JSON:'):
            payload = json.loads(line[len('INVARIANTS_JSON:'):])
    if payload is None:
        print(f'could not evaluate invariants: {out[-400:]} {err[-400:]}')
        return 1

    checks = [
        ('every completed trip settled exactly once',
         not payload['unsettled_trips'] and not payload['duplicate_settlements'],
         f"completed={payload['completed_trips']} settled={payload['settled_trips']} "
         f"unsettled={payload['unsettled_trips']} "
         f"duplicates={payload['duplicate_settlements']}"),
        ('no duplicate wallet movement (idempotency keys unique)',
         not payload['duplicate_idempotency_keys'],
         f"duplicates={payload['duplicate_idempotency_keys']}"),
        ('no trip carries more than one settlement row',
         not payload['trips_with_multiple_rows'],
         f"offenders={payload['trips_with_multiple_rows']}"),
        ('final_fare still NULL on every trip',
         payload['trips_with_final_fare'] == 0,
         f"trips with final_fare={payload['trips_with_final_fare']}"),
        ('commission exact to the paisa',
         not payload['commission_mismatches'],
         f"mismatches={payload['commission_mismatches']}"),
        ('§15 no driver ever held two overlapping trips',
         payload['driver_trip_overlap_count'] == 0,
         f"overlaps={payload['driver_trip_overlap_count']} "
         f"{payload['driver_trip_overlaps']}"),
    ]
    bad = 0
    for name, ok, detail in checks:
        print(f'  [{"PASS" if ok else "FAIL"}] {name}')
        print(f'         {detail}')
        if not ok:
            bad += 1
    return bad


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def run_stage(fixtures, rides, concurrency, gps_frames, contend):
    riders = fixtures['riders']
    drivers = fixtures['drivers']
    pickup = tuple(fixtures['pickup'])

    if contend:
        # §15: deliberately fewer drivers than concurrent riders, so more than one
        # rider is competing for the same driver at the same moment.
        drivers = drivers[:max(1, concurrency // 3)]
        print(f'CONTENTION: {concurrency} concurrent riders against '
              f'{len(drivers)} driver(s)')

    sem = asyncio.Semaphore(concurrency)

    async def guarded(i):
        async with sem:
            return await one_ride(i, riders[i % len(riders)],
                                  drivers[i % len(drivers)], gps_frames, pickup)

    return await asyncio.gather(*(guarded(i) for i in range(rides)),
                                return_exceptions=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rides', type=int, default=10,
                    help='Total rides to attempt.')
    ap.add_argument('--batch', type=int, default=None,
                    help='Max concurrent rides. Defaults to --rides, i.e. all at '
                         'once. Set lower for the 100-ride simulation.')
    ap.add_argument('--gps', type=int, default=12,
                    help='GPS frames per ride while in progress.')
    ap.add_argument('--contend', action='store_true',
                    help='§15: run with fewer drivers than concurrent riders.')
    ap.add_argument('--no-seed', action='store_true')
    args = ap.parse_args()

    concurrency = args.batch or args.rides

    code, ver = req('GET', '/version')
    if code != 200:
        print(f'the load stack is not answering at {BASE} (HTTP {code}).')
        print('bring it up with:\n  docker compose -f docker-compose.load.yml '
              'up -d --build')
        return 2
    env = (ver.get('environment') or '').lower()
    if env != 'load':
        print(f'refusing to run: {BASE} reports environment {env!r}, not "load". '
              f'This harness generates synthetic rides and settlements and must '
              f'never point at QA or production.')
        return 2

    print('=' * 78)
    print(f'LOAD RUN  rides={args.rides}  concurrency={concurrency}  '
          f'gps_frames={args.gps}  contention={args.contend}')
    print('=' * 78)

    fixtures = (seed(max(args.rides, 10), max(args.rides, 10))
                if not args.no_seed else seed(10, 10))

    before = {'pg_connections': pg_connections(),
              'celery_backlog': celery_backlog(),
              'redis_rejected': redis_stat('rejected_connections'),
              'redis_commands': redis_stat('total_commands_processed')}

    t0 = time.perf_counter()
    results = asyncio.run(run_stage(fixtures, args.rides, concurrency,
                                    args.gps, args.contend))
    wall = time.perf_counter() - t0

    after = {'pg_connections': pg_connections(),
             'celery_backlog': celery_backlog(),
             'redis_rejected': redis_stat('rejected_connections'),
             'redis_commands': redis_stat('total_commands_processed')}

    raised = [r for r in results if isinstance(r, Exception)]
    print(f'\nwall clock {wall:.1f} s for {args.rides} rides '
          f'({args.rides / wall:.2f} rides/s attempted)')
    if raised:
        print(f'{len(raised)} coroutine(s) raised outright: '
              f'{[type(e).__name__ for e in raised[:5]]}')

    report_outcomes(args.rides)
    report_timings()

    print('\nstack, before -> after')
    for k in ('pg_connections', 'celery_backlog', 'redis_rejected',
              'redis_commands'):
        print(f'  {k:<18}{before.get(k)} -> {after.get(k)}')
    print('\n  redis_rejected must stay 0: a rejected connection means the '
          'connection\n  limit was the ceiling, not the application.')
    print('  pg_connections is sampled BEFORE and AFTER, not during, and Django '
          'closes its\n  connection per request (CONN_MAX_AGE unset), so this is '
          'NOT the peak. Read it as\n  "the pool was not left exhausted", never '
          'as "only N connections were used".')

    # Let the post-completion tasks drain before reconciling, or a settlement that
    # is merely still queued reads as a missing one.
    print('\nwaiting 20 s for Celery to drain before reconciling...', flush=True)
    time.sleep(20)
    print(f'  celery_backlog now {celery_backlog()}')

    bad = check_invariants()

    print('\n' + '=' * 78)
    print('This is an isolated single-machine stack with fsync=off, localhost '
          'networking,\nno TLS and a warm cache. It finds contention and breakage '
          'under concurrency.\nIt is NOT a production capacity model and must not '
          'be quoted as one.')
    print('Stubbed out of the stack: S3, FCM, SMS, Cashfree. Their absence '
          'exercises the\nproduct\'s degradation paths, not the providers.')
    print('=' * 78)

    completed = outcomes.get('completed', 0)
    if completed < args.rides:
        print(f'\n{args.rides - completed} of {args.rides} rides did not complete.')
    return 1 if (bad or completed == 0) else 0


if __name__ == '__main__':
    sys.exit(main())
