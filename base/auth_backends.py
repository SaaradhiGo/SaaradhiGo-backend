"""Let an operator sign in to the console with an email address OR a phone.

WHY
---
`customUser.USERNAME_FIELD` is `phone_number`, so Django's ModelBackend looks
up the identifier it is given as a phone number and nothing else. The console
login form therefore only ever accepted a phone, while the API had offered
`/auth/email/login/` for a while -- two sign-in surfaces for the same account
with two different identifiers, and the one a person is most likely to
remember worked on only one of them.

This adds the other lookup. It does not replace ModelBackend; it sits beside
it, so every existing phone+password sign-in keeps working unchanged.

WHAT IT DELIBERATELY KEEPS
--------------------------
* **The same password check.** Authentication is still
  `user.check_password` plus `user_can_authenticate`, so an inactive account
  is refused exactly as before.

* **No enumeration.** A miss returns None, which the caller turns into the
  one generic refusal it already uses. Nothing here distinguishes "no such
  address" from "wrong password".

* **The timing floor.** A lookup that finds nothing still runs a password
  hash, so an unknown address does not return measurably faster than a wrong
  password. `email_login` takes the same care; a backend that skipped it
  would reintroduce the oracle through the console.

* **Ambiguity is a refusal.** If more than one account somehow shares an
  address, this returns None instead of picking one. The database has a
  case-insensitive unique index on email, so that should be impossible --
  "should be impossible" is not a reason to guess which account to log
  somebody into.

It does NOT weaken MFA: the console checks `ops_mfa.mfa_required_for` after
authenticate() returns, on whichever backend authenticated.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

logger = logging.getLogger(__name__)


class EmailOrPhoneBackend(ModelBackend):
    """Authenticate against `email` when the identifier looks like one."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()

        identifier = username or kwargs.get(user_model.USERNAME_FIELD) or ''
        identifier = str(identifier).strip()

        # Only take the email path. Phone numbers are ModelBackend's job, and
        # handling them here too would mean two code paths for one case.
        if '@' not in identifier or not password:
            return None

        try:
            user = user_model.objects.get(email__iexact=identifier)
        except user_model.DoesNotExist:
            # Same work as a real check, so a miss is not faster than a hit.
            user_model().set_password(password)
            return None
        except user_model.MultipleObjectsReturned:
            logger.error(
                'email_login_ambiguous: more than one account holds an '
                'address; refusing rather than choosing one'
            )
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
