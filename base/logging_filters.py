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


class PIIRedactionFilter(logging.Filter):
    """Run each log message through the patterns above. Returns True so
    the record is always passed on, just with a scrubbed message."""

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
