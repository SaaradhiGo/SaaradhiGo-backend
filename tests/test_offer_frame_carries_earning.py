"""The driver's socket forwards the offer's earning fields (2026-11-06).

`DriverLocationConsumer.ride_request` rebuilds the frame key by key, so the four fields
dispatch adds (earning, commission_rate, commission_amount, rider_rating) were
dropped before they reached the app. Checked at the socket boundary.
"""

import asyncio
import json

from servers.consumers import DriverLocationConsumer


def test_the_socket_frame_carries_the_earning_fields():
    sent = []
    consumer = DriverLocationConsumer.__new__(DriverLocationConsumer)

    async def fake_send(text_data=None, **_):
        sent.append(json.loads(text_data))

    consumer.send = fake_send
    event = {
        'type': 'ride_request', 'trip_id': 9, 'pickup_lat': '17.4', 'pickup_lng': '78.3',
        'destination_lat': '17.5', 'destination_lng': '78.4', 'estimated_fare': '100',
        'earning': '85.00', 'commission_rate': '15.00', 'commission_amount': '15.00',
        'rider_rating': 4.6,
    }
    asyncio.run(consumer.ride_request(event))
    frame = sent[0]
    assert frame['earning'] == '85.00'
    assert frame['commission_rate'] == '15.00'
    assert frame['commission_amount'] == '15.00'
    assert frame['rider_rating'] == 4.6


def test_an_offer_without_them_sends_none():
    sent = []
    consumer = DriverLocationConsumer.__new__(DriverLocationConsumer)

    async def fake_send(text_data=None, **_):
        sent.append(json.loads(text_data))

    consumer.send = fake_send
    asyncio.run(consumer.ride_request({'trip_id': 1, 'pickup_lat': '1', 'pickup_lng': '1',
                                       'destination_lat': '1', 'destination_lng': '1'}))
    assert sent[0]['earning'] is None and sent[0]['rider_rating'] is None
