"""A suspended driver gets no rides, even with a socket still open.

Suspension writes approved=False and status='blocked' and drops the driver from
the geo index, but an open driver socket kept pinging: the next location frame
re-added them and they were offered rides until they reconnected (accepting was
refused). Three layers now close that, and each is pinned here:

* suspend closes the driver's open socket server-side (code 4004);
* the location handler never (re-)indexes a driver who is not dispatchable;
* candidate selection excludes non-dispatchable drivers even if the geo index
  still holds them.

Reinstatement changes nothing special: the driver reconnects and is matched.
"""

import asyncio
import json
from decimal import Decimal

import pytest
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model

import servers.redis_client as rc
from base.asgi import application
from servers.driver.admin_drivers import operator_reinstate, operator_suspend
from servers.driver.models import Driver, DriverSuspension, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus

User = get_user_model()

LAT, LNG = Decimal('17.4450'), Decimal('78.3800')
VT = 'sedan'


def _make_driver(suffix):
    vt, _ = VehicleType.objects.get_or_create(type=VT)
    u = User.objects.create_user(phone_number=f'+9198400{suffix:05d}', role='driver')
    d = Driver.objects.create(id=7000 + suffix, user_id=u, approved=True, status='active')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number=f'TS09SU{suffix:04d}')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


def _token(user):
    from rest_framework_simplejwt.tokens import AccessToken

    return str(AccessToken.for_user(user))


def _indexed(driver_id):
    key = rc.geo_key_for(VT)
    return rc.redis_client.zscore(key, f'driver:{driver_id}:{VT}') is not None


def _skip_without_redis():
    if rc.redis_client is None:
        pytest.skip('Redis not available')
    try:
        rc.redis_client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip('Redis not available')


@pytest.fixture(autouse=True)
def _clean_redis():
    _skip_without_redis()
    yield


async def _connect(driver):
    tok = await asyncio.to_thread(_token, driver.user_id)
    comm = WebsocketCommunicator(
        application, f'/ws/driver/location/?token={tok}&lat={LAT}&lng={LNG}')
    connected, code = await comm.connect(timeout=20)
    assert connected, f'connect refused: {code}'
    await comm.receive_from(timeout=10)  # connection_established
    return comm


async def _wait_indexed(driver_id, timeout=10):
    """connect() accepts first and indexes afterwards; wait for it."""
    for _ in range(int(timeout * 10)):
        if await asyncio.to_thread(_indexed, driver_id):
            await asyncio.sleep(0.5)  # group_add follows the index write
            return True
        await asyncio.sleep(0.1)
    return False


async def _assert_not_indexed(driver_id, timeout=5):
    """Disconnect cleanup is asynchronous; allow it to land, then require absence."""
    for _ in range(int(timeout * 10)):
        if not await asyncio.to_thread(_indexed, driver_id):
            return
        await asyncio.sleep(0.1)
    members = await asyncio.to_thread(
        rc.redis_client.zrange, rc.geo_key_for(VT), 0, -1)
    raise AssertionError(f'driver {driver_id} still indexed; members={members}')


