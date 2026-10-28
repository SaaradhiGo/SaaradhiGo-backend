"""Running the new access model beside the old one, during the migration.

THE PROBLEM THIS SOLVES
-----------------------
The new model answers "may this admin do this, HERE" from `AdminAssignment`
rows. There are none yet, and there will be none until the existing
`ops_role` holders are migrated one by one with a name against each
decision. Swapping the endpoints over to `HasAdminPermission` today would
therefore lock every operator out of the console — a correct access model
that denies everybody is still an outage.

Equally, leaving the endpoints on `IsAdmin` means scope is enforced
nowhere, which is the hole in the first place.

THE RULE
--------
An endpoint grants if the NEW model grants it, or — while the migration is
open — if the LEGACY capability grants it. Every legacy grant is logged
with the account and the permission, so the log is the migration's to-do
list: when it falls silent, everybody has been migrated.

SCOPE TIGHTENS PER ACCOUNT, NOT ALL AT ONCE
-------------------------------------------
An account with no assignments is unmigrated, and sees what it sees today:
everything. An account WITH assignments is scoped, strictly. So migrating
one person is what turns scoping on for that person, and a half-finished
migration leaves nobody broken and nobody silently unscoped — the state of
each account is visible in `list_admin_access`.

This is a deliberately temporary file. `ADMIN_ACCESS_STRICT=True` closes
the legacy path, and the plan is to set it once the legacy log is quiet and
then delete this module along with `ops_role`. Until then the mapping below
is the contract between the two systems.
"""

import logging

from django.conf import settings
from rest_framework.permissions import BasePermission

from base.permissions import ALL_CAPABILITIES, has_capability, is_operator

from .access import active_assignments, has_permission_at
from .roles import ALL_PERMISSIONS, SUPER_ADMIN

logger = logging.getLogger(__name__)

#: New permission -> the legacy capability that used to grant it.
#: Eight of the nine strings are already identical; `ops.admin` was renamed
#: to `admin.manage` because it now means "manage admins", not "is one".
LEGACY_EQUIVALENT = {
    'trip.read': 'trip.read',
    'driver.read': 'driver.read',
    'driver.kyc': 'driver.kyc',
    'support.read': 'support.read',
    'support.reply': 'support.reply',
    'finance.read': 'finance.read',
    'finance.payout': 'finance.payout',
    'pricing.write': 'pricing.write',
    'admin.manage': 'ops.admin',
}

#: Permissions with no legacy capability because, before the migration,
#: the endpoints behind them were gated on `IsAdmin` alone — i.e. open to
#: EVERY operator, whatever their ops_role.
#:
#: They are listed rather than mapped onto some existing capability,
#: because mapping `report.read` to `trip.read` would be a quiet lie about
#: what the old system enforced. The old system enforced nothing here.
#:
#: This is exactly the gap the new model closes: an Analyst gets
#: `report.read` without `pii.view`, and a Marketing role gets neither
#: trips nor people. But closing it for an UNMIGRATED account would log
#: that account out of the dashboard it uses today, so these stay open
#: until that account is assigned a role.
LEGACY_OPEN_TO_ANY_OPERATOR = frozenset({
    # `/api/v1/ride/admin/dashboard/` was reachable by every operator, and
    # `test_operator_rbac.test_every_operator_can_still_do_their_job`
    # asserts it: "every operator keeps the shared read surface".
    'report.read',
    # `/api/v1/auth/admin/users/` returned every account's name, phone and
    # email to any operator. Narrowing it to `pii.view` is the intended end
    # state and is enforced for migrated accounts today.
    'pii.view',
    # `/api/v1/sos/admin/` and the acknowledge / resolve / false-alarm
    # transitions were gated on `IsAdmin` alone: every operator could read
    # and close the SOS queue. Migrated accounts now need `safety.respond`
    # (Safety Officer and Super Admin only); an unmigrated account keeps
    # the queue it has today, so the console's SOS page does not go dark
    # for an on-call person before their account has been assigned a role.
    'safety.respond',
    # `/api/v1/pricing/admin/zones/` and `.../rate-cards/` were readable by
    # any operator (`IsPlatformAdmin` let every operator read). Migrated
    # accounts need `zone.read`; unmigrated ones keep today's read.
    'zone.read',
})

