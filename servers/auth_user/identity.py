"""One identity per person, reachable by phone OR email.

WHAT THIS MODULE IS FOR
-----------------------
The platform's identity has always been the phone number: `USERNAME_FIELD =
'phone_number'`, OTP to prove possession, and that is the whole story for a
rider or a driver. Operators additionally have a password, used by the console
and by `/auth/admin/login/`.

Adding email+password login introduces a SECOND way to name the same person,
and that is where the risk is. Two failure modes matter more than the feature:

  1. **Duplicate accounts.** If an email can be attached to two accounts, a
     rider ends up with two wallets, two trip histories and two sets of
     receipts, and support cannot tell which is real. `email` was a plain
     `CharField(blank=True, null=True)` with no uniqueness, so nothing stopped
     this.

  2. **Privilege escalation by linking.** If attaching an email to an account
     could ever move authority between accounts — or if an unverified email
     could be used to reset a password — then email becomes a way to take over
     an operator account without touching their phone. Operators hold KYC
     approval and payout release.

So the rules below are deliberately strict and deliberately boring.

THE RULES
---------
* An email address identifies **at most one** account. Enforced in the
  database by a unique constraint on the lower-cased address, not only here.
* Comparison and storage are **lower-cased and trimmed**. `A@B.com` and
  `a@b.com` are the same person.
* Linking an email to an account **never** changes the account's `role`,
  `is_staff`, `is_superuser` or `ops_role`. Authority comes from the account,
  never from the address used to reach it.
* An email that already belongs to another account is **refused**, never
  silently re-pointed.
* Password reset requires a **verified** email. An unverified address can be
  used to *receive* a verification link and nothing else — otherwise anyone
  who can set an unverified address on an account they control could pivot.

WHAT IS NOT DONE HERE
---------------------
No deliverability check, no disposable-domain blocking, no MX lookup. Those
are product decisions with false-positive costs, and a pilot does not need
them.
"""

import logging
import unicodedata

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

logger = logging.getLogger(__name__)

#: Fields that an email link or an email login must never be able to change.
#: Asserted in tests against the live model, so adding a new authority field
#: without considering it fails loudly.
AUTHORITY_FIELDS = ('role', 'is_staff', 'is_superuser', 'ops_role')


class EmailAlreadyLinked(Exception):
    """The address belongs to a different account."""


class InvalidEmail(Exception):
    """Not a usable address."""


def normalize_email(raw):
    """Trim, NFKC-normalise and lower-case an address.

    Unicode normalisation matters: a full-width or composed character can make
    two visually identical addresses compare unequal, which is a way to slip a
    second account past a uniqueness check.

    Returns '' for None or blank, so a caller can treat "no email" uniformly.
    Raises [InvalidEmail] for something that is present but not an address.
    """
    if raw is None:
        return ''
    text = unicodedata.normalize('NFKC', str(raw)).strip()
    if not text:
        return ''

    # Lower-case the whole thing. The local part is technically
    # case-sensitive per RFC 5321, but no mail provider in practice treats it
    # so, and honouring that would mean Foo@x.com and foo@x.com could be two
    # accounts -- which is exactly the duplicate this module exists to stop.
    text = text.lower()

    try:
        validate_email(text)
    except ValidationError as exc:
        raise InvalidEmail(str(exc)) from exc
    return text


def find_by_email(email, *, user_model=None):
    """The single account with this address, or None.

    Case-insensitive, because storage is normalised but historical rows may
    not be.
    """
    normalized = normalize_email(email)
    if not normalized:
        return None

    if user_model is None:
        from django.contrib.auth import get_user_model
        user_model = get_user_model()

    return user_model.objects.filter(email__iexact=normalized).first()


def assert_email_available(email, *, for_user=None, user_model=None):
    """Raise [EmailAlreadyLinked] if this address belongs to someone else.

    `for_user` is the account being updated, so re-saving an account with its
    own address is not a conflict.
    """
    owner = find_by_email(email, user_model=user_model)
    if owner is None:
        return
    if for_user is not None and owner.pk == for_user.pk:
        return

    # The log line names the account that OWNS the address, not the attempt:
    # writing the attempted address into the log makes the log an enumeration
    # oracle for anyone who can read it.
    logger.warning(
        'email_link_refused reason=already_linked owner_user_id=%s', owner.pk,
    )
    raise EmailAlreadyLinked(
        'That email address is already in use on another account.'
    )


def link_email(user, email, *, verified=False):
    """Attach an address to an account, refusing a duplicate.

    Returns the normalised address. Does NOT save; the caller decides the
    `update_fields`, because this is called from both a profile update and an
    invite flow and widening someone else's write is how a stale instance
    overwrites a concurrent change.

    `verified=True` is for the paths that have actually proven ownership —
    a confirmed verification token, or an operator-created invite whose
    set-password link was delivered to that address and used. Nothing else may
    pass it.
    """
    normalized = normalize_email(email)
    if not normalized:
        raise InvalidEmail('An email address is required.')

    assert_email_available(normalized, for_user=user)

    before_authority = {f: getattr(user, f, None) for f in AUTHORITY_FIELDS}

    user.email = normalized
    if verified:
        from django.utils import timezone
        user.email_verified_at = timezone.now()

    # Belt and braces. Linking must never move authority, and this catches a
    # future edit to this function that starts doing so.
    after_authority = {f: getattr(user, f, None) for f in AUTHORITY_FIELDS}
    if before_authority != after_authority:
        raise AssertionError(
            'linking an email changed an authority field: '
            f'{before_authority} -> {after_authority}'
        )

    return normalized


def email_is_verified(user) -> bool:
    """Has this account proven it controls its address?

    The gate for password reset. A `None` here is why a reset request for an
    unverified address is answered with the same generic message as one for an
    address nobody has.
    """
    return bool(getattr(user, 'email_verified_at', None))


def can_use_password_login(user) -> bool:
    """Is this account able to sign in with a password at all?

    False for an account that has never had one set — `set_unusable_password`
    is Django's default for a user created by the OTP path, and
    `check_password` against it always fails. Saying so explicitly keeps the
    login view from treating "no password set" as "wrong password" in its own
    logs, while still showing the user the same generic refusal.
    """
    return bool(user and user.has_usable_password())
