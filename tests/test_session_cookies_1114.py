"""14 Nov (owner): website login keys move into HttpOnly cookies held by the web's own server.

Two things the backend needs for that:
  * the web's server makes the signed-in calls, so it vouches for the visitor's address with a
    shared secret, and only then is that address believed (rate limits, login lockout, audit);
  * a page with no token opens its ride socket with a single-use ticket instead of `?token=`.
"""
import asyncio

import pytest
from django.test import RequestFactory, override_settings
from rest_framework.test import APIClient

from base.client_ip import client_ip
from servers.redis_client import redis_client

SECRET = 'x' * 40
VISITOR = '81.2.69.142'
WEB_SERVER = '152.233.76.9'


def _req(**meta):
    return RequestFactory().get('/', REMOTE_ADDR='10.0.0.5', HTTP_X_FORWARDED_FOR=WEB_SERVER, **meta)


class TestVouchedAddress:
    @override_settings(BFF_CLIENT_IP_SECRET=SECRET)
    def test_the_web_server_vouches_with_the_secret(self):
        assert client_ip(_req(HTTP_X_SG_CLIENT_IP=VISITOR, HTTP_X_SG_CLIENT_IP_KEY=SECRET)) == VISITOR

    @override_settings(BFF_CLIENT_IP_SECRET=SECRET)
    def test_a_wrong_or_missing_key_is_ignored(self):
        assert client_ip(_req(HTTP_X_SG_CLIENT_IP='6.6.6.6', HTTP_X_SG_CLIENT_IP_KEY='guess')) == WEB_SERVER
        assert client_ip(_req(HTTP_X_SG_CLIENT_IP='6.6.6.6')) == WEB_SERVER

    @override_settings(BFF_CLIENT_IP_SECRET='')
    def test_no_secret_configured_believes_nobody(self):
        assert client_ip(_req(HTTP_X_SG_CLIENT_IP='6.6.6.6', HTTP_X_SG_CLIENT_IP_KEY='')) == WEB_SERVER

    @override_settings(BFF_CLIENT_IP_SECRET='short')
    def test_a_short_secret_is_not_used(self):
        assert client_ip(_req(HTTP_X_SG_CLIENT_IP='6.6.6.6', HTTP_X_SG_CLIENT_IP_KEY='short')) == WEB_SERVER


needs_redis = pytest.mark.skipif(redis_client is None, reason='tickets live in Redis')


@pytest.mark.django_db(transaction=True)
@needs_redis
class TestRideSocketTicket:
    def _user(self):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.create_user(
            email='ticket-rider@example.test', phone_number='+15550001114', password='pass-word-1', role='rider')

    def test_a_signed_in_rider_gets_a_ticket_that_opens_the_socket_once(self):
        from servers.ws_middleware import get_user_from_ticket

        user = self._user()
        api = APIClient()
        api.force_authenticate(user)
        r = api.post('/api/v1/auth/ws-ticket/', {}, format='json')
        assert r.status_code == 201, r.content
        ticket = (r.json().get('data') or r.json())['ticket']
        first = asyncio.run(get_user_from_ticket(ticket))
        again = asyncio.run(get_user_from_ticket(ticket))
        assert first.id == user.id
        assert not again.is_authenticated

    def test_signed_out_gets_none(self):
        assert APIClient().post('/api/v1/auth/ws-ticket/', {}, format='json').status_code == 401

    def test_a_console_ticket_does_not_open_a_ride_socket(self):
        from servers.live_ops.tickets import issue_ticket
        from servers.ws_middleware import get_user_from_ticket

        user = self._user()
        console_ticket = issue_ticket(user.id)
        assert not asyncio.run(get_user_from_ticket(console_ticket)).is_authenticated
