"""The client IP comes from the trusted end of the proxy chain (AUDIT 27.78, M1).

Every reader used to take the FIRST X-Forwarded-For entry, which the client
writes. These tests send a forged left-most entry, as an attacker would, with
the real address appended the way Railway's edge appends it.
"""

import pytest
from django.test import RequestFactory

from base.client_ip import client_ip
from servers.admin_dashboard import login_guard

REAL = '81.2.69.142'


def _req(xff=None, remote='10.0.0.5'):
    meta = {'REMOTE_ADDR': remote}
    if xff is not None:
        meta['HTTP_X_FORWARDED_FOR'] = xff
    return RequestFactory().get('/', **meta)


class TestTheHelper:
    def test_a_spoofed_leftmost_entry_does_not_change_the_ip(self):
        assert client_ip(_req(f'6.6.6.6, {REAL}')) == REAL
        assert client_ip(_req(f'1.1.1.1, 2.2.2.2, {REAL}')) == REAL

    def test_no_header_falls_back_to_remote_addr(self):
        assert client_ip(_req(None, remote=REAL)) == REAL

    def test_internal_platform_hops_on_the_right_are_skipped(self):
        assert client_ip(_req(f'6.6.6.6, {REAL}, 10.1.2.3, 100.64.0.9')) == REAL

    def test_garbage_and_ports_are_handled(self):
        assert client_ip(_req(f'not-an-ip, {REAL}:51234')) == REAL
        assert client_ip(_req('[2606:4700::1111]:443')) == '2606:4700::1111'

    def test_a_second_trusted_proxy_moves_one_hop_left(self, settings):
        settings.TRUSTED_PROXY_COUNT = 2
        assert client_ip(_req(f'6.6.6.6, {REAL}, 81.2.69.20')) == REAL

    def test_a_meta_dict_is_accepted(self):
        assert client_ip({'HTTP_X_FORWARDED_FOR': f'6.6.6.6, {REAL}',
                          'REMOTE_ADDR': '10.0.0.5'}) == REAL


class TestTheReaders:
    def test_audit_rows_record_the_trusted_address(self):
        from servers.admin_audit.services import _client_ip
        assert _client_ip(_req(f'6.6.6.6, {REAL}')) == REAL

    def test_sos_records_the_trusted_address(self):
        from servers.sos.views import _client_ip
        assert _client_ip(_req(f'6.6.6.6, {REAL}')) == REAL

    def test_webhook_logs_the_trusted_address(self):
        from servers.notifications.webhooks import _client_address
        assert _client_address(_req(f'6.6.6.6, {REAL}')) == REAL

    def test_guest_throttle_keys_on_the_trusted_address(self):
        from base.throttles import GuestMapsThrottle
        t = GuestMapsThrottle()
        assert t.get_ident(_req(f'6.6.6.6, {REAL}')) == t.get_ident(_req(f'7.7.7.7, {REAL}'))

    def test_app_config_throttle_keys_on_the_trusted_address(self):
        from servers.platform_ops.views import AppConfigThrottle
        t = AppConfigThrottle()
        a = t.get_cache_key(_req(f'6.6.6.6, {REAL}'), None)
        b = t.get_cache_key(_req(f'7.7.7.7, {REAL}'), None)
        assert a == b and REAL in a


@pytest.mark.django_db
class TestTheLockoutHoldsAcrossSpoofing:
    def test_rotating_the_forged_entry_still_trips_the_per_ip_lockout(self, settings):
        settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 3
        settings.OPS_LOGIN_MAX_ATTEMPTS = 100
        from django.core.cache import cache
        cache.clear()
        for n in range(3):
            # A different phone and a different forged address every time:
            # only the per-IP counter can catch this, and it must.
            login_guard.record_failure(_req(f'9.9.9.{n}, {REAL}'), f'+9198700000{n:02d}')
        assert login_guard.is_locked(_req(f'8.8.8.8, {REAL}'), '+919870000099')
        # A different real client is not locked.
        assert not login_guard.is_locked(_req('8.8.8.8, 81.2.69.1'), '+919870000099')
