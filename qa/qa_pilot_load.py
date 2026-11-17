"""Hyderabad pilot-size load on QA (launch round 4, 16 Nov 2026).

200 simulated drivers stream GPS every 4 s; every WAVE_SECONDS all 50 riders quote and book at once;
a bot driver accepts each load trip and the rider cancels inside the free window (no money moves).
Tokens come from a mode-600 file (qa_pilot_seed.py); nothing secret is printed.

    python qa/qa_pilot_load.py <tokens file> <minutes> <out.json>

Seed with qa/qa_pilot_seed.py and clean up with qa/qa_pilot_cleanup.py (PHASE dry, then apply), both
in `manage.py shell` on QA. qa/qa_pilot_monitor.py samples Postgres and Redis during a run.
"""
import asyncio
import json
import random
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict

import websockets

API = 'https://backend-qa-811d.up.railway.app/api/v1'
WSB = 'wss://backend-qa-811d.up.railway.app'
WAVE_SECONDS = 180
GPS_EVERY = 4.0
BOX = (17.370, 17.430, 78.520, 78.580)   # east Hyderabad (Uppal), away from the HITEC City test area

TOK = {'d': {}, 'r': {}}
for line in open(sys.argv[1], encoding='utf-8'):
    p = line.split()
    if len(p) == 6 and p[0] == 'LTTOK':
        TOK[p[1]][int(p[2])] = {'uid': int(p[3]), 'did': int(p[4]), 'token': p[5]}
import os
for k, n in (('d', int(os.environ.get('LT_D', 200))), ('r', int(os.environ.get('LT_R', 50)))):
    TOK[k] = {i: v for i, v in TOK[k].items() if i < n}
WAVE_SECONDS = int(os.environ.get('LT_WAVE', WAVE_SECONDS))
MINUTES = float(sys.argv[2])
OUT = sys.argv[3]

m = defaultdict(list)          # metric -> seconds
c = Counter()                  # outcomes and errors
errors = Counter()             # error kind -> count
load_trips = set()             # trip ids booked by our riders
busy = set()                   # driver idx currently holding an accepted trip
SSL = ssl.create_default_context()
STOP = None


def now():
    return time.perf_counter()


def http(method, path, tok, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    rq = urllib.request.Request(f'{API}{path}', data=data, method=method, headers={
        'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json',
        'User-Agent': 'sg-loadtest/1116'})
    t = now()
    try:
        with urllib.request.urlopen(rq, timeout=timeout, context=SSL) as r:
            return r.status, json.loads(r.read() or b'{}'), now() - t
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b'{}')
        except ValueError:
            payload = {}
        return e.code, payload, now() - t
    except Exception as e:  # noqa: BLE001
        return 0, {'exc': type(e).__name__}, now() - t


async def ahttp(*a, **kw):
    return await asyncio.to_thread(http, *a, **kw)


async def wsurl(path, tok, extra=''):
    """A single-use socket ticket (16 Nov: socket addresses never carry the token)."""
    st, body, dt = await ahttp('POST', '/auth/ws-ticket/', tok, {})
    m['ws_ticket_http'].append(dt)
    if st != 201:
        errors[f'ws_ticket_{st}'] += 1
        raise RuntimeError('no ticket')
    return f"{WSB}{path}?ticket={body['data']['ticket']}{extra}"


def point():
    return random.uniform(BOX[0], BOX[1]), random.uniform(BOX[2], BOX[3])


async def driver(idx):
    tok = TOK['d'][idx]['token']
    lat, lng = point()
    while now() < STOP:
        try:
            async with websockets.connect(await wsurl('/ws/driver/location/', tok, f'&lat={lat}&lng={lng}'),
                                          open_timeout=40, ping_interval=20, close_timeout=5,
                                          max_queue=64) as ws:
                c['driver_connects'] += 1
                last = 0.0
                while now() < STOP:
                    if now() - last >= GPS_EVERY:
                        lat += random.uniform(-0.0004, 0.0004)
                        lng += random.uniform(-0.0004, 0.0004)
                        await ws.send(json.dumps({'lat': lat, 'lng': lng}))
                        c['gps_sent'] += 1
                        last = now()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, GPS_EVERY - (now() - last)))
                    except asyncio.TimeoutError:
                        continue
                    msg = json.loads(raw)
                    t = msg.get('type')
                    if t == 'location_updated':
                        c['gps_acked'] += 1
                    elif t == 'error':
                        errors[f"driver_ws_error:{str(msg.get('message'))[:40]}"] += 1
                    elif t == 'ride_request':
                        tid = msg.get('trip_id')
                        c['offers_seen'] += 1
                        if tid in BOOKED_AT and tid not in FIRST_OFFER:
                            FIRST_OFFER[tid] = now()
                            m['book_to_first_offer'].append(now() - BOOKED_AT[tid])
                        if tid in load_trips and idx not in busy:
                            asyncio.create_task(accept(idx, tid))
        except websockets.ConnectionClosed as e:
            if now() < STOP:
                c['driver_drops'] += 1
                errors[f'driver_close:{e.rcvd.code if e.rcvd else "none"}'] += 1
        except Exception as e:  # noqa: BLE001
            if now() < STOP:
                c['driver_drops'] += 1
                errors[f'driver_exc:{type(e).__name__}'] += 1
        await asyncio.sleep(5)


ACCEPT_AT = {}
BOOKED_AT = {}
FIRST_OFFER = {}


