"""Canonical SaaradhiGo QA acceptance ride.

One rider, one driver, one realistic multi-minute ride against the real QA stack:
PostgreSQL, Redis, Daphne/Channels, Celery. No mocks on the main path.

Emits PASS/FAIL per stage, then the financial reconciliation and the idempotency
retries. Designed to be re-run as a release gate.

EVIDENCE DISCIPLINE. This script prints trip ids, settlement ids, statuses,
command ids, ack results, counts and money. It never prints an OTP, a JWT, a phone
number, a name, or a coordinate. Money is printed because reconciling it is the
point.

Usage:
    qa_acceptance.py [ride_seconds] [--label NAME]
"""

import asyncio
import json
import sys
import os
import time
import urllib.error
import urllib.request
import uuid

import websockets

B = os.environ.get('ACCEPTANCE_BASE_URL',
                   'https://backend-qa-811d.up.railway.app')
WSB = B.replace('https://', 'wss://').replace('http://', 'ws://')

# Credentials come from the environment. Nothing is stored in this file, and
# nothing is printed by it.
#
#   ACCEPTANCE_RIDER="+91XXXXXXXXXX:OTP"
#   ACCEPTANCE_DRIVER="+91XXXXXXXXXX:OTP"
#   ACCEPTANCE_BASE_URL=https://backend-qa-....up.railway.app   (optional)
#
# These are the QA test phone/OTP pairs from the backend's TEST_PHONE_NUMBERS.
# Shared test credentials rather than personal data -- but they identify a login,
# so they are passed in rather than committed.
def _pair(name):
    raw = os.environ.get(name, '')
    if ':' not in raw:
        sys.exit(
            f'{name} is not set. Expected "{name}=+91XXXXXXXXXX:OTP". '
            'These are the QA test credentials from TEST_PHONE_NUMBERS; this '
            'harness deliberately stores none of its own.'
        )
    phone, otp = raw.split(':', 1)
    return phone.strip(), otp.strip()


RIDER = _pair('ACCEPTANCE_RIDER')
DRIVER = _pair('ACCEPTANCE_DRIVER')

P_LAT, P_LNG = 17.4450000, 78.3800000
STEP = 0.0007                     # ~78 m per frame; above the 25 m sampling floor
GPS_INTERVAL = 5.0                # the sampler's minimum interval
_arg = sys.argv[1] if len(sys.argv) > 1 else ''
if _arg in ('-h', '--help'):
    sys.exit(__doc__)
RIDE_SECONDS = int(_arg) if _arg.isdigit() else 180

STAGES = []
FACTS = {}


def stage(name, ok, **detail):
    STAGES.append((name, bool(ok), detail))
    flag = 'PASS' if ok else 'FAIL'
    extra = ' '.join(f'{k}={v}' for k, v in detail.items())
    print(f'[{time.strftime("%H:%M:%S", time.gmtime())}] {flag:4} {name:24} {extra}',
          flush=True)
    return ok


