"""The per-role profile row that has to exist for an account to work.

WHY THIS MODULE EXISTS
----------------------
A `User` carries the identity and the role. The things a rider or a driver
actually DOES hang off a second row -- `Rider` or `Driver` -- and nearly
every endpoint for that role reaches it as `request.user.rider` or
`request.user.driver`, with no guard:

    driver = request.user.driver          # servers/driver/views.py
    serializer = DriverProfileSerializer(driver)

An account whose role is `driver` but which has no `Driver` row therefore
raises `RelatedObjectDoesNotExist` and the endpoint answers HTTP 500. Not a
404, not a helpful refusal -- a server error, on every driver screen.

THAT ACCOUNT WAS REACHABLE FROM THE PUBLIC WEBSITE. Four places create
accounts, and only two of them created the profile:

  * `views.login` (phone + OTP)             -- creates it inline
  * `invite_views.invite_account`           -- creates it inline
  * `bootstrap_qa_accounts`                 -- creates it inline
  * `registration_views.register_with_email` -- created a `Rider` for a
    rider and NOTHING AT ALL for a driver. Every driver who signed up
    through "Apply to drive" on the website therefore got an account that
    could sign in and then answer 500 on its own profile, on every driver
    endpoint, for good.

This function is what that path calls now, and it is where any new creation
path should go rather than growing a fifth copy of the branch. It is
idempotent: an account that already has its row is left exactly as it is,
so it is safe to call on an existing account and safe to call twice.
"""

import logging

logger = logging.getLogger(__name__)


def ensure_role_profile(user):
    """Create the `Rider` or `Driver` row for `user` if it is missing.

    Returns the profile row, or None for a role that has no profile (an
    operator has none: the console reads the `User` and the audit log).

    Deliberately tolerant. A profile that cannot be created must not take
    down the sign-in that is creating it -- the account itself is valid, and
    an operator can repair the row. The failure is logged at error level
    rather than raised.
    """
    role = getattr(user, 'role', None)

    try:
        if role == 'rider':
            from servers.rider.models import Rider
            profile, created = Rider.objects.get_or_create(user_id=user)
        elif role == 'driver':
            from servers.driver.models import Driver
            profile, created = Driver.objects.get_or_create(user_id=user)
        else:
            return None
    except Exception:
        logger.exception(
            'role_profile_create_failed user_id=%s role=%s',
            getattr(user, 'pk', None), role,
        )
        return None

    if created:
        logger.info('role_profile_created user_id=%s role=%s', user.pk, role)
    return profile
