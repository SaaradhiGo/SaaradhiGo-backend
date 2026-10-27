"""Operations on admin access that must not be done by hand.

The Super Admin floor lives here rather than in a model `clean()`, because
it is a rule about the SET of assignments, not about one row — a `clean()`
on the row being removed cannot see what remains.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import GLOBAL, AdminAssignment
from .roles import MIN_SUPER_ADMINS, SUPER_ADMIN


def active_super_admin_ids():
    """Distinct NAMED accounts currently holding Super Admin at Global.

    THE BREAK-GLASS ACCOUNT IS EXCLUDED, DELIBERATELY
    -------------------------------------------------
    The floor exists to guarantee two ACCOUNTABLE PEOPLE -- so that no
    single person can be the only way in, and so that any Super Admin
    action has a second person who could have done it instead. A shared
    emergency account whose credentials sit in an envelope is not a second
    person; counting it would let the floor be satisfied by a key under
    glass, which is the one case it exists to rule out.

    Excluding it is also what keeps the warning honest: with the emergency
    account counted, an environment with one real Super Admin would show
    "floor met" and nobody would ever appoint the second.
    """
    return set(
        AdminAssignment.objects
        .filter(role__code=SUPER_ADMIN, scope__kind=GLOBAL,
                is_active=True, role__is_active=True,
                admin__is_active=True)
        .exclude(admin__is_break_glass=True)
        .values_list('admin_id', flat=True)
    )


def super_admin_shortfall():
    """How many Super Admins short of the floor, for the warning banner.

    Zero means the floor is met. A positive number is shown persistently to
    Super Admins rather than blocking anything: a fresh environment must be
    allowed to run with one while a second real person is appointed, and
    forcing a test account up to Global to satisfy a counter would be worse
    than the warning.

    One named Super Admin plus a visible warning is a better state than
    none at all, because with nobody assigned EVERY account still falls
    through to the legacy unscoped path -- the warning is the honest
    description of a half-migrated platform, not a reason to delay starting.
    """
    return max(0, MIN_SUPER_ADMINS - len(active_super_admin_ids()))


class WouldLeaveTooFewSuperAdmins(ValidationError):
    """Raised instead of silently removing the last way back in."""


def revoke_assignment(assignment, *, actor=None, reason=''):
    """Deactivate an assignment, refusing to cross the Super Admin floor.

    A guard, not a bootstrap requirement: it blocks going BELOW the floor
    from at-or-above it. An environment already below the floor can still
    revoke — otherwise a mistake made at one Super Admin could never be
    undone.
    """
    with transaction.atomic():
        if (assignment.role.code == SUPER_ADMIN
                and assignment.scope.kind == GLOBAL
                and assignment.is_active):
            remaining = active_super_admin_ids() - {assignment.admin_id}
            currently = len(active_super_admin_ids())
            if currently >= MIN_SUPER_ADMINS and len(remaining) < MIN_SUPER_ADMINS:
                raise WouldLeaveTooFewSuperAdmins(
                    f'Removing this assignment would leave '
                    f'{len(remaining)} Super Admin(s); at least '
                    f'{MIN_SUPER_ADMINS} are required. Appoint another '
                    f'Super Admin first.'
                )

        assignment.is_active = False
        assignment.save(update_fields=['is_active'])

    return assignment