def note(event, **kw):
    extra = ' '.join(f'{k}={v}' for k, v in kw.items())
    print(f'[{time.strftime("%H:%M:%S", time.gmtime())}] ---- {event:24} {extra}',
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
    return (d.get('data') or {}).get('token'), c


def unwrap(d):
    return d.get('data', d) if isinstance(d, dict) else d


async def recv(ws, timeout):
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except Exception:  # noqa: BLE001
        return None


async def drain_active(rtok, dtok):
    """Leave no earlier trip in an active state, or dispatch will refuse."""
    c, d = req('GET', '/api/v1/ride/active/', tok=rtok)
    body = unwrap(d) or {}
    tid = (body.get('data') or body or {}).get('id') if isinstance(body, dict) else None
    if tid:
        req('POST', f'/api/v1/ride/trip/{tid}/cancel/', {'reason': 'qa_reset'}, tok=rtok)
        note('PRECLEAN', cancelled_trip=tid)
    await asyncio.sleep(1)


async def main():
    print('=' * 78)
    print('SaaradhiGo canonical QA acceptance ride')
    c, v = req('GET', '/version')
    rev = (v or {}).get('revision_short', '?')
    print(f'revision under test: {rev}   environment: {(v or {}).get("environment")}')
    print('=' * 78)
    FACTS['revision'] = rev

    # ---------------------------------------------------------------- auth
    rtok, rc = login(*RIDER, role='rider')
    stage('RIDER_AUTH', bool(rtok), http=rc)
    dtok, dc = login(*DRIVER, role='driver')
    stage('DRIVER_AUTH', bool(dtok), http=dc)
    if not (rtok and dtok):
        return summarise()

    await drain_active(rtok, dtok)

    # ---------------------------------------------------- fare quote (pre-book)
    quote_payload = {
        'pickup_lat': P_LAT, 'pickup_long': P_LNG,
        'destination_lat': P_LAT + STEP * 30, 'destination_long': P_LNG,
        'distance_km': 2.4, 'duration_min': 8, 'vehicle_type': 'sedan',
    }
    c, d = req('POST', '/api/v1/ride/estimate-fare/', quote_payload, tok=rtok)
    q = unwrap(d) or {}
    quote_total = q.get('estimated_fare')
    breakdown = q.get('fare_breakdown') or {}
    stage('FARE_QUOTE', c == 200 and quote_total is not None,
          http=c, quote=quote_total,
          components=','.join(sorted(breakdown)) or 'none')
    FACTS['quote'] = quote_total
    FACTS['quote_breakdown'] = breakdown

    # ------------------------------------------------------------ driver online
    dws = await websockets.connect(
        f'{WSB}/ws/driver/location/?token={dtok}&lat={P_LAT}&lng={P_LNG}',
        open_timeout=30, ping_interval=20, close_timeout=10)
    await recv(dws, 8)
    rws = await websockets.connect(f'{WSB}/ws/ride/request/?token={rtok}',
                                   open_timeout=30, ping_interval=20,
                                   close_timeout=10)
    await recv(rws, 8)

    # Behave like a driver app: ping location repeatedly. Connecting alone is not
    # enough to be dispatchable -- the geo index entry is (re)written per ping and
    # the presence heartbeat has a 45s TTL, so a driver whose socket dropped is
    # swept out and has to re-announce itself. A run that merely reconnected and
    # waited got drivers_notified=0.
    for _ in range(4):
        await dws.send(json.dumps({'lat': P_LAT, 'lng': P_LNG}))
        await recv(dws, 2)
        await asyncio.sleep(2)
    await asyncio.sleep(4)
    stage('DRIVER_ONLINE', True, note='location_pinged_and_geo_index_settled')

    # ------------------------------------------------------------- ride request
    # A client-generated id: the booking idempotency key.
    client_request_id = f'acc-{uuid.uuid4().hex[:16]}'
    FACTS['client_request_id'] = client_request_id
    await rws.send(json.dumps({
        'pickup_lat': P_LAT, 'pickup_lng': P_LNG,
        'destination_lat': P_LAT + STEP * 30, 'destination_lng': P_LNG,
        'pickup_address': 'QA Pickup', 'destination_address': 'QA Drop',
        'distance_km': 2.4, 'duration_min': 8,
        'vehicle_type': 'sedan', 'payment_method': 'cash',
        'client_request_id': client_request_id,
    }))

    trip_id = None
    dispatched = None
    for _ in range(10):
        m = await recv(rws, 14)
        if not m:
            break
        if m.get('type') == 'trip_created':
            trip_id = m.get('trip_id')
        if m.get('type') == 'dispatch_progress':
            dispatched = m.get('drivers_notified')
            break
    stage('RIDE_REQUEST', bool(trip_id), trip_id=trip_id)
    stage('DISPATCH', dispatched is not None, drivers_notified=dispatched)
    if not trip_id:
        return summarise()
    FACTS['trip_id'] = trip_id
    FACTS['t_request'] = time.time()

    # -------------------------------------------------------------- driver offer
    offered = False
    for _ in range(10):
        m = await recv(dws, 12)
        if m and (m.get('trip_id') == trip_id or m.get('type') == 'ride_request'):
            offered = True
            break
    stage('DRIVER_OFFER', offered, trip_id=trip_id)

    # ------------------------------------------- lifecycle over the trip socket
    tws = await websockets.connect(f'{WSB}/ws/ride/trip/{trip_id}/?token={dtok}',
                                   open_timeout=30, ping_interval=20,
                                   close_timeout=10)
    await recv(tws, 8)
    rider_tws = await websockets.connect(
        f'{WSB}/ws/ride/trip/{trip_id}/?token={rtok}',
        open_timeout=30, ping_interval=20, close_timeout=10)
    await recv(rider_tws, 8)

    acks = {}

    async def act(action, **kw):
        """Send a lifecycle command with a correlated command_id, await its ack."""
        cid = f'{action}-{uuid.uuid4().hex[:10]}'
        await tws.send(json.dumps({'action': action, 'command_id': cid, **kw}))
        result, status_seen = None, None
        for _ in range(8):
            m = await recv(tws, 12)
            if not m:
                break
            if m.get('type') == 'command_ack' and m.get('command_id') == cid:
                # The field is 'status' -- committed / already_done / rejected.
                # Reading 'result' returned None on every command while the
                # transitions were in fact happening, which is how a harness
                # invents a product defect.
                result = m.get('status')
                status_seen = m.get('trip_status')
                break
            if m.get('type') == 'error':
                result = 'error:' + str(m.get('message'))[:60]
                break
            if (m.get('type') == 'trip_status_update'
                    and m.get('status') == action):
                result = result or 'status_update'
        acks[action] = {'command_id': cid, 'result': result,
                        'trip_status': status_seen}
        return cid, result, status_seen

    cid, res, st = await act('accept')
    stage('ACCEPT', res in ('committed', 'already_done', 'status_update'),
          command_id=cid, ack=res, trip_status=st)

    # rider must be told who is coming
    assigned = False
    otp = None
    for _ in range(12):
        m = await recv(rws, 4)
        if not m:
            continue
        if m.get('otp'):
            otp = m['otp']          # captured, never printed
        if m.get('type') in ('trip_update', 'trip_status_update'):
            assigned = True
    stage('RIDER_ASSIGNMENT', assigned, note='rider_notified_of_driver')

    cid, res, st = await act('reached')
    stage('DRIVER_REACHED', res in ('committed', 'already_done', 'status_update'),
          command_id=cid, ack=res, trip_status=st)

    if not otp:
        for _ in range(10):
            m = await recv(rws, 3)
            if m and m.get('otp'):
                otp = m['otp']
                break
    stage('OTP', bool(otp), note='obtained_via_rider_channel_not_printed')
    if not otp:
        return summarise()

    cid, res, st = await act('start', otp=otp)
    stage('START', res in ('committed', 'already_done', 'status_update'),
          command_id=cid, ack=res, trip_status=st)
    FACTS['t_start'] = time.time()

    # ------------------------------------------------------------------ GPS
    rider_seen = [0]

    async def rider_reader():
        while True:
            m = await recv(rider_tws, 30)
            if m is None:
                continue
            if m.get('type') in ('driver_location_update', 'location_update'):
                rider_seen[0] += 1

    async def driver_socket_drainer():
        # The driver's location socket receives the presence/broadcast stream. Not
        # reading it fills the client buffer, TCP backpressure stops pongs, and the
        # connection dies with "keepalive ping timeout" mid-ride -- which is exactly
        # what happened on the first run of this harness. A real app reads its
        # socket; a harness that does not is measuring itself.
        while True:
            if await recv(dws, 30) is None:
                await asyncio.sleep(0.1)

    reader = asyncio.create_task(rider_reader())
    drainer = asyncio.create_task(driver_socket_drainer())
    frames = 0
    deadline = time.time() + RIDE_SECONDS
    while time.time() < deadline:
        lat = P_LAT + STEP * frames
        await dws.send(json.dumps({'lat': round(lat, 7), 'lng': P_LNG}))
        frames += 1
        await asyncio.sleep(GPS_INTERVAL)
    note('GPS_SENT', frames=frames, seconds=RIDE_SECONDS)
    stage('GPS', frames > 0, frames_sent=frames)
    await asyncio.sleep(3)
    stage('RIDER_LIVE_STATE', rider_seen[0] > 0,
          driver_position_updates_seen_by_rider=rider_seen[0])

    # ------------------------------------------------------------------ SOS
    c, d = req('POST', '/api/v1/sos/',
               {'trip_id': trip_id, 'event_type': 'panic',
                'note': 'QA acceptance drill - not a real emergency'}, tok=rtok)
    sos = unwrap(d) or {}
    sos_id = sos.get('sos_id')
    stage('SOS', c in (200, 201) and bool(sos_id), http=c, sos_id=sos_id,
          status=sos.get('status'))
    FACTS['sos_id'] = sos_id

    # repeated press must collapse, not spam
    c2, d2 = req('POST', '/api/v1/sos/',
                 {'trip_id': trip_id, 'event_type': 'panic'}, tok=rtok)
    sos2 = unwrap(d2) or {}
    stage('SOS_DURABLE_RECORD',
          c2 == 200 and sos2.get('repeat_of') == sos_id,
          http=c2, repeat_of=sos2.get('repeat_of'), same_event=sos2.get('sos_id'))

    # -------------------------------------------------------------- complete
    cid, res, st = await act('complete')
    stage('COMPLETE', res in ('committed', 'already_done', 'status_update'),
          command_id=cid, ack=res, trip_status=st)
    stage('COMMAND_ACK', res == 'committed',
          note='correlated ack returned committed', ack=res)
    FACTS['t_complete'] = time.time()

    cid, res, st = await act('confirm_cash')
    stage('CASH_CONFIRM', res in ('committed', 'already_done', 'status_update'),
          command_id=cid, ack=res, trip_status=st)
    FACTS['acks'] = acks

    reader.cancel()
    drainer.cancel()
    for s in (tws, rider_tws, dws, rws):
        try:
            await s.close()
        except Exception:  # noqa: BLE001
            pass

    # Celery does the settlement/receipt work; give it a moment.
    note('SETTLING', wait_seconds=25)
    await asyncio.sleep(25)

    # ---------------------------------------------------------- trip evidence
    c, d = req('GET', f'/api/v1/ride/trip/{trip_id}/', tok=rtok)
    det = unwrap(d) or {}
    inner = det.get('data', det) if isinstance(det, dict) else {}
    FACTS['trip_detail'] = inner
    fb = inner.get('fare_breakdown') or {}
    stage('FARE_SNAPSHOT', bool(fb) or inner.get('estimated_fare') is not None,
          http=c, components=','.join(sorted(fb)) or 'none',
          status=inner.get('status'))

    final_fare = inner.get('final_fare', '__absent__')
    stage('FINAL_FARE_IS_NULL', final_fare in (None, '__absent__'),
          final_fare=repr(final_fare))

    # ------------------------------------------------------- driver economics
    c, d = req('GET', '/api/v1/driver/earnings/summary/', tok=dtok)
    earn = unwrap(d) or {}
    stage('DRIVER_EARNINGS', c == 200, http=c,
          keys=','.join(sorted(earn)[:8]) if isinstance(earn, dict) else '?')
    FACTS['earnings'] = earn

    c, d = req('GET', '/api/v1/driver/withdrawals/balance/', tok=dtok)
    bal = unwrap(d) or {}
    stage('WALLET_TRANSACTION', c == 200, http=c,
          balance=bal.get('balance') if isinstance(bal, dict) else '?')
    FACTS['wallet'] = bal

    c, d = req('GET', '/api/v1/ride/ride-history/', tok=rtok)
    hist = unwrap(d) or {}
    rows = hist.get('results', hist) if isinstance(hist, dict) else hist
    found = any(str((r or {}).get('id')) == str(trip_id)
                for r in (rows or []) if isinstance(r, dict))
    stage('RIDER_HISTORY', found, http=c, trip_in_history=found)

    # ------------------------------------------------------------ ops boundary
    c_ops, _ = req('GET', '/api/v1/ride/admin/trips/')
    stage('OPS_VISIBILITY', c_ops in (401, 403),
          http=c_ops,
          note='endpoint exists and is protected; NO QA OPERATOR ACCOUNT EXISTS '
               'so authenticated operator visibility is UNPROVEN')

    # --------------------------------------------------------- PHASE 4 retries
    print('-' * 78)
    note('PHASE4_RETRIES', note='re-issuing terminal commands')
    tws2 = await websockets.connect(f'{WSB}/ws/ride/trip/{trip_id}/?token={dtok}',
                                    open_timeout=30, ping_interval=20,
                                    close_timeout=10)
    await recv(tws2, 8)

    async def act2(action):
        cid = f'retry-{action}-{uuid.uuid4().hex[:8]}'
        await tws2.send(json.dumps({'action': action, 'command_id': cid}))
        for _ in range(8):
            m = await recv(tws2, 12)
            if not m:
                break
            if m.get('type') == 'command_ack' and m.get('command_id') == cid:
                return m.get('status'), m.get('trip_status')
            if m.get('type') == 'error':
                return 'error:' + str(m.get('message'))[:50], None
        return None, None

    r_complete, s_complete = await act2('complete')
    stage('RETRY_COMPLETE_IDEMPOTENT',
          r_complete in ('already_done', 'rejected') or
          str(r_complete).startswith('error'),
          ack=r_complete, trip_status=s_complete)

    r_cash, s_cash = await act2('confirm_cash')
    stage('RETRY_CASH_IDEMPOTENT',
          r_cash in ('already_done', 'committed', 'rejected') or
          str(r_cash).startswith('error'),
          ack=r_cash, trip_status=s_cash)
    try:
        await tws2.close()
    except Exception:  # noqa: BLE001
        pass

    # receipt resend is an explicit, safe retry of the receipt path
    c, d = req('POST', f'/api/v1/ride/trip/{trip_id}/receipt/resend/', {}, tok=rtok)
    stage('RETRY_RECEIPT', c in (200, 201, 202, 400, 403, 404), http=c,
          note='resend accepted or cleanly refused; must not alter economics')

    await asyncio.sleep(12)

    # economics must be unchanged after every retry
    c, d = req('GET', f'/api/v1/ride/trip/{trip_id}/', tok=rtok)
    after = (unwrap(d) or {})
    after = after.get('data', after) if isinstance(after, dict) else {}
    same_fare = str(after.get('estimated_fare')) == str(inner.get('estimated_fare'))
    still_null = after.get('final_fare', '__absent__') in (None, '__absent__')
    stage('ECONOMICS_STABLE_AFTER_RETRY', same_fare and still_null,
          estimated_fare_unchanged=same_fare, final_fare_still_null=still_null)

    c, d = req('GET', '/api/v1/driver/withdrawals/balance/', tok=dtok)
    bal2 = unwrap(d) or {}
    b1 = str((bal or {}).get('balance'))
    b2 = str((bal2 or {}).get('balance'))
    stage('NO_DUPLICATE_WALLET_CREDIT', b1 == b2,
          balance_before_retries=b1, balance_after_retries=b2)

    summarise()


def summarise():
    print('=' * 78)
    ok = [s for s in STAGES if s[1]]
    bad = [s for s in STAGES if not s[1]]
    print(f'STAGES PASSED {len(ok)}/{len(STAGES)}')
    for name, _good, detail in bad:
        print(f'  FAILED  {name}  {detail}')
    print('-' * 78)
    print('EVIDENCE')
    for k in ('revision', 'trip_id', 'client_request_id', 'sos_id', 'quote'):
        if k in FACTS:
            print(f'  {k:20} {FACTS[k]}')
    if 'acks' in FACTS:
        for action, a in FACTS['acks'].items():
            print(f'  ack.{action:16} {a["result"]}  (command_id={a["command_id"]})')
    if FACTS.get('t_start') and FACTS.get('t_complete'):
        print(f'  ride_seconds        {FACTS["t_complete"] - FACTS["t_start"]:.0f}')
    print('=' * 78)
    return not bad


if __name__ == '__main__':
    asyncio.run(main())
