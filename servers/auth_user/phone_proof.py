"""Proof that the caller holds a phone number, for email sign-up (AUDIT 27.78, H4).

WHY THIS EXISTS
---------------
`/auth/email/register/` took a phone number on trust. The number is the
account's USERNAME_FIELD and is unique, so anybody could register an email
account on a stranger's number: the stranger's own later sign-up by SMS then
found an account already holding their number -- someone else's email, someone
else's password -- and the stranger was locked out of their own identity on
the platform. The review's scenario was exactly that: an attacker typing the
victim's number into the website form.

THE SHAPE OF THE FIX
--------------------
The phone is proved with an SMS code BEFORE the account exists:

  1. `POST /auth/otp/` -- unchanged, the same throttles, the same QA test-phone
     fixed codes, the same cache entry.
  2. `POST /auth/otp/verify/` -- checks the code with the SAME rules as the
     OTP sign-in (attempt counter, expiry, per-phone verify throttle), consumes
     it, and returns a proof. It does NOT create an account and does not say
     whether one exists.
  3. `POST /auth/email/register/` -- requires the proof for the submitted
     number and role, and redeems it.

A proof is a Django-signed token (SECRET_KEY, its own salt) carrying the phone,
the role and a random nonce, so it cannot be forged or moved to another number.
It is short-lived (`PHONE_PROOF_TTL_SECONDS`, ten minutes: long enough to fill
in a form, short enough to be useless later), and single-use: the nonce lives in
the cache and redeeming deletes it, so the delete itself is the one-time gate.
"""

import secrets

from django.conf import settings
from django.core import signing
from django.core.cache import cache

_SALT = 'saaradhigo.auth.phone_proof.v1'
_NONCE_PREFIX = 'phone_proof_nonce:'

#: Error codes, one per reason, so the client can say what to do next.
MISSING = 'AUTH_PHONE_PROOF_REQUIRED'
INVALID = 'AUTH_PHONE_PROOF_INVALID'
EXPIRED = 'AUTH_PHONE_PROOF_EXPIRED'
USED = 'AUTH_PHONE_PROOF_USED'


def ttl_seconds():
    return int(getattr(settings, 'PHONE_PROOF_TTL_SECONDS', 600))


def issue(phone, role):
    """A fresh single-use proof that `phone` was verified for `role`."""
    nonce = secrets.token_urlsafe(18)
    cache.set(_NONCE_PREFIX + nonce, str(phone), ttl_seconds())
    return signing.dumps({'p': str(phone), 'r': str(role), 'n': nonce}, salt=_SALT)


def redeem(token, phone, role, *, same_phone=None):
    """Check `token` against `phone` and `role` and use it up.

    Returns None when the proof is good (and is now spent), else one of the
    error codes above. `same_phone(a, b)` compares numbers when the caller
    normalises formats; plain equality otherwise.

    The order matters: signature and binding are checked BEFORE the nonce is
    deleted, so a proof for another number (or a forged one) cannot be used to
    burn a real person's proof.
    """
    if not token or not isinstance(token, str):
        return MISSING
    try:
        payload = signing.loads(token, salt=_SALT, max_age=ttl_seconds())
    except signing.SignatureExpired:
        return EXPIRED
    except signing.BadSignature:
        return INVALID
    if not isinstance(payload, dict):
        return INVALID
    proved = payload.get('p')
    matches = same_phone or (lambda a, b: a == b)
    if not proved or not matches(proved, phone) or payload.get('r') != role:
        return INVALID
    nonce = payload.get('n')
    if not nonce:
        return INVALID
    # The single-use gate. `delete` reports whether the key was there, so of
    # two concurrent registrations with one proof only one sees True.
    if not cache.delete(_NONCE_PREFIX + nonce):
        return USED
    return None
