"""Stripe over plain HTTPS (no SDK), shared by the payments and Connect payout adapters (13 Nov).

  * Requests are form-encoded, basic auth with the secret key, `Idempotency-Key` on every POST.
  * A live key (`sk_live_`) is refused outside production; QA runs on test keys only.
  * Webhooks: `Stripe-Signature: t=<unix>,v1=<hex>`; v1 = HMAC-SHA256(secret, "<t>.<raw body>"),
    compared in constant time, and `t` must be within the tolerance (default 300 s).
"""

import hashlib
import hmac
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
API = 'https://api.stripe.com/v1'
TIMEOUT = 15


class StripeTransportError(Exception):
    """The request may not have reached Stripe, or its answer was lost."""


def secret_key():
    return getattr(settings, 'STRIPE_SECRET_KEY', '') or ''


def key_refusal(key):
    if not key:
        return 'secret key is not set'
    if key.startswith('sk_live_') and not getattr(settings, 'IS_PRODUCTION', False):
        return 'a live key is refused outside production'
    if not (key.startswith('sk_test_') or key.startswith('sk_live_') or key.startswith('rk_')):
        return 'the key is not a Stripe secret key'
    return None


def flatten(data, prefix=''):
    """{'metadata': {'a': 1}} -> {'metadata[a]': '1'} (Stripe's form encoding)."""
    out = {}
    for key, value in (data or {}).items():
        name = f'{prefix}[{key}]' if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, name))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                out[f'{name}[{i}]'] = str(item)
        elif value is not None:
            out[name] = str(value).lower() if isinstance(value, bool) else str(value)
    return out


def request(method, path, data=None, idempotency_key=None, params=None):
    headers = {'Idempotency-Key': str(idempotency_key)[:255]} if idempotency_key else {}
    try:
        response = requests.request(method, f'{API}{path}', auth=(secret_key(), ''),
                                    data=flatten(data) if data else None, params=params,
                                    headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise StripeTransportError(type(exc).__name__) from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body


def error_text(data):
    err = (data or {}).get('error') or {}
    return f"{err.get('type', '')} {err.get('code', '')} {err.get('message', '')}".strip()[:200]


def verify_signature(raw_body, header, secret, tolerance=None, now=None):
    """True only for a v1 signature of this body under `secret`, made within the tolerance."""
    if not (secret and header):
        return False
    tolerance = int(tolerance if tolerance is not None else
                    getattr(settings, 'STRIPE_WEBHOOK_TOLERANCE_SECONDS', 300))
    parts = {}
    for item in str(header).split(','):
        key, _, value = item.strip().partition('=')
        parts.setdefault(key, []).append(value)
    try:
        stamp = int((parts.get('t') or [''])[0])
    except ValueError:
        return False
    if abs((now or time.time()) - stamp) > tolerance:
        return False
    raw = raw_body if isinstance(raw_body, (bytes, bytearray)) else str(raw_body).encode()
    expected = hmac.new(secret.encode(), f'{stamp}.'.encode() + bytes(raw), hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get('v1', []))


def sign(raw_body, secret, stamp=None):
    """A valid Stripe-Signature header (for the tests and the QA check)."""
    stamp = int(stamp or time.time())
    raw = raw_body if isinstance(raw_body, (bytes, bytearray)) else str(raw_body).encode()
    v1 = hmac.new(secret.encode(), f'{stamp}.'.encode() + bytes(raw), hashlib.sha256).hexdigest()
    return f't={stamp},v1={v1}'
