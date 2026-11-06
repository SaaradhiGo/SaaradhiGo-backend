"""The rider learns when the trip started and how long it should take (2026-11-06).

The rider's on-trip screen says "Arriving about HH:MM" from `started_at` and
`estimated_duration_min`. Both exist on the trip but were never sent, and the
two socket handlers rebuild each frame key by key. Found on QA trip 136: the
screen fell back to "On the way to Inorbit Mall". Checked at the socket boundary.
"""

import asyncio
import json

from servers.consumers import RideRequestConsumer, TripStatusConsumer, _arrival_fields

START = {'started_at': '2026-11-06T09:03:50+00:00', 'estimated_duration_min': '6'}


def _capture(consumer):
    sent = []

    async def fake_send(text_data=None, **_):
        sent.append(json.loads(text_data))

    consumer.send = fake_send
    return sent


def test_the_riders_own_socket_carries_them_on_start():
    c = RideRequestConsumer.__new__(RideRequestConsumer)
    sent = _capture(c)
    asyncio.run(c.trip_update({'type': 'trip_update', 'trip_id': 136, 'status': 'start', **START}))
    assert sent[0]['started_at'] == START['started_at']
    assert sent[0]['estimated_duration_min'] == '6'


def test_the_trip_socket_carries_them_on_start():
    c = TripStatusConsumer.__new__(TripStatusConsumer)
    c.participation = 'rider'
    sent = _capture(c)
    asyncio.run(c.trip_status_update(
        {'type': 'trip_status_update', 'trip_id': 136, 'status': 'start', **START}))
    assert sent[0]['started_at'] == START['started_at']
    assert sent[0]['estimated_duration_min'] == '6'


def test_other_events_are_unchanged():
    c = RideRequestConsumer.__new__(RideRequestConsumer)
    sent = _capture(c)
    asyncio.run(c.trip_update({'type': 'trip_update', 'trip_id': 136, 'status': 'reached'}))
    assert 'started_at' not in sent[0]


def test_missing_values_are_left_out_not_null():
    assert _arrival_fields({'started_at': None, 'estimated_duration_min': '6'}) == {
        'estimated_duration_min': '6'}