async def accept(idx, tid):
    busy.add(idx)
    won = False
    await asyncio.sleep(random.uniform(0.5, 2.5))
    tok = TOK['d'][idx]['token']
    try:
        async with websockets.connect(await wsurl(f'/ws/ride/trip/{tid}/', tok), open_timeout=40,
                                      close_timeout=5) as ws:
            cid = f'accept-{uuid.uuid4().hex[:10]}'
            t = now()
            await ws.send(json.dumps({'action': 'accept', 'command_id': cid}))
            end = now() + 30
            while now() < end:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=end - now()))
                if msg.get('type') == 'command_ack' and msg.get('command_id') == cid:
                    m['accept_ack'].append(now() - t)
                    if msg.get('status') in ('committed', 'already_done'):
                        won = True
                        c['accepted'] += 1
                        ACCEPT_AT.setdefault(tid, now())
                    else:
                        c[f"accept_{msg.get('status')}"] += 1
                    break
                if msg.get('type') == 'trip_taken':
                    c['accept_lost_race'] += 1
                    break
    except websockets.ConnectionClosed as e:
        c['accept_closed'] += 1
        errors[f'trip_ws_close:{e.rcvd.code if e.rcvd else "none"}'] += 1
    except websockets.InvalidStatus as e:
        c[f'accept_refused_{e.response.status_code}'] += 1   # the trip went to another driver first
    except Exception as e:  # noqa: BLE001
        errors[f'accept_exc:{type(e).__name__}'] += 1
    if won:
        await asyncio.sleep(25)      # holds the trip until the rider cancels
    busy.discard(idx)


async def rider_wave(idx, wave):
    tok = TOK['r'][idx]['token']
    lat, lng = point()
    dlat, dlng = lat + 0.03, lng + 0.01
    t0 = now()
    st, body, dt = await ahttp('POST', '/ride/estimate-fare/', tok, {
        'pickup_lat': lat, 'pickup_long': lng, 'destination_lat': dlat, 'destination_long': dlng,
        'distance_km': 4.2, 'duration_min': 14, 'vehicle_type': 'sedan'})
    m['quote_http'].append(dt)
    if st != 200:
        errors[f'quote_http_{st}:{json.dumps(body)[:60]}'] += 1
        return
    quote_id = (body.get('data') or {}).get('quote_id')
    try:
        url = await wsurl('/ws/ride/request/', tok)
        tc = now()
        async with websockets.connect(url, open_timeout=40, close_timeout=5) as ws:
            m['rider_ws_connect'].append(now() - tc)
            t1 = now()
            await ws.send(json.dumps({
                'pickup_lat': lat, 'pickup_lng': lng, 'destination_lat': dlat, 'destination_lng': dlng,
                'pickup_address': 'LT1116 pickup', 'destination_address': 'LT1116 drop',
                'distance_km': 4.2, 'duration_min': 14, 'vehicle_type': 'sedan',
                'payment_method': 'cash', 'quote_id': quote_id,
                'client_request_id': f'lt1116-{wave}-{idx}-{uuid.uuid4().hex[:8]}'}))
            tid = None
            end = now() + 60
            while now() < end:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=end - now()))
                if msg.get('type') == 'trip_created':
                    tid = msg.get('trip_id')
                    load_trips.add(tid)
                    BOOKED_AT[tid] = t1
                    m['booking_ws'].append(now() - t1)
                    m['booking_total'].append(now() - t0)
                    c['booked'] += 1
                    break
                if msg.get('type') == 'error':
                    errors[f"book_error:{str(msg.get('code') or msg.get('message'))[:50]}"] += 1
                    return
            if tid is None:
                c['book_timeout'] += 1
                return
            end = now() + 60
            while now() < end and tid not in ACCEPT_AT:
                await asyncio.sleep(0.25)
            if tid in ACCEPT_AT:
                m['book_to_driver'].append(ACCEPT_AT[tid] - t1)
                c['matched'] += 1
            else:
                c['unmatched_60s'] += 1
    except websockets.ConnectionClosed as e:
        c['rider_drops'] += 1
        errors[f'rider_close:{e.rcvd.code if e.rcvd else "none"}'] += 1
        return
    except Exception as e:  # noqa: BLE001
        errors[f'rider_exc:{type(e).__name__}'] += 1
        return
    await asyncio.sleep(random.uniform(3, 8))
    st, body, dt = await ahttp('POST', f'/ride/trip/{tid}/cancel/', tok, {'reason': 'other', 'note': 'LT1116 load test'})
    m['cancel_http'].append(dt)
    c[f'cancel_{st}'] += 1


def pct(xs, q):
    if len(xs) < 20:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1))))], 3)


def summary():
    return {
        'metrics': {k: {'n': len(v), 'p50': pct(v, 50), 'p95': pct(v, 95), 'p99': pct(v, 99),
                        'max': round(max(v), 3) if v else None,
                        'mean': round(statistics.mean(v), 3) if v else None} for k, v in m.items()},
        'counts': dict(c), 'errors': dict(errors.most_common(30))}


async def main():
    global STOP
    STOP = now() + MINUTES * 60
    drivers = [asyncio.create_task(driver(i)) for i in sorted(TOK['d'])]
    await asyncio.sleep(30)                      # let every driver connect and index
    wave = 0
    while now() + 90 < STOP:
        wave += 1
        print(f'wave {wave} at {time.strftime("%H:%M:%S")} drivers_connected={c["driver_connects"]} drops={c["driver_drops"]}', flush=True)
        await asyncio.gather(*(rider_wave(i, wave) for i in sorted(TOK['r'])))
        json.dump(summary(), open(OUT, 'w'), indent=1)
        await asyncio.sleep(max(0, WAVE_SECONDS - 60))
    await asyncio.gather(*drivers, return_exceptions=True)
    json.dump(summary(), open(OUT, 'w'), indent=1)
    print(json.dumps(summary()['counts']), flush=True)


from maps_guard import require_fake_maps  # noqa: E402

require_fake_maps(API)
asyncio.run(main())
