"""Project-level DRF permission classes.

Single source of truth so we don't end up with two divergent `IsAdmin`
implementations (one strict, one lax) that get silently imported from
different apps and produce different access decisions.
"""

import logging

from rest_framework.permissions import BasePermission

logger = logging.getLogger(__name__)


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


# ===========================================================================
# Operator authority — minimal role separation
# ===========================================================================
#
# `is_operator` answers "may this account operate the platform at all". It does
# not, and should not, answer "may this account release a payout". Until now there
# was no second question: every operator could approve driver KYC, release money,
# read every trip and rewrite the rate card. For a ten-driver pilot with two
# trusted people that is survivable; as a permanent position it means the support
# person who answers "where is my driver" also holds payout authority.
#
# Deliberately small. Four operator roles, nine capabilities, one mapping. No
# permission editor, no per-object rules, no inheritance graph — those are how an
# authorization system becomes something nobody can reason about, which is worse
# than the coarse model it replaced.

OPS_SUPPORT = 'support'
OPS_DRIVER_OPS = 'driver_ops'
OPS_FINANCE = 'finance'
OPS_ADMIN = 'admin'

OPS_ROLE_CHOICES = [
    (OPS_SUPPORT, 'Support'),
    (OPS_DRIVER_OPS, 'Driver operations'),
    (OPS_FINANCE, 'Finance'),
    (OPS_ADMIN, 'Administrator'),
]

CAP_TRIP_READ = 'trip.read'
CAP_DRIVER_READ = 'driver.read'
CAP_DRIVER_KYC = 'driver.kyc'
CAP_SUPPORT_READ = 'support.read'
CAP_SUPPORT_REPLY = 'support.reply'
CAP_FINANCE_READ = 'finance.read'
CAP_FINANCE_PAYOUT = 'finance.payout'
CAP_PRICING_WRITE = 'pricing.write'
CAP_OPS_ADMIN = 'ops.admin'

ALL_CAPABILITIES = frozenset({
    CAP_TRIP_READ, CAP_DRIVER_READ, CAP_DRIVER_KYC,
    CAP_SUPPORT_READ, CAP_SUPPORT_REPLY,
    CAP_FINANCE_READ, CAP_FINANCE_PAYOUT,
    CAP_PRICING_WRITE, CAP_OPS_ADMIN,
})

# Flat, not nested. A hierarchy would make "finance" a superset of "support" by
# accident of ordering rather than by decision, and the decision here is that
# reading a trip is shared while approving KYC, releasing money and changing
# prices are each held by exactly one role plus the administrator.
OPS_ROLE_CAPABILITIES = {
    OPS_SUPPORT: frozenset({
        CAP_TRIP_READ, CAP_DRIVER_READ, CAP_SUPPORT_READ, CAP_SUPPORT_REPLY,
    }),
    OPS_DRIVER_OPS: frozenset({
        CAP_TRIP_READ, CAP_DRIVER_READ, CAP_DRIVER_KYC, CAP_SUPPORT_READ,
    }),
    OPS_FINANCE: frozenset({
        CAP_TRIP_READ, CAP_DRIVER_READ, CAP_SUPPORT_READ,
        CAP_FINANCE_READ, CAP_FINANCE_PAYOUT,
    }),
    OPS_ADMIN: ALL_CAPABILITIES,
}


def ops_role_of(user):
    """The operator role on this account, or None if it is not an operator.

    A superuser is treated as an administrator regardless of the stored value:
    break-glass access must not depend on a field somebody may not have set.
    """
    if not is_operator(user):
        return None
    if getattr(user, 'is_superuser', False):
        return OPS_ADMIN
    role = getattr(user, 'ops_role', None) or OPS_ADMIN
    return role if role in OPS_ROLE_CAPABILITIES else OPS_ADMIN


def capabilities_of(user):
    role = ops_role_of(user)
    return OPS_ROLE_CAPABILITIES.get(role, frozenset()) if role else frozenset()


def has_capability(user, capability) -> bool:
    """Does this account hold this specific authority?

    Non-operators hold nothing, so this is strictly narrower than `is_operator`
    and can be used without repeating that check.
    """
    if capability not in ALL_CAPABILITIES:
        # An unknown capability string is a programming error, and the safe
        # reading of a programming error in an authorization check is "no".
        logger.error('unknown capability requested: %r', capability)
        return False
    return capability in capabilities_of(user)


class RequiresCapability(BasePermission):
    """DRF permission for one capability. Use as `RequiresCapability(CAP_...)`.

    Instantiated rather than subclassed per capability, because nine subclasses
    that differ by one string is nine places for one of them to be wrong.

    DRF accepts permission *instances* in `permission_classes` as of 3.9 via
    `OperandHolder`, but the supported and obvious form is a callable, so this
    defines `__call__` to return itself — letting `permission_classes` hold
    `RequiresCapability(CAP_FINANCE_PAYOUT)` directly and read like the sentence
    it is.
    """

    def __init__(self, capability):
        self.capability = capability

    def __call__(self):
        return self

    def has_permission(self, request, view):
        return has_capability(request.user, self.capability)

    def __repr__(self):
        return f'RequiresCapability({self.capability!r})'
