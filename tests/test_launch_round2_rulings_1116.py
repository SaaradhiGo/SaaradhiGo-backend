"""Srini's 16 Nov rulings: a search nobody accepts releases the card hold; sockets sign in only
with a single-use ticket; credentials in a URL never reach the logs."""
import asyncio
import logging

import pytest
from rest_framework_simplejwt.tokens import AccessToken

from base.logging_filters import PIIRedactionFilter
from servers.payments.models import CardHold
from tests.test_usa_payments_1114 import FAKE, _quote, _trip, world  # noqa: F401

pytestmark = pytest.mark.django_db


@FAKE
def test_nobody_accepted_releases_the_hold(world):  # noqa: F811
    from servers.payments.card_hold import create_hold, link_to_trip
    from servers.ride.tasks import auto_cancel_trip

    hold, _ = create_hold(world['rider'], _quote(world['rider']))
    trip = _trip(world, status='requested')
    trip.driver_id = None
    trip.save(update_fields=['driver_id'])
    assert link_to_trip(world['rider'], hold.id, trip) is None
    auto_cancel_trip.apply(args=(trip.id,)).get()
    hold.refresh_from_db()
    assert hold.status == CardHold.RELEASED


def _scope_user(query):
    from servers.ws_middleware import JWTAuthMiddleware

    seen = {}

    async def inner(scope, receive, send):
        seen['user'] = scope['user']

    asyncio.run(JWTAuthMiddleware(inner)({'type': 'websocket', 'path': '/ws/ride/request/',
                                          'query_string': query.encode()}, None, None))
    return seen['user']


def test_a_token_in_the_socket_address_signs_nobody_in(world):  # noqa: F811
    token = str(AccessToken.for_user(world['rider']))
    assert not _scope_user(f'token={token}').is_authenticated


@pytest.mark.django_db(transaction=True)
def test_a_ticket_signs_the_socket_in(world):  # noqa: F811
    from tests.ws_auth import ws_q

    try:
        query = ws_q(AccessToken.for_user(world['rider']))
    except Exception:  # noqa: BLE001
        pytest.skip('tickets live in Redis')
    assert _scope_user(query) == world['rider']


def test_a_token_in_a_logged_url_is_redacted():
    record = logging.LogRecord('uvicorn.error', logging.INFO, __file__, 1,
                               '%s - "WebSocket %s" [accepted]',
                               ('1.2.3.4:0', '/ws/ride/request/?token=abc.def-ghi&lat=1'), None)
    PIIRedactionFilter().filter(record)
    assert 'abc.def-ghi' not in record.getMessage()
    assert '?token=***&lat=1' in record.getMessage()


@FAKE
def test_a_card_trip_with_no_hold_is_unpaid_and_pay_now_uses_the_saved_card(world):  # noqa: F811
    """Trip 226 (16 Nov): booked with no hold linked, it sat 'pending' with nobody asked to pay."""
    from servers.payments.card_hold import capture_for_trip, create_hold
    from servers.payments.unpaid import retry_card, unpaid_trip_for

    create_hold(world['rider'], _quote(world['rider']))   # the card saved by an earlier hold
    trip = _trip(world, status='completed')
    trip.payment_status = 'pending'
    trip.currency = 'USD'
    trip.save(update_fields=['payment_status', 'currency'])
    assert capture_for_trip(trip.id) == 'failed'
    assert unpaid_trip_for(world['rider']) == trip
    assert retry_card(trip, by_rider=True) == 'paid'
    trip.refresh_from_db()
    assert trip.payment_status == 'completed'
    assert unpaid_trip_for(world['rider']) is None
