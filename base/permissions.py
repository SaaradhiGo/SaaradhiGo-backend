"""Project-level DRF permission classes.

Single source of truth so we don't end up with two divergent `IsAdmin`
implementations (one strict, one lax) that get silently imported from
different apps and produce different access decisions.
"""

from rest_framework.permissions import BasePermission


def is_operator(user) -> bool:
    """The single definition of "is this account allowed to operate the platform".

    THIS FUNCTION EXISTS BECAUSE THERE WERE FOUR ANSWERS
    ----------------------------------------------------
    The same question was decided independently in four places, and they did not
    agree:

      1. `IsAdmin.has_permission`   -- role OR superuser (no is_staff)
      2. `admin_dashboard.admin_required`  -- role OR superuser
      3. the console login view     -- role OR superuser
      4. `auth_user.admin_views.admin_login` -- role OR is_staff OR superuser,
         which admitted a rider carrying is_staff, on a token-MINTING endpoint

    Four copies is how the weakest one becomes the real policy: an attacker needs
    only the most permissive gate, and nobody notices the others are stricter.
    Number 1 is what allowed a self-registered `role='admin'` account to read the
    payout queue.

    Now: one function, and every caller delegates to it.

    THE RULE
    --------
    An operator is authenticated AND (`role == 'admin'` AND `is_staff`).
    A superuser is also an operator, for break-glass.

    `is_staff` is the load-bearing half, because it is the half no request can set.
    `role` was settable by a client until this run. `is_staff` is written only by
    `bootstrap_qa_operator` and the Django admin. It is also already how the rest
    of the codebase identifies operators -- `sos.dispatch_sos` fans out to
    `User.objects.filter(is_staff=True, role='admin')`.

    Takes a user rather than a request so the Django console, the DRF permission
    class and a plain function-based view can all share it.
    """
    if not (user and getattr(user, 'is_authenticated', False)):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    return bool(
        getattr(user, 'role', None) == 'admin'
        and getattr(user, 'is_staff', False)
    )


class IsAdmin(BasePermission):
    """Allow access only to authenticated operator users.

    Three conditions, all required:
    - authenticated (JWT or session)
    - `is_staff=True` (Django-standard signal for back-office access)
    - `role == 'admin'` (application-level intent flag on our CustomUser)

    `is_superuser` alone is also sufficient, for break-glass.

    THE is_staff CHECK USED TO BE MISSING
    -------------------------------------
    This docstring already claimed all three were required. The implementation
    checked only `authenticated and (role == 'admin' or is_superuser)`, so the
    second condition existed in documentation and nowhere else.

    That mattered because `role` was settable by a client. Asking for
    `role: "admin"` at the OTP step cached it, `/login/` created the account with
    it, and this class then granted full operator API access -- the KYC queue, the
    withdrawal queue, every trip, live driver locations, the operations dashboard.
    Verified end to end: all HTTP 200, on an account with `is_staff=False`.

    Two independent defects, and either one alone would have stopped it. Both are
    now closed: `request_otp` no longer accepts a privileged role, and the check
    below enforces what this docstring always said.

    `is_staff` is the right second gate because it is already how the rest of the
    codebase identifies operators -- `sos.dispatch_sos` fans out to
    `User.objects.filter(is_staff=True, role='admin')` -- and because it is set
    only by `bootstrap_qa_operator` and the Django admin, never by a request.
    """

    def has_permission(self, request, view):
        return is_operator(request.user)
