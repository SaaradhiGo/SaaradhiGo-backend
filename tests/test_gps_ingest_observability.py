"""GPS that stops arriving must not stop silently.

The trip's location trail is the evidence behind measured distance, disputes and
safety. Before this, a frame that failed to reach Redis produced one warning among
hundreds of identical ones and no aggregate anywhere, so "why did this ride's GPS
trail stop?" had no answer -- which is precisely the question that took a day to
answer during the pilot-blocker investigation.

Two integers per connection, one summary line at disconnect, and one warning the
first time a frame is refused. No per-frame logging, no coordinates.
"""

import asyncio
import json
import logging
from decimal import Decimal

import pytest
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model

from base.asgi import application
from servers.driver.models import Driver, Vehicle, VehicleType

User = get_user_model()

LAT, LNG = 17.4450000, 78.3800000

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio,
              pytest.mark.django_db(transaction=True)]


def _make_driver(suffix, with_vehicle=True):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    u = User.objects.create_user(phone_number=f'+9198300{suffix:05d}', role='driver')
    d = Driver.objects.create(user_id=u, approved=True, status='active')
    if with_vehicle:
        v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                                   vehicle_number=f'TS09OB{suffix:04d}')
        d.active_vehicle = v
        d.save(update_fields=['active_vehicle'])
    return d


def _token(user):
    from rest_framework_simplejwt.tokens import AccessToken

    return str(AccessToken.for_user(user))


async def _open_driver_socket(driver_user):
    tok = await asyncio.to_thread(_token, driver_user)
    comm = WebsocketCommunicator(
        application, f'/ws/driver/location/?token={tok}&lat={LAT}&lng={LNG}')
    connected, code = await comm.connect(timeout=20)
    if not connected:
        return None, code
    await comm.receive_from(timeout=10)
    return comm, None


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


def _events(caplog, name):
    return [r.__dict__ for r in caplog.records
            if getattr(r, 'event', None) == name]


async def test_a_healthy_session_reports_its_accepted_frame_count(caplog):
    driver = await asyncio.to_thread(_make_driver, 1)
    comm, _ = await _open_driver_socket(driver.user_id)

    with caplog.at_level(logging.INFO, logger='servers.consumers'):
        for i in range(12):
            await comm.send_to(text_data=json.dumps(
                {'lat': LAT + 0.0005 * i, 'lng': LNG}))
        await asyncio.sleep(2)
        _pending(comm)
        await comm.disconnect(timeout=5)
        await asyncio.sleep(0.5)

    summaries = _events(caplog, 'gps_session_summary')
    print(f'healthy session summaries: '
          f'{[(s["accepted"], s["rejected"]) for s in summaries]}')

    assert summaries, 'a session that carried GPS must report what happened to it'
    s = summaries[0]
    assert s['accepted'] == 12, s
    assert s['rejected'] == 0, s
    assert s['driver_id'] == driver.id
    # A healthy session is INFO, so it does not cry wolf in production.
    assert summaries[0]['levelname'] == 'INFO', summaries[0]['levelname']


async def test_a_refused_frame_warns_once_and_is_counted(caplog):
    """Out-of-range coordinates are refused by the ingest path.

    Chosen because it is deterministic. A driver with no active vehicle turned out
    to have its frames ACCEPTED (the vehicle type resolves from a Redis cache), so
    that state would have made this test pass or fail for reasons unrelated to the
    observability being tested.
    """
    driver = await asyncio.to_thread(_make_driver, 2)
    comm, code = await _open_driver_socket(driver.user_id)
    if comm is None:
        pytest.skip(f'this build refuses the socket outright (close {code}), '
                    'so there are no frames to count')

    with caplog.at_level(logging.INFO, logger='servers.consumers'):
        for _ in range(8):
            # Latitude beyond +/-90: refused by _validate_coordinates.
            await comm.send_to(text_data=json.dumps(
                {'lat': 999.0, 'lng': LNG}))
        await asyncio.sleep(2)
        errors = [m for m in _pending(comm) if m.get('type') == 'error']
        await comm.disconnect(timeout=5)
        await asyncio.sleep(0.5)

    rejects = _events(caplog, 'gps_frame_rejected')
    summaries = _events(caplog, 'gps_session_summary')
    print(f'refused: error_frames={len(errors)} first_warnings={len(rejects)} '
          f'summary={[(s["accepted"], s["rejected"]) for s in summaries]}')

    assert len(rejects) == 1, (
        f'expected exactly one first-failure warning per connection, got '
        f'{len(rejects)} -- one per frame would drown out the signal'
    )
    assert rejects[0]['reason'], 'the warning must say why'
    assert summaries, 'a session that lost frames must still summarise'
    assert summaries[0]['rejected'] == 8, summaries[0]
    assert summaries[0]['levelname'] == 'WARNING', (
        'lost GPS must be visible at production log level')
    assert 'first_reject_reason' in summaries[0]


