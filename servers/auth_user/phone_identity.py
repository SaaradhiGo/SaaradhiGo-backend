"""Accounts without a proven mobile number (11 Nov, Srini's ruling: riders sign up by email only).

`phone_number` is the USERNAME_FIELD, unique and NOT NULL, so an email-only rider holds a reserved
placeholder until they verify a real number at their first booking. Placeholders are not E.164,
cannot receive an OTP and never collide with a real number (the invite path uses `invite-`).
"""

import secrets

PLACEHOLDER_PREFIXES = ('invite-', 'email-')


def placeholder_phone():
    return f'email-{secrets.token_hex(6)}'


def is_placeholder_phone(value):
    return str(value or '').startswith(PLACEHOLDER_PREFIXES)


def has_verified_phone(user):
    """A real number is on the account. Every path that sets one proves it by OTP first."""
    return bool(getattr(user, 'phone_number', None)) and not is_placeholder_phone(user.phone_number)
