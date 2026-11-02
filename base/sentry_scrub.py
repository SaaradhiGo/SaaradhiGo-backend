"""Sentry `before_send` / `before_breadcrumb`: secrets never leave for Sentry.

`send_default_pii=False` stops the SDK attaching users, IPs and cookies, but
it does not look inside what the integrations attach on their own. The one
that mattered: CeleryIntegration copies a failing task's args and kwargs into
`extra["celery-job"]`, and the login SMS task used to receive the OTP text as
an argument. Request bodies, exception messages, frame locals and breadcrumbs
can carry the same things.

So every event is walked before it is sent, with the rules the log filter
already applies (base/logging_filters.py), so logs and Sentry cannot disagree
about what is secret:
  * a value under a sensitive key (otp, password, token, secret,
    authorization, api_key, cookie, phone, args, kwargs, ...) is replaced;
  * every remaining string goes through the log filter's patterns, so
    "Your OTP for X is 123456", `otp=1234`, bearer tokens and JWTs are
    masked wherever they appear, exception messages included.

Unlike the log filter, depth is not capped at a small number: a Sentry
event's stack frames sit deep, and replacing them would make the event
useless. The cap only guards against pathological nesting.

If scrubbing itself fails the event is DROPPED, not sent unscrubbed: a
missing error report is recoverable, a leaked OTP is not.
"""

import logging

from base.logging_filters import _PII_PATTERNS, _key_is_sensitive

logger = logging.getLogger(__name__)

FILTERED = '[Filtered]'
_MAX_DEPTH = 32


def _scrub_string(value):
    for pattern, replacement in _PII_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def scrub(value, depth=0):
    """A copy of `value` with sensitive keys replaced and strings pattern-scrubbed."""
    if depth > _MAX_DEPTH:
        return FILTERED
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        return {k: (FILTERED if _key_is_sensitive(k) else scrub(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth + 1) for v in value]
    return value


def before_send(event, hint):
    try:
        return scrub(event)
    except Exception:  # noqa: BLE001 - never send an event we could not scrub
        logger.warning('sentry event dropped: scrubbing failed', exc_info=True)
        return None


def before_breadcrumb(crumb, hint):
    try:
        return scrub(crumb)
    except Exception:  # noqa: BLE001
        return None