async def _expect_close(comm, timeout=10):
    """Drain frames until the socket's close arrives; return its code."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        msg = await asyncio.wait_for(comm.output_queue.get(), timeout=timeout)
        if msg.get('type') == 'websocket.close':
            return msg.get('code')
    return None


# ---------------------------------------------------------------------------
# Sockets (need a live channel layer, as the other socket tests do)
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_suspend_closes_a_connected_drivers_socket():
    driver = await asyncio.to_thread(_make_driver, 1)
    comm = await _connect(driver)
    assert await _wait_indexed(driver.id)

    outcome, _ = await asyncio.to_thread(
        operator_suspend, driver.id, reason_code='safety')
    assert outcome == 'suspended'

    assert await _expect_close(comm) == 4004
    await asyncio.sleep(0.5)
    await _assert_not_indexed(driver.id)
    # Closing the socket must not lift the block (disconnect used to write 'off').
    fresh = await asyncio.to_thread(Driver.objects.get, pk=driver.pk)
    assert fresh.status == 'blocked' and fresh.approved is False


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_ping_after_suspension_does_not_reindex_the_driver():
    driver = await asyncio.to_thread(_make_driver, 2)
    comm = await _connect(driver)
    assert await _wait_indexed(driver.id)

    # Suspend in the database only, so the socket stays open and the next
    # ping is the only thing standing between the driver and the index.
    def _block_quietly():
        Driver.objects.filter(pk=driver.pk).update(approved=False, status='blocked')
        rc.remove_driver(driver.id)
    await asyncio.to_thread(_block_quietly)
    await _assert_not_indexed(driver.id)

    await comm.send_to(text_data=json.dumps({'lat': 17.4455, 'lng': 78.3805}))
    assert await _expect_close(comm) == 4004
    await _assert_not_indexed(driver.id)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_reinstated_driver_reconnects_and_is_indexed_again():
    driver = await asyncio.to_thread(_make_driver, 3)
    comm = await _connect(driver)
    assert await _wait_indexed(driver.id)
    await asyncio.to_thread(operator_suspend, driver.id, reason_code='conduct')
    assert await _expect_close(comm) == 4004

    tok = await asyncio.to_thread(_token, driver.user_id)
    refused = WebsocketCommunicator(
        application, f'/ws/driver/location/?token={tok}&lat={LAT}&lng={LNG}')
    connected, code = await refused.connect(timeout=20)
    assert not connected and code == 4004

    outcome, _ = await asyncio.to_thread(operator_reinstate, driver.id)
    assert outcome == 'reinstated'
    # Reinstatement returns the driver to the KYC gate unless the document
    # checklist passes (not the case for this bare fixture); re-approving is the
    # console's normal step and is not what is under test, so do it directly.
    await asyncio.to_thread(
        Driver.objects.filter(pk=driver.pk).update, approved=True)
    fresh = await asyncio.to_thread(Driver.objects.get, pk=driver.pk)
    assert fresh.status != 'blocked'

    comm2 = await _connect(driver)
    assert await _wait_indexed(driver.id)
    await comm2.disconnect()


# ---------------------------------------------------------------------------
# Matching (candidate selection)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_matching_skips_a_suspended_driver_even_if_the_index_still_holds_them():
    from servers.ride.dispatch import _candidate_driver_ids

    rider = User.objects.create_user(phone_number='+919840099999', role='rider')
    vt, _ = VehicleType.objects.get_or_create(type=VT)
    good, bad = _make_driver(10), _make_driver(11)
    status, _ = TripStatus.objects.get_or_create(status_code='requested')
    trip = Trip.objects.create(
        user_id=rider, status_id=status, requested_vehicle_type=vt,
        pickup_lat=LAT, pickup_long=LNG,
        destination_lat=Decimal('17.4500'), destination_long=Decimal('78.4000'),
        pickup_address='P', destination_address='D', estimated_fare=Decimal('100.00'))

    key = rc.geo_key_for(VT)
    members = []
    for d in (good, bad):
        m = f'driver:{d.id}:{VT}'
        members.append((d, m))
        rc.redis_client.geoadd(key, [float(LNG), float(LAT), m])
        rc.redis_client.setex(f'{rc.HEARTBEAT_PREFIX}{d.id}', 60, '1')
    try:
        assert set(_candidate_driver_ids(trip, 5000, 1)) == {str(good.id), str(bad.id)}

        operator_suspend(bad.id, reason_code='fraud')
        # Put them back in the index, as a racing ping would have.
        rc.redis_client.geoadd(key, [float(LNG), float(LAT), f'driver:{bad.id}:{VT}'])
        rc.redis_client.setex(f'{rc.HEARTBEAT_PREFIX}{bad.id}', 60, '1')
        assert _candidate_driver_ids(trip, 5000, 1) == [str(good.id)]
        assert DriverSuspension.objects.filter(driver=bad, lifted_at__isnull=True).exists()
    finally:
        for d, m in members:
            rc.redis_client.zrem(key, m)
            rc.redis_client.delete(f'{rc.HEARTBEAT_PREFIX}{d.id}')
