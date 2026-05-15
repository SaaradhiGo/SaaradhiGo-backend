"""Operational health endpoint.

GET /healthz returns:
    200 {"status":"ok","db":"ok","cache":"ok","version":"…"} when healthy
    503 {...,"db":"fail",...} if any dependency is unreachable

Cheap on purpose — checks DB with a `SELECT 1`, cache with a round-trip
on a short-TTL key. No authentication so an external monitor
(BetterStack, UptimeRobot, ALB target-group health check) can hit it
without secrets. Doesn't expose anything an attacker could use.
"""

import os

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse


def _release_marker():
    """Best-effort release identifier. Operators can set DJANGO_RELEASE
    in the env (e.g. git SHA from the deploy step); otherwise we return
    `unknown` so the field is always present."""
    return os.environ.get('DJANGO_RELEASE') or 'unknown'


def healthz(request):
    status = {
        'status': 'ok',
        'db': 'ok',
        'cache': 'ok',
        'version': _release_marker(),
    }
    http_status = 200

    # Database — single round-trip; short on purpose.
    try:
        with connection.cursor() as c:
            c.execute('SELECT 1')
            c.fetchone()
    except Exception as e:
        status['status'] = 'degraded'
        status['db'] = 'fail'
        status['db_error'] = type(e).__name__
        http_status = 503

    # Cache (Redis in production, locmem in tests).
    try:
        cache.set('_healthz_ping', '1', timeout=5)
        if cache.get('_healthz_ping') != '1':
            raise RuntimeError('cache value did not round-trip')
    except Exception as e:
        status['status'] = 'degraded'
        status['cache'] = 'fail'
        status['cache_error'] = type(e).__name__
        http_status = 503

    return JsonResponse(status, status=http_status)
