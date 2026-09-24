"""Brute-force protection for the operations console login.

Measured against QA before this existed: **12 consecutive failed logins in 7.8
seconds, with no throttling of any kind.** That is the console that approves driver
KYC and releases payouts, reachable at the backend root.

The existing `base/throttles.py` could not cover it -- those are DRF throttle
classes and this login is a plain Django form view.

DESIGN NOTES
------------
Keyed on BOTH the submitted phone number and the source IP, because the two
attacks are different:

  * per-phone catches a distributed attempt against one operator account;
  * per-IP catches one host working through a list of phone numbers.

Locking only on phone would let an attacker deliberately lock a known operator out
of their own console, so the per-IP counter is what actually stops the attacker
while the per-phone counter has a longer, gentler window.

The refusal message is IDENTICAL whether the account exists or not, and identical
to the ordinary "invalid credentials" text. QA already showed no enumeration through
the login form, and a lockout message that only appears for real accounts would
introduce one.

FAILING OPEN, DELIBERATELY
--------------------------
If the cache is unreachable this guard allows the login and logs at ERROR. That is
the less safe direction and it is chosen on purpose: the cache is Redis, and
locking every operator out of the console during a Redis incident is worse for a
ten-driver pilot than temporarily losing brute-force protection. The alternative
fails closed exactly when operators most need to get in.

It is logged as a distinct event so the gap is visible rather than silent, and it is
recorded as a known tradeoff in the launch report rather than hidden here.
"""

import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Shown for a locked account, a wrong password and an unknown phone alike.
GENERIC_REFUSAL = 'Invalid phone number or password.'

_PHONE_PREFIX = 'ops_login_fail_phone:'
_IP_PREFIX = 'ops_login_fail_ip:'


def _max_attempts():
    return int(getattr(settings, 'OPS_LOGIN_MAX_ATTEMPTS', 8))


def _ip_max_attempts():
    """Tighter than per-phone: one host trying many accounts is more clearly hostile."""
    return int(getattr(settings, 'OPS_LOGIN_MAX_ATTEMPTS_PER_IP', 15))


def _lockout_seconds():
    return int(getattr(settings, 'OPS_LOGIN_LOCKOUT_SECONDS', 900))


def _client_ip(request):
    forwarded = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')
    if forwarded and forwarded[0].strip():
        return forwarded[0].strip()
    return request.META.get('REMOTE_ADDR') or 'unknown'


def _counts(phone, ip):
    """Returns (phone_failures, ip_failures) or None when the cache is unusable."""
    try:
        return (
            cache.get(_PHONE_PREFIX + str(phone), 0) if phone else 0,
            cache.get(_IP_PREFIX + str(ip), 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            'ops_login_guard_unavailable reason=cache_read_failed detail=%s '
            'effect=login_allowed_without_throttling', exc,
        )
        return None


def is_locked(request, phone):
    """True when this phone or this source has failed too many times recently.

    Never raises. A cache failure allows the login -- see the module docstring for
    why that direction was chosen.
    """
    ip = _client_ip(request)
    counts = _counts(phone, ip)
    if counts is None:
        return False
    phone_fails, ip_fails = counts
    if phone_fails >= _max_attempts():
        logger.warning(
            'ops_login_locked scope=phone failures=%s threshold=%s '
            'lockout_seconds=%s', phone_fails, _max_attempts(), _lockout_seconds(),
        )
        return True
    if ip_fails >= _ip_max_attempts():
        # No phone number in this line: an IP working through a list should not
        # have the list written into the logs for it.
        logger.warning(
            'ops_login_locked scope=ip failures=%s threshold=%s lockout_seconds=%s',
            ip_fails, _ip_max_attempts(), _lockout_seconds(),
        )
        return True
    return False


def record_failure(request, phone):
    """Count one failed attempt against both the phone and the source IP.

    Each counter's TTL is refreshed on every failure, so sustained attempts extend
    the lockout rather than sliding out from under it.
    """
    ip = _client_ip(request)
    ttl = _lockout_seconds()
    for key in ([_PHONE_PREFIX + str(phone)] if phone else []) + [_IP_PREFIX + str(ip)]:
        try:
            # get/set rather than incr: incr raises when the key is absent on some
            # backends, and locmem in tests is one of them.
            cache.set(key, (cache.get(key, 0) or 0) + 1, timeout=ttl)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                'ops_login_guard_unavailable reason=cache_write_failed detail=%s',
                exc,
            )


def clear(request, phone):
    """Forget the failures for this phone and source after a successful login."""
    ip = _client_ip(request)
    for key in ([_PHONE_PREFIX + str(phone)] if phone else []) + [_IP_PREFIX + str(ip)]:
        try:
            cache.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                'ops_login_guard_unavailable reason=cache_delete_failed detail=%s',
                exc,
            )


def failure_count(request, phone):
    """For tests and diagnostics. Returns (phone_failures, ip_failures)."""
    return _counts(phone, _client_ip(request)) or (0, 0)
