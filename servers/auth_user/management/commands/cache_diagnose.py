"""Non-destructive check of the cache a deployment is actually using.

WHY
---
On 2026-10-17 a live QA login failed like this, from the service's own log:

    00:03:22  OTP test-phone bypass for +9199*** - no SMS sent
    00:03:22  POST /api/v1/auth/otp/  200
    00:03:28  OTP expired or not found for: +9199***

An OTP written with a 600-second TTL was gone six seconds later, on a Redis
holding 12 MB of a 24 GB limit with no connection errors logged. It did not
reproduce minutes later. Reading the code ruled the code out: both views
derive `otp_role_<phone>` from the same field, and `/auth/login/` does route
to `auth_user.views.login`.

That leaves the cache itself, and the test suite cannot see it -- an autouse
fixture in `tests/conftest.py` swaps in LocMemCache, so every test passes
whatever Redis does. This command is the missing instrument.

WHAT IT DOES
------------
Writes one key, reads it back, re-reads it after a pause, then deletes it.
It reports the backend, a redacted location, the round-trip result and the
TTL the server actually applied.

SAFETY
------
  * It writes exactly one key, `cache_diagnose:<uuid4>`, and deletes it.
  * It never calls `cache.clear()` or scans the keyspace.
  * It never reads, prints or touches an OTP, a session or any user key.
  * It prints no credentials: the location is reduced to scheme, host and
    database index, with any password removed.

Run it where the problem is:

    railway run python manage.py cache_diagnose          # against QA's env
    python manage.py cache_diagnose --settle 10          # longer re-read
"""

import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand


def _redact_location(location):
    """Scheme, host, port and db index only. Credentials stripped.

    A Railway Redis URL carries its password in the netloc, and this command
    is meant to be run by an operator and pasted into a ticket.
    """
    if not location:
        return '(unset)'
    if isinstance(location, (list, tuple)):
        return ', '.join(_redact_location(item) for item in location)
    try:
        parts = urlsplit(str(location))
        host = parts.hostname or ''
        port = f':{parts.port}' if parts.port else ''
        netloc = f'{host}{port}'
        if parts.username or parts.password:
            netloc = f'<redacted>@{netloc}'
        return urlunsplit((parts.scheme, netloc, parts.path, '', ''))
    except Exception:  # noqa: BLE001
        return '(unparseable)'


class Command(BaseCommand):
    help = (
        'Write, read back and delete one throwaway cache key, and report what '
        'the configured cache did. Prints no credentials and no user data.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--settle',
            type=int,
            default=3,
            help='Seconds to wait before the second read. The QA failure had '
                 'a six-second gap, so the default re-reads after three and '
                 'the flag allows longer.',
        )
        parser.add_argument(
            '--ttl',
            type=int,
            default=600,
            help='TTL to set, in seconds. Defaults to 600 to match OTP_EXPIRY.',
        )

    def handle(self, *args, **options):
        settle = options['settle']
        ttl = options['ttl']

        config = settings.CACHES.get('default', {})
        self.stdout.write('--- cache configuration ---')
        self.stdout.write(f"backend  : {config.get('BACKEND', '(none)')}")
        self.stdout.write(
            f"location : {_redact_location(config.get('LOCATION'))}")
        self.stdout.write(f"key_prefix: {config.get('KEY_PREFIX') or '(none)'}")
        self.stdout.write(
            f"environment: {getattr(settings, 'ENVIRONMENT', '(unset)')}")

        key = f'cache_diagnose:{uuid.uuid4().hex}'
        marker = uuid.uuid4().hex

        self.stdout.write('\n--- round trip ---')
        try:
            cache.set(key, marker, ttl)
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(
                f'SET FAILED: {type(exc).__name__}: {exc}'))
            self.stdout.write(
                'A failing SET means every OTP request is silently issuing a '
                'code nobody can verify.')
            return

        immediate = cache.get(key)
        self.stdout.write(
            f'immediate read : {"OK" if immediate == marker else "MISMATCH"}')

        if immediate != marker:
            self.stdout.write(self.style.ERROR(
                'The value did not survive its own request. Nothing that '
                'depends on the cache can work: OTP login, throttling, the '
                'surge counters or the platform-settings cache.'))
            self._cleanup(key)
            return

        # The TTL the server actually applied, where the backend exposes it.
        # django_redis does; LocMemCache does not, and saying so is better
        # than printing a guess.
        ttl_report = 'not available for this backend'
        try:
            ttl_report = f'{cache.ttl(key)}s'  # django_redis only
        except Exception:  # noqa: BLE001
            pass
        self.stdout.write(f'server ttl     : {ttl_report} (requested {ttl}s)')

        self.stdout.write(f'waiting {settle}s ...')
        time.sleep(settle)

        delayed = cache.get(key)
        if delayed == marker:
            self.stdout.write(self.style.SUCCESS(
                f'read after {settle}s : OK'))
            self.stdout.write(
                '\nThe cache held a value across the window in which the QA '
                'OTP vanished. If a login still fails with '
                '"OTP has expired", the cause is not this cache instance.')
        else:
            self.stdout.write(self.style.ERROR(
                f'read after {settle}s : GONE (got {delayed!r})'))
            self.stdout.write(
                '\nThis reproduces the QA symptom. A key with a '
                f'{ttl}s TTL disappeared within {settle}s. Check the Redis '
                'maxmemory policy, whether anything is issuing FLUSHDB, and '
                'whether more than one Redis instance is in play.')

        self._cleanup(key)

    def _cleanup(self, key):
        try:
            cache.delete(key)
            self.stdout.write(f'\ncleaned up {key}')
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(
                f'could not delete {key}: {type(exc).__name__}'))
