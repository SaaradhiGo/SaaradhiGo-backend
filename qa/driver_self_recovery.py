"""Let a returning driver finish a ride that was left abandoned. No shell, no SQL.

WHY THIS SCRIPT EXISTS
----------------------
The Branch B stale-ride drill deliberately leaves its trip `in_progress` and hands it
to an operator, because an automatic terminal transition is the wrong answer and
inventing one would hide the gap rather than prove it is handled. That is correct
behaviour and the drill says so.

The consequence showed up immediately: with no operator account in QA, trip 49 stayed
`in_progress`, its driver stayed out of supply, and the next acceptance ride was
refused with "You are already on an active ride." The operator gap is not theoretical
-- it blocked QA within the hour.

So this is the OTHER recovery path, the one that needs nobody: the driver comes back
and finishes the ride themselves, through the same WebSocket their app uses. It is
Branch A of the abandoned-ride drill, applied to a specific stuck trip.

WHAT IT DELIBERATELY IS NOT
---------------------------
Not a cleanup tool, and not a substitute for the operator queue. It cannot cancel, it
cannot touch money, and it cannot act on a trip that is not the driver's own -- the
backend refuses all three, which is the point. If a driver never returns, this script
cannot help and an operator is still required. That is the gap, unchanged.

    ACCEPTANCE_DRIVER="+91...:OTP" python qa/driver_self_recovery.py
"""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

import websockets

B = os.environ.get('ACCEPTANCE_BASE_URL',
                   'https://backend-qa-811d.up.railway.app')
WSB = B.replace('https://', 'wss://').replace('http://', 'ws://')


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


def _pair(name):
    raw = os.environ.get(name, '')
    if ':' not in raw:
        print(f'{name} must be set as "+91XXXXXXXXXX:OTP"')
        raise SystemExit(2)
    phone, otp = raw.split(':', 1)
    return phone.strip(), otp.strip()


def login(phone, otp, role):
    req('POST', '/api/v1/auth/otp/', {'phone_number': phone, 'role': role})
    c, d = req('POST', '/api/v1/auth/login/',
               {'phone_number': phone, 'otp': otp, 'role': role})
    return (d.get('data') or {}).get('token'), c


async def recv(ws, timeout):
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))
    except Exception:  # noqa: BLE001
        return None


async def main():
    phone, otp = _pair('ACCEPTANCE_DRIVER')
    dtok, code = login(phone, otp, 'driver')
    if not dtok:
        print(f'driver login failed: HTTP {code}')
        return 1
    print('driver authenticated')

    # The durable active trip, from PostgreSQL. This is the same call the driver app
    # makes on relaunch, and it is what makes self-recovery possible at all.
    c, d = req('GET', '/api/v1/ride/active/', tok=dtok)
    body = (d.get('data') if isinstance(d, dict) else None) or {}
    trip_id = body.get('id')
    status = body.get('status')
    if not trip_id:
        print(f'no active trip for this driver (HTTP {c}). Nothing to recover.')
        return 0
    print(f'recovered active trip {trip_id}, status {status}')

    tws = await websockets.connect(f'{WSB}/ws/ride/trip/{trip_id}/?token={dtok}',
                                  open_timeout=30, ping_interval=20,
                                  close_timeout=10)
    await recv(tws, 8)

    async def act(action, **kw):
        cid = f'{action}-{uuid.uuid4().hex[:10]}'
        await tws.send(json.dumps({'action': action, 'command_id': cid, **kw}))
        for _ in range(10):
            m = await recv(tws, 15)
            if not m:
                break
            if m.get('type') == 'command_ack' and m.get('command_id') == cid:
                # The field is `status`, not `result`.
                return m.get('status'), m.get('trip_status')
            if m.get('type') == 'error':
                return 'error:' + str(m.get('message'))[:70], None
        return None, None

    # Only the transitions a driver legitimately owns, in order. Whatever the trip
    # has already passed answers `already_done` and costs nothing.
    for action in ('reached', 'complete', 'confirm_cash'):
        ack, st = await act(action)
        print(f'  {action:<14} ack={ack} status={st}')

    await tws.close()

    c, d = req('GET', f'/api/v1/ride/trip/{trip_id}/', tok=dtok)
    final = (d.get('data') if isinstance(d, dict) else None) or {}
    print(f'\ntrip {trip_id} final status: {final.get("status")}')
    print(f'final_fare: {final.get("final_fare")!r}  '
          f'(must stay None -- recovery charges nobody)')

    # Whether the driver is free is NOT "does /ride/active/ return something".
    #
    # That endpoint deliberately falls back to a trip COMPLETED within the last hour,
    # so an app killed after a ride can still show its rating and payment screen. An
    # earlier version of this script read that fallback as "still busy" and reported
    # a defect that does not exist.
    #
    # The real question is whether the trip is in a status that blocks a new
    # assignment -- which is what DRIVER_ACTIVE_TRIP_STATUSES means and what the
    # accept gate actually consults.
    blocking = {'requested', 'accepted', 'reached', 'in_progress'}
    c, d = req('GET', '/api/v1/ride/active/', tok=dtok)
    still = (d.get('data') if isinstance(d, dict) else None) or {}
    blocked = still.get('status') in blocking
    print(f'driver blocked from a new ride: {blocked}   '
          f'(status of whatever /ride/active/ returned: {still.get("status")!r})')
    print('  False means supply is restored -- a completed trip showing here is the '
          'post-ride screen, not a block.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
