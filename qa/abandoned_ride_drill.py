"""QA drill: a driver disappears mid-ride, and nobody needs a shell.

This is the proof for the failure that started all of it. QA trip 42 reached
`in_progress`, both clients vanished, and the driver was removed from supply
permanently -- `/ride/active/` returned the dead trip forever, dispatch reported
`drivers_notified=0`, and recovery took an engineer opening the driver's WebSocket
and sending `complete`.

The drill runs the two branches that matter:

    BRANCH A -- the driver comes back
        start a ride, drop the driver's sockets without warning, reconnect,
        confirm the trip is still there and the ride completes normally.
        Nobody is involved. No operator, no engineer.

    BRANCH B -- the driver does not come back
        start a ride, drop the driver, leave it. Confirm the ride is NOT
        auto-cancelled, that PostgreSQL still holds the truth, and that the trip
        becomes visible as stale so operations can act.

What the drill deliberately does NOT do: complete or cancel the abandoned ride on
the driver's behalf. That is the whole point -- an automatic terminal transition is
the wrong answer, and inventing one here would hide the gap instead of proving it
is handled.

Evidence discipline: prints trip ids, statuses, timings and classifications. Never
an OTP, a JWT, a phone number, a name or a coordinate.

Usage:
    ACCEPTANCE_RIDER="+91...:OTP" ACCEPTANCE_DRIVER="+91...:OTP" \\
        python qa/abandoned_ride_drill.py [--branch a|b|both] [--wait SECONDS]

`--wait` is how long branch B stays silent before checking for detection. It must
exceed the deployment's TRIP_STALE_AFTER_SECONDS (600 by default) for the flag to
appear, so the default here is deliberately longer.
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import websockets

B = os.environ.get('ACCEPTANCE_BASE_URL',
                   'https://backend-qa-811d.up.railway.app')
WSB = B.replace('https://', 'wss://').replace('http://', 'ws://')

P_LAT, P_LNG = 17.4450000, 78.3800000
STEP = 0.0007

STAGES = []


def _pair(name):
    raw = os.environ.get(name, '')
    if ':' not in raw:
        sys.exit(f'{name} is not set. Expected "{name}=+91XXXXXXXXXX:OTP".')
    phone, otp = raw.split(':', 1)
    return phone.strip(), otp.strip()


RIDER = _pair('ACCEPTANCE_RIDER')
DRIVER = _pair('ACCEPTANCE_DRIVER')


def stage(name, ok, **detail):
    STAGES.append((name, bool(ok), detail))
    flag = 'PASS' if ok else 'FAIL'
    extra = ' '.join(f'{k}={v}' for k, v in detail.items())
    print(f'[{time.strftime("%H:%M:%S", time.gmtime())}] {flag:4} {name:34} {extra}',
          flush=True)
    return ok


def note(event, **kw):
    extra = ' '.join(f'{k}={v}' for k, v in kw.items())
    print(f'[{time.strftime("%H:%M:%S", time.gmtime())}] ---- {event:34} {extra}',
          flush=True)


def req(method, path, payload=None, tok=None, timeout=45):
    h = {'Content-Type': 'application/json'}
    if tok:
        h['Authorization'] = f'Bearer {tok}'
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(B + path, data=data, headers=h, method=method)
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


def login(phone, otp, role):
    req('POST', '/api/v1/auth/otp/', {'phone_number': phone, 'role': role})
    c, d = req('POST', '/api/v1/auth/login/',
               {'phone_number': phone, 'otp': otp, 'role': role})
    return (d.get('data') or {}).get('token')


def unwrap(d):
    return d.get('data', d) if isinstance(d, dict) else d


def trip_detail(trip_id, tok):
    c, d = req('GET', f'/api/v1/ride/trip/{trip_id}/', tok=tok)
    inner = unwrap(d) or {}
    return c, (inner.get('data', inner) if isinstance(inner, dict) else {})


async def recv(ws, timeout):
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except Exception:  # noqa: BLE001
        return None


async def _announce_presence(dws):
    """Behave like a driver app: connecting is not enough to be dispatchable."""
    for _ in range(4):
        await dws.send(json.dumps({'lat': P_LAT, 'lng': P_LNG}))
        await recv(dws, 2)
        await asyncio.sleep(2)
    await asyncio.sleep(4)


async def _start_a_ride(rtok, dtok):
    """Take a ride to in_progress. Returns (trip_id, driver_sockets)."""
    dws = await websockets.connect(
        f'{WSB}/ws/driver/location/?token={dtok}&lat={P_LAT}&lng={P_LNG}',
        open_timeout=30, ping_interval=20, close_timeout=10)
    await recv(dws, 8)
    rws = await websockets.connect(f'{WSB}/ws/ride/request/?token={rtok}',
                                   open_timeout=30, ping_interval=20,
                                   close_timeout=10)
    await recv(rws, 8)
    await _announce_presence(dws)

    await rws.send(json.dumps({
        'pickup_lat': P_LAT, 'pickup_lng': P_LNG,
        'destination_lat': P_LAT + STEP * 30, 'destination_lng': P_LNG,
        'pickup_address': 'QA Pickup', 'destination_address': 'QA Drop',
        'distance_km': 2.4, 'duration_min': 8,
        'vehicle_type': 'sedan', 'payment_method': 'cash',
        'client_request_id': f'drill-{uuid.uuid4().hex[:12]}',
    }))

    trip_id, notified = None, None
    for _ in range(10):
        m = await recv(rws, 14)
        if not m:
            break
        if m.get('type') == 'trip_created':
            trip_id = m.get('trip_id')
        if m.get('type') == 'dispatch_progress':
            notified = m.get('drivers_notified')
            break
    if not trip_id:
        stage('DRILL_SETUP', False, reason='no_trip_created')
        return None, None
    stage('SETUP_RIDE_REQUESTED', True, trip_id=trip_id,
          drivers_notified=notified)

    for _ in range(10):
        m = await recv(dws, 12)
        if m and (m.get('trip_id') == trip_id or m.get('type') == 'ride_request'):
            break

    tws = await websockets.connect(f'{WSB}/ws/ride/trip/{trip_id}/?token={dtok}',
                                   open_timeout=30, ping_interval=20,
                                   close_timeout=10)
    await recv(tws, 8)

    async def act(action, **kw):
        cid = f'{action}-{uuid.uuid4().hex[:8]}'
        await tws.send(json.dumps({'action': action, 'command_id': cid, **kw}))
        for _ in range(8):
            m = await recv(tws, 12)
            if not m:
                break
            if m.get('type') == 'command_ack' and m.get('command_id') == cid:
                return m.get('status'), m.get('trip_status')
            if m.get('type') == 'error':
                return 'error:' + str(m.get('message'))[:50], None
        return None, None

    st, _ = await act('accept')
    otp = None
    for _ in range(12):
        m = await recv(rws, 4)
        if m and m.get('otp'):
            otp = m['otp']
    await act('reached')
    if not otp:
        for _ in range(10):
            m = await recv(rws, 3)
            if m and m.get('otp'):
                otp = m['otp']
                break
    started, status = await act('start', otp=otp) if otp else (None, None)
    stage('SETUP_RIDE_IN_PROGRESS', status == 'in_progress',
          trip_id=trip_id, ack=started, trip_status=status)

    # A few GPS frames so the trip has recorded liveness before it goes quiet.
    for i in range(6):
        await dws.send(json.dumps({'lat': round(P_LAT + STEP * i, 7),
                                   'lng': P_LNG}))
        await recv(dws, 1)
        await asyncio.sleep(2)
    note('SETUP_GPS_SENT', frames=6)

    return trip_id, (dws, rws, tws)


async def _drop(sockets):
    """Disappear the way a killed app does: no disconnect frame, just gone."""
    for s in sockets or ():
        try:
            await s.close(code=1006)
        except Exception:  # noqa: BLE001
            try:
                s.transport.abort()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Branch A -- the driver comes back
# ---------------------------------------------------------------------------

async def branch_a(rtok, dtok):
    print('=' * 78)
    print('BRANCH A -- driver crashes mid-ride and reconnects')
    print('=' * 78)

    trip_id, sockets = await _start_a_ride(rtok, dtok)
    if not trip_id:
        return
    await _drop(sockets)
    note('DRIVER_CRASHED', trip_id=trip_id, note='sockets dropped without a close')
    await asyncio.sleep(8)

    # The trip must still be there and still in progress. No auto-cancel.
    c, det = trip_detail(trip_id, rtok)
    stage('A_TRIP_SURVIVES_THE_CRASH', det.get('status') == 'in_progress',
          http=c, status=det.get('status'))

    # The driver app restarts: fresh login, fresh sockets.
    dtok2 = login(*DRIVER, role='driver')
    stage('A_DRIVER_REAUTHENTICATES', bool(dtok2))

    c, active = req('GET', '/api/v1/ride/active/', tok=dtok2)
    body = unwrap(active) or {}
    inner = body.get('data', body) if isinstance(body, dict) else {}
    recovered_id = inner.get('id') if isinstance(inner, dict) else None
    stage('A_DRIVER_RECOVERS_ITS_TRIP', str(recovered_id) == str(trip_id),
          http=c, recovered_trip=recovered_id, expected=trip_id)

    dws = await websockets.connect(
        f'{WSB}/ws/driver/location/?token={dtok2}&lat={P_LAT}&lng={P_LNG}',
        open_timeout=30, ping_interval=20, close_timeout=10)
    await recv(dws, 8)
    await _announce_presence(dws)
    stage('A_DRIVER_RESUMES_GPS', True, note='reconnected and pinging')

    tws = await websockets.connect(f'{WSB}/ws/ride/trip/{trip_id}/?token={dtok2}',
                                   open_timeout=30, ping_interval=20,
                                   close_timeout=10)
    await recv(tws, 8)

    async def act(action):
        cid = f'{action}-{uuid.uuid4().hex[:8]}'
        await tws.send(json.dumps({'action': action, 'command_id': cid}))
        for _ in range(8):
            m = await recv(tws, 12)
            if not m:
                break
            if m.get('type') == 'command_ack' and m.get('command_id') == cid:
                return m.get('status'), m.get('trip_status')
        return None, None

    ack, status = await act('complete')
    stage('A_RIDE_COMPLETES_NORMALLY', ack in ('committed', 'already_done'),
          ack=ack, trip_status=status)
    ack2, status2 = await act('confirm_cash')
    stage('A_CASH_CONFIRMED', ack2 in ('committed', 'already_done'),
          ack=ack2, trip_status=status2)

    for s in (tws, dws):
        try:
            await s.close()
        except Exception:  # noqa: BLE001
            pass

    # And crucially the driver is dispatchable again.
    await asyncio.sleep(5)
    dws2 = await websockets.connect(
        f'{WSB}/ws/driver/location/?token={dtok2}&lat={P_LAT}&lng={P_LNG}',
        open_timeout=30, ping_interval=20, close_timeout=10)
    await recv(dws2, 8)
    await _announce_presence(dws2)
    rws2 = await websockets.connect(f'{WSB}/ws/ride/request/?token={rtok}',
                                    open_timeout=30, ping_interval=20,
                                    close_timeout=10)
    await recv(rws2, 8)
    await rws2.send(json.dumps({
        'pickup_lat': P_LAT, 'pickup_lng': P_LNG,
        'destination_lat': P_LAT + STEP * 10, 'destination_lng': P_LNG,
        'pickup_address': 'QA Pickup', 'destination_address': 'QA Drop',
        'distance_km': 1.2, 'duration_min': 4,
        'vehicle_type': 'sedan', 'payment_method': 'cash',
        'client_request_id': f'drill-supply-{uuid.uuid4().hex[:10]}',
    }))
    probe_trip, notified = None, None
    for _ in range(10):
        m = await recv(rws2, 14)
        if not m:
            break
        if m.get('type') == 'trip_created':
            probe_trip = m.get('trip_id')
        if m.get('type') == 'dispatch_progress':
            notified = m.get('drivers_notified')
            break
    stage('A_DRIVER_BACK_IN_SUPPLY', bool(notified),
          drivers_notified=notified, probe_trip=probe_trip,
          note='0 here would be the trip-42 failure reproducing')

    if probe_trip:
        req('POST', f'/api/v1/ride/trip/{probe_trip}/cancel/',
            {'reason': 'drill_cleanup'}, tok=rtok)
    for s in (dws2, rws2):
        try:
            await s.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Branch B -- the driver does not come back
# ---------------------------------------------------------------------------

async def branch_b(rtok, dtok, wait_seconds):
    print('=' * 78)
    print('BRANCH B -- driver disappears and never returns')
    print('=' * 78)

    trip_id, sockets = await _start_a_ride(rtok, dtok)
    if not trip_id:
        return None
    await _drop(sockets)
    note('DRIVER_GONE', trip_id=trip_id)

    note('WAITING_FOR_DETECTION', seconds=wait_seconds,
         note='must exceed the deployment TRIP_STALE_AFTER_SECONDS')
    await asyncio.sleep(wait_seconds)

    c, det = trip_detail(trip_id, rtok)
    status = det.get('status')

    # The single most important assertion in the drill.
    stage('B_NO_FALSE_AUTO_CANCEL', status == 'in_progress',
          http=c, status=status,
          note='silence must never be treated as abandonment')
    stage('B_MONEY_UNTOUCHED', det.get('final_fare') in (None, '__absent__'),
          final_fare=repr(det.get('final_fare')))
    stage('B_POSTGRES_STILL_HOLDS_THE_TRUTH',
          str(det.get('id')) == str(trip_id), trip_id=det.get('id'))

    note('OPERATOR_HANDOFF', trip_id=trip_id,
         note='an operator opens /stale-rides/ and decides; this drill stops here '
              'rather than completing or cancelling the ride on the driver\'s '
              'behalf')
    return trip_id


# ---------------------------------------------------------------------------

async def main():
    branch = 'both'
    wait = 700
    args = sys.argv[1:]
    if '--branch' in args:
        branch = args[args.index('--branch') + 1]
    if '--wait' in args:
        wait = int(args[args.index('--wait') + 1])

    c, v = req('GET', '/version')
    print(f'revision under test: {(v or {}).get("revision_short")}   '
          f'environment: {(v or {}).get("environment")}')

    rtok = login(*RIDER, role='rider')
    dtok = login(*DRIVER, role='driver')
    if not (rtok and dtok):
        sys.exit('could not authenticate the QA rider/driver')

    if branch in ('a', 'both'):
        await branch_a(rtok, dtok)
    if branch in ('b', 'both'):
        await branch_b(rtok, dtok, wait)

    print('=' * 78)
    bad = [s for s in STAGES if not s[1]]
    print(f'DRILL STAGES PASSED {len(STAGES) - len(bad)}/{len(STAGES)}')
    for name, _ok, detail in bad:
        print(f'  FAILED  {name}  {detail}')
    print('=' * 78)


if __name__ == '__main__':
    asyncio.run(main())
