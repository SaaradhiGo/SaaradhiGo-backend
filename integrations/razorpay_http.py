"""Razorpay / RazorpayX HTTP plumbing shared by the payments and payouts adapters.

HTTP only, via `requests` (no vendor SDK, no new package). Everything here is vendor-specific, so
it lives under integrations/ (see tests/test_vendor_boundary.py).

KEYS AND MODE. One API root serves both modes; test vs live is decided by the KEY PREFIX
(`rzp_test_` / `rzp_live_`). On a non-production deployment (`IS_PRODUCTION` false) a `rzp_live_`
key is REFUSED at first use: the adapter reports "not configured" and no request is sent, so a
QA deployment can never move real money. Nothing here logs or returns a key or secret.
"""

import hashlib
import hmac
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BASE_URL = 'https://api.razorpay.com/v1'
#: Every outbound call is bounded (connect, read) seconds.
TIMEOUT = (5, 15)


class RazorpayTransportError(Exception):
    """The request may or may not have been processed: timeout, dropped connection, no reply."""


def key_mode(key_id):
    """`test`, `live`, or None for a missing / unrecognised key id."""
    text = str(key_id or '')
    if text.startswith('rzp_test_'):
        return 'test'
    if text.startswith('rzp_live_'):
        return 'live'
    return None


def key_refusal(key_id):
    """Why this key id may not be used here, or None when it may. Never includes the key."""
    if not key_id:
        return 'key id is not set'
    mode = key_mode(key_id)
    if mode is None:
        return 'key id is not a Razorpay key (expected a rzp_test_ or rzp_live_ prefix)'
    if mode == 'live' and not getattr(settings, 'IS_PRODUCTION', False):
        return ('a rzp_live_ key is refused outside production: use rzp_test_ keys on QA and '
                'local environments')
    return None


def hmac_hex(secret, message):
    """HMAC-SHA256 hex digest of `message` (bytes or str) under `secret`."""
    if isinstance(message, str):
        message = message.encode('utf-8')
    return hmac.new(str(secret).encode('utf-8'), message, hashlib.sha256).hexdigest()


def signatures_match(expected_hex, received):
    """Constant-time compare; False for an empty or non-string signature."""
    if not expected_hex or not received or not isinstance(received, str):
        return False
    return hmac.compare_digest(expected_hex.encode('utf-8'), received.strip().encode('utf-8'))


def request(method, path, key_id, key_secret, json=None, params=None, headers=None):
    """One bounded call. Returns `(status_code, body_dict)`; a non-JSON body gives `{}`.

    Raises RazorpayTransportError for a timeout / connection failure (the caller decides what an
    unknown outcome means: for a payout create it is `PayoutDispatchUnknown`).
    """
    try:
        resp = requests.request(
            method, BASE_URL + path, auth=(key_id, key_secret), json=json, params=params,
            headers=dict({'Accept': 'application/json'}, **(headers or {})), timeout=TIMEOUT)
    except requests.exceptions.RequestException as exc:
        raise RazorpayTransportError(type(exc).__name__) from exc
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return resp.status_code, body


def error_text(body):
    """A short, secret-free description from a Razorpay error body."""
    err = (body or {}).get('error') or {}
    return f"{err.get('code', 'ERROR')}: {str(err.get('description', ''))[:160]}"
