"""Logging formatters + redaction filter for SaaradhiGo.

Two pieces wired into LOGGING in settings.py:

1. JSONFormatter — emits a single-line JSON object per log record, suitable
   for CloudWatch / Loki / Datadog ingestion. Falls back gracefully when
   the record contains an exception or non-serializable extras.

2. PIIRedactionFilter — scrubs obvious PII / secrets out of log messages
   before they go to stdout. Belt-and-braces for the cases where a
   developer logs request.body or a dict containing 'otp' / 'token'.
   Use it via `LOGGING.filters` and reference from each handler.

The JSON format is env-gated (DJANGO_LOG_FORMAT=json) so local dev keeps
the human-friendly text logs while CloudWatch in prod gets structured.
"""

import json
import logging
import re


# Compiled once at import — cheap per-message check.
_PII_PATTERNS = [
    # OTP-like 4-8 digit numbers attached to "otp" or "code" tokens
    (re.compile(r'(?i)\b(otp|code)["\']?\s*[:=]\s*["\']?\d{4,8}'), r'\1=***'),
    # Bearer tokens
    (re.compile(r'(?i)bearer\s+[A-Za-z0-9._\-]+'), 'Bearer ***'),
    # JWTs out in the open
    (re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'), '<jwt-redacted>'),
    # AWS access keys
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '<aws-key-redacted>'),
    # Phone numbers in E.164 — leave country code + 2 digits, redact the rest
    (re.compile(r'(\+91)(\d{2})\d{8}'), r'\1\2********'),
]


# Structured-logging keys whose VALUE must never be emitted, matched exactly
# on the lowercased key name. Coordinates are included deliberately: a precise
# pickup or drop point is location data about a named trip, and no log line
# needs it — `trip_id` is enough to look the trip up.
_REDACT_KEYS_EXACT = frozenset({
    'lat', 'lng', 'lon', 'latitude', 'longitude',
    'pickup_lat', 'pickup_lng', 'pickup_long',
    'destination_lat', 'destination_lng', 'destination_long',
    'coords', 'coordinates', 'location',
    'rider_name', 'driver_name', 'full_name', 'payee_name',
    'address', 'pickup_address', 'destination_address',
    'upi', 'vpa', 'account_number', 'ifsc',
    # Celery embeds the full task argument list under these keys, in both
    # `celery.worker.strategy` ("received") and `celery.app.trace`
    # ("succeeded"). Any task may legitimately carry PII in its arguments —
    # the push bodies include rider and driver names — so arguments are never
    # logged verbatim. Task name, id, runtime and return value are kept, which
    # is what the lines are actually useful for.
    'args', 'kwargs',
})

# Substring match on the key name, for the families where exact enumeration
# would inevitably miss one (`fcm_token`, `access_token`, `webhook_secret`, …).
_REDACT_KEY_SUBSTRINGS = (
    'otp', 'token', 'secret', 'password', 'passwd', 'authorization',
    'jwt', 'api_key', 'apikey', 'phone', 'email', 'cvv',
    # Payment-instrument keys only. A bare 'card' substring also swallowed
    # `rate_card_id` and `rate_card_version`, which are pricing-schedule
    # identifiers with nothing sensitive in them -- and redacting them removes
    # exactly the provenance a fare audit needs. Over-redaction is not free: it
    # looks like privacy while destroying the audit trail.
    'card_number', 'cardnumber', 'card_no', 'card_num', 'pan_number',
)

_REDACTED = '***'

# Depth limit so a self-referential or deeply nested extra cannot make logging
# recurse forever.
_MAX_REDACT_DEPTH = 4

# Record attributes that belong to logging itself, never to the caller's
# `extra`. Mirrors the skip-list in JSONFormatter.
_STDLIB_RECORD_ATTRS = frozenset({
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
    'funcName', 'levelname', 'levelno', 'lineno', 'module', 'msecs',
    'message', 'msg', 'name', 'pathname', 'process', 'processName',
    'relativeCreated', 'stack_info', 'thread', 'threadName', 'taskName',
})


def _key_is_sensitive(key):
    k = str(key).lower()
    if k in _REDACT_KEYS_EXACT:
        return True
    return any(frag in k for frag in _REDACT_KEY_SUBSTRINGS)


def _scrub_value(value, depth=0):
    """Redact a structured value in place-ish, returning the safe version."""
    if depth >= _MAX_REDACT_DEPTH:
        return _REDACTED
    if isinstance(value, str):
        out = value
        for pat, repl in _PII_PATTERNS:
            out = pat.sub(repl, out)
        return out
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _key_is_sensitive(k) else _scrub_value(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return type(value)(_scrub_value(v, depth + 1) for v in value)
    return value


class PIIRedactionFilter(logging.Filter):
    """Scrub PII from the message *and* from structured `extra` fields.

    The message half is the original behaviour: run the formatted text through
    `_PII_PATTERNS`.

    The `extra` half was missing, and mattered the moment structured logging
    arrived. `JSONFormatter` promotes every non-stdlib record attribute to a
    top-level JSON key, so a caller passing `extra={'phone': ...}` or
    `extra={'pickup_lat': ...}` would have emitted it verbatim — the message
    filter never sees those values because they are not part of the message.
    Sensitive keys are now redacted by name, and remaining string values are
    run through the same patterns as the message.

    Always returns True: a redaction bug must never drop a log record.
    """

    def filter(self, record):
        try:
            msg = record.getMessage()
            for pat, repl in _PII_PATTERNS:
                msg = pat.sub(repl, msg)
            record.msg = msg
            record.args = ()
        except Exception:
            # Never break logging for a redaction bug.
            pass

        try:
            for key, value in list(record.__dict__.items()):
                if key in _STDLIB_RECORD_ATTRS or key.startswith('_'):
                    continue
                if _key_is_sensitive(key):
                    record.__dict__[key] = _REDACTED
                else:
                    record.__dict__[key] = _scrub_value(value)
        except Exception:
            pass

        return True


class JSONFormatter(logging.Formatter):
    """One log record → one line of JSON. Keys are kept short on purpose
    so high-volume request logs don't bloat CloudWatch."""

    def format(self, record):
        data = {
            'ts': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'lvl': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        if record.exc_info:
            data['exc'] = self.formatException(record.exc_info)
        # Any custom attributes via logger.info("...", extra={...})
        for k, v in record.__dict__.items():
            if k in {
                'args', 'asctime', 'created', 'exc_info', 'exc_text',
                'filename', 'funcName', 'levelname', 'levelno', 'lineno',
                'module', 'msecs', 'message', 'msg', 'name', 'pathname',
                'process', 'processName', 'relativeCreated', 'stack_info',
                'thread', 'threadName',
            }:
                continue
            try:
                json.dumps(v)
                data[k] = v
            except (TypeError, ValueError):
                data[k] = repr(v)
        try:
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            # Last-ditch: never crash logging
            return json.dumps({'lvl': data['lvl'], 'msg': data['msg']})