# A typo here would silently grant or deny. Checked at import.
_unknown_new = (
    set(LEGACY_EQUIVALENT) | set(LEGACY_OPEN_TO_ANY_OPERATOR)
) - set(ALL_PERMISSIONS)
_unknown_old = set(LEGACY_EQUIVALENT.values()) - set(ALL_CAPABILITIES)
if _unknown_new or _unknown_old:  # pragma: no cover - import-time guard
    raise RuntimeError(
        f'bridge mapping is wrong: unknown new {sorted(_unknown_new)}, '
        f'unknown legacy {sorted(_unknown_old)}'
    )


#: Legacy `ops_role` -> the new role that most closely replaces it.
#:
#: NOT used to migrate anybody automatically: who holds what is a decision
#: with a name against it, which is what `migrate_operators` and its plan
#: are for. It exists so the TEST SUITE can build an operator that works
#: under both models, and so a reviewer can see what the old four roles
#: were understood to mean.
#:
#: `admin` maps to Super Admin because `OPS_ADMIN` held every capability.
#: `driver_ops` maps to KYC Verifier, which is the role that owns
#: `driver.kyc`; note it also carries `pii.view`, which `driver_ops` did
#: not have as a separate concept because nothing was masked then.
LEGACY_ROLE_EQUIVALENT = {
    'support': 'support_agent',
    'driver_ops': 'kyc_verifier',
    'finance': 'finance',
    'admin': SUPER_ADMIN,
}


def strict_mode():
    """True once the legacy path is closed."""
    return bool(getattr(settings, 'ADMIN_ACCESS_STRICT', False))


def is_migrated(user):
    """Does this account hold any assignment in the new model?

    The one question that decides whether scope applies to them. A
    superuser is not "migrated" by virtue of being a superuser — break-glass
    bypasses scope through `scope_paths`, not through this.
    """
    return active_assignments(user).exists()


def granted(user, permission, node=None):
    """May this account do this, here — by either model?

    Returns `(allowed, how)` where `how` is 'new', 'legacy' or 'denied', so
    callers can log the answer rather than guess at it.
    """
    if not is_operator(user):
        return False, 'denied'

    if has_permission_at(user, permission, node):
        return True, 'new'

    if strict_mode():
        return False, 'denied'

    if is_migrated(user):
        # MIGRATED ACCOUNTS DO NOT FALL BACK.
        #
        # Without this, assigning somebody a narrow role made them no
        # narrower: `ops_role_of` returns OPS_ADMIN when the legacy column
        # is empty -- which it is for every operator, because nobody ever
        # set it -- so a freshly-assigned Support Agent still held every
        # capability through the fallback, including driver KYC and payouts.
        # Migrating a person would have granted scope and taken away
        # nothing.
        #
        # So the new model is the ONLY authority for an account that has
        # any assignment. Migration is per-account and complete: assign
        # someone, and both their authority and their scope come from the
        # assignment, immediately.
        return False, 'denied'

    if permission in LEGACY_OPEN_TO_ANY_OPERATOR:
        logger.warning(
            'admin_access: granted %s to user %s because it was open to '
            'every operator before the migration (unmigrated account)',
            permission, getattr(user, 'id', None),
        )
        return True, 'legacy'

    legacy = LEGACY_EQUIVALENT.get(permission)
    if legacy and has_capability(user, legacy):
        # Not an error -- it is the expected state until migration finishes.
        # It is a WARNING because the volume here IS the migration's
        # progress bar, and it should reach zero.
        logger.warning(
            'admin_access: granted %s to user %s via LEGACY ops_role=%s '
            '(unmigrated account, unscoped access)',
            permission, getattr(user, 'id', None),
            getattr(user, 'ops_role', None) or '(default admin)',
        )
        return True, 'legacy'

    return False, 'denied'