async def test_the_summary_never_contains_a_coordinate(caplog):
    """Location data is sensitive. The observability must not leak it."""
    driver = await asyncio.to_thread(_make_driver, 3)
    comm, _ = await _open_driver_socket(driver.user_id)

    with caplog.at_level(logging.INFO, logger='servers.consumers'):
        await comm.send_to(text_data=json.dumps({'lat': LAT, 'lng': LNG}))
        await asyncio.sleep(1.5)
        _pending(comm)
        await comm.disconnect(timeout=5)
        await asyncio.sleep(0.5)

    summaries = _events(caplog, 'gps_session_summary')
    assert summaries
    blob = json.dumps({k: str(v) for k, v in summaries[0].items()})
    for forbidden in ('17.445', '78.38', str(LAT), str(LNG)):
        assert forbidden not in blob, (
            f'the GPS session summary leaked {forbidden!r}: {blob}')
    assert 'lat' not in summaries[0] and 'lng' not in summaries[0]


async def test_a_session_that_carried_no_gps_says_nothing(caplog):
    """Silence for an idle connection, or the signal becomes noise.

    Every driver opening the app would otherwise emit a summary.
    """
    driver = await asyncio.to_thread(_make_driver, 4)
    comm, _ = await _open_driver_socket(driver.user_id)

    with caplog.at_level(logging.INFO, logger='servers.consumers'):
        await comm.disconnect(timeout=5)
        await asyncio.sleep(0.5)

    assert not _events(caplog, 'gps_session_summary'), (
        'an idle connection must not emit a GPS summary')


def test_a_duplicate_driver_earning_is_logged_not_just_swallowed(caplog, db):
    """The ledger's idempotency guard was silent.

    `TRIP_<id>_EARNING` refusing a duplicate is what stops a driver being credited
    twice, so this branch is load-bearing. A rising rate means something upstream is
    retrying settlement more than expected -- worth knowing before it becomes a
    support question about a missing payment.
    """
    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Wallet, WalletTransaction

    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    duser = User.objects.create_user(phone_number='+919833000001', role='driver')
    driver = Driver.objects.create(user_id=duser, approved=True, status='active')
    Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt,
                           vehicle_number='TS09OB9999')
    rider = User.objects.create_user(phone_number='+919733000001', role='rider')
    st, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=st,
        pickup_lat=Decimal(str(LAT)), pickup_long=Decimal(str(LNG)),
        destination_lat=Decimal(str(LAT + 0.01)), destination_long=Decimal(str(LNG)),
        estimated_fare=Decimal('200.00'), payment_method='cash',
    )
    Wallet.objects.get_or_create(user_id=duser, defaults={'balance': Decimal('0')})

    # Pre-create the earning so the real call hits its own idempotency guard.
    WalletTransaction.objects.create(
        user_id=duser, amount=Decimal('10.00'), txn_type='credit',
        status='completed', purpose='trip earning',
        reference_id=f'TRIP_{trip.id}',
        idempotency_key=f'TRIP_{trip.id}_EARNING',
    )
    before = WalletTransaction.objects.filter(
        idempotency_key=f'TRIP_{trip.id}_EARNING').count()

    from servers.driver.utils import credit_driver_wallet as _credit
    with caplog.at_level(logging.INFO, logger='servers.driver.utils'):
        _credit(trip)

    after = WalletTransaction.objects.filter(
        idempotency_key=f'TRIP_{trip.id}_EARNING').count()
    events = [r.__dict__ for r in caplog.records
              if getattr(r, 'event', None) == 'driver_earning_duplicate_suppressed']
    print(f'duplicate earning: rows {before} -> {after}, logged={len(events)}')

    assert after == before == 1, 'the ledger must not gain a second earning row'
    assert events, 'the suppression must be visible to operations'
    assert events[0]['trip_id'] == trip.id
