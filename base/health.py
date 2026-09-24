"""Operational health and revision endpoints.

GET /healthz  — dependency health, for an external monitor.
GET /version  — which revision is serving, for a deploy verifier.

WHY /version EXISTS SEPARATELY

A deploy was verified by polling /healthz until it returned 200, and then a
behaviour check was run against the result. The check failed, and the reported
defect was wrong: the deployment was still QUEUED, and the 200 had come from the
OLD container. A health endpoint answers "is something alive", never "is the thing
I just shipped alive".

/healthz already carried a `version`, but it read DJANGO_RELEASE — a variable an
operator sets by hand. On Railway that is a static service variable, so it
reported the same string across every deploy and could not distinguish revisions.
It looked like revision visibility while providing none.

`revision` is now resolved from whatever the platform injects per build, falling
back through a written build stamp to the hand-set variable, and the response says
WHICH source answered so nobody trusts a stale value again. When nothing can be
resolved it returns 'unknown' rather than something plausible.

/version does no database or cache work, so a verifier can poll it every second
during a deploy without adding load to the thing it is watching.

WHAT IS DELIBERATELY NOT EXPOSED

No secrets, no hostname, no dependency versions, no settings. A revision hash and
an environment name are already public in any client bundle and tell an attacker
nothing they could not get from the repository. Both endpoints stay unauthenticated
so a monitor and a deploy script need no credentials.
"""

import os

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse

# Checked in order. The platform-injected ones come first because they are the
# only ones that change on their own; a hand-set variable is the least
# trustworthy and therefore last.
_REVISION_SOURCES = (
    # Railway injects this when the service is connected to a repository.
    'RAILWAY_GIT_COMMIT_SHA',
    # Render, Heroku-style buildpacks, and several CI images.
    'RENDER_GIT_COMMIT',
    'SOURCE_VERSION',
    'SOURCE_COMMIT',
    'GIT_COMMIT',
    # GitHub Actions, when a workflow passes it through.
    'GITHUB_SHA',
    # Set by hand. Correct only if someone remembered to update it, which is why
    # it cannot be the primary answer.
    'DJANGO_RELEASE',
)

# Written at image build time if the build has a way to do it. A file beats a
# hand-set variable because it is produced by the build rather than maintained.
_BUILD_STAMP = os.path.join(settings.BASE_DIR, '.build-revision') \
    if hasattr(settings, 'BASE_DIR') else None


def _revision():
    """Return (revision, source). 'unknown' when nothing can be resolved.

    Never guesses. A wrong revision is worse than an absent one: it is what makes
    a verifier confirm a deploy that never happened.
    """
    for name in _REVISION_SOURCES:
        value = (os.environ.get(name) or '').strip()
        if value:
            return value, name

    if _BUILD_STAMP:
        try:
            with open(_BUILD_STAMP, encoding='utf-8') as fh:
                stamped = fh.read().strip()
            if stamped:
                return stamped, 'build-stamp'
        except OSError:
            pass

    return 'unknown', 'none'


def _release_marker():
    """Backwards-compatible `version` field.

    Kept so existing monitors and the QA scripts that read `version` do not
    break, but it now reports the resolved revision rather than only
    DJANGO_RELEASE.
    """
    return _revision()[0]


def _revision_payload():
    revision, source = _revision()
    return {
        'service': 'saaradhigo-backend',
        'environment': os.environ.get('ENVIRONMENT')
        or os.environ.get('RAILWAY_ENVIRONMENT_NAME')
        or 'unknown',
        'revision': revision,
        # Which env var or file answered. An operator staring at an unexpected
        # revision needs to know whether they are reading the platform's value or
        # somebody's forgotten variable.
        'revision_source': source,
        # Short form for eyeballing against `git log --oneline`.
        'revision_short': revision[:7] if revision != 'unknown' else 'unknown',
        'build_time': os.environ.get('BUILD_TIME') or 'unknown',
    }


def version(request):
    """Cheap revision metadata. No database, no cache, no authentication.

    A deploy verifier should poll THIS and compare `revision` with the commit it
    pushed, rather than treating a 200 from /healthz as proof.
    """
    return JsonResponse(_revision_payload(), status=200)


def healthz(request):
    status = {
        'status': 'ok',
        'db': 'ok',
        'cache': 'ok',
        'version': _release_marker(),
    }
    # Revision metadata alongside health, so one call can answer both "is it up"
    # and "is it the build I shipped".
    status.update(_revision_payload())
    status['status'] = 'ok'
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