class ScopedPermission(BasePermission):
    """The permission class every admin endpoint should use.

    Use as `ScopedPermission('driver.read')`.

    `has_permission` asks the question without a place, because the view
    does not yet know which object is involved — that is what a list
    endpoint is. The place is enforced in two further spots, and BOTH are
    required:

      * `has_object_permission`, for a detail or write endpoint, and
      * `scoped(...)` on the queryset, for a list endpoint.

    A list endpoint that uses this class and forgets `scoped()` is
    authorized correctly and still returns the whole world. There is no way
    for a permission class to catch that, which is why `scoping.scoped` is
    the only approved way to build an admin list queryset.
    """

    message = 'You do not have permission to do that here.'

    def __init__(self, permission):
        if permission not in ALL_PERMISSIONS:
            raise ValueError(f'Unknown permission {permission!r}')
        self.permission = permission

    def __call__(self):
        return self

    def has_permission(self, request, view):
        allowed, _how = granted(request.user, self.permission)
        return allowed

    def has_object_permission(self, request, view, obj):
        allowed, _how = granted(request.user, self.permission, node_of(obj))
        return allowed

    def __repr__(self):
        return f'ScopedPermission({self.permission!r})'


def node_of(obj):
    """The GeoNode an object sits in, or None if it has none.

    Mirrors `scoping.SCOPE_PATHS` for single objects. Kept short and
    explicit rather than walking the path string, because a wrong answer
    here grants access to the wrong country.
    """
    from .models import GeoNode

    if obj is None:
        return None
    if isinstance(obj, GeoNode):
        return obj

    # sos.SOSEvent: anchored at SOS time (trip zone, GPS -> city, or the
    # driver's home city). None here means UNANCHORED, and `granted` with
    # no node asks "does the caller hold it anywhere?" -- which is exactly
    # the rule for an SOS nobody can place: every Safety Officer may act.
    if hasattr(obj, 'anchor_source'):
        return getattr(obj, 'anchor_node', None)

    # driver.Driver
    node = getattr(obj, 'home_city', None)
    if node is not None:
        return node

    # ride.Trip -> pricing.ServiceZone -> GeoNode
    zone = getattr(obj, 'zone', None)
    if zone is not None:
        return getattr(zone, 'geo_node', None)

    # driver.WithdrawalRequest -> Driver
    driver = getattr(obj, 'driver', None)
    if driver is not None:
        return getattr(driver, 'home_city', None)

    # sos.SOSEvent -> Trip -> zone -> GeoNode, else the raiser's Driver
    # profile -> home_city. support.SupportTicket is the same shape under
    # different names (`trip_id`, `user_id`). A rider has no anchor by
    # design (see `scoping.RIDERS_ARE_GLOBAL`), so an event or ticket a
    # rider raised outside any trip has no node and is visible only at
    # Global -- the same rule as a driver with no home city.
    for trip_attr in ('trip', 'trip_id'):
        trip = getattr(obj, trip_attr, None)
        if trip is not None and not isinstance(trip, int):
            zone = getattr(trip, 'zone', None)
            if zone is not None:
                return getattr(zone, 'geo_node', None)
            break
    for user_attr in ('user', 'user_id'):
        raiser = getattr(obj, user_attr, None)
        if raiser is not None and not isinstance(raiser, int):
            profile = getattr(raiser, 'driver', None)
            if profile is not None:
                return getattr(profile, 'home_city', None)
            return None

    # pricing.ServiceZone
    return getattr(obj, 'geo_node', None)


def in_scope(user, obj, permission):
    """Is this already-fetched object inside the caller's scope?

    For the places that cannot filter in SQL. `select_for_update()` plus a
    scope filter means joining to `home_city`, which is nullable and so
    produces a LEFT OUTER JOIN — and Postgres refuses `FOR UPDATE` on the
    nullable side of an outer join. The withdrawal endpoints must keep that
    row lock, since it is what stops two admins double-paying a driver.

    So those endpoints take the lock first and ask this second. The window
    between is harmless: it can only end in a refusal, never in an action.

    Unmigrated accounts pass, as everywhere else during the migration.
    """
    if not strict_mode() and not is_migrated(user):
        return True
    allowed, _how = granted(user, permission, node_of(obj))
    return allowed
