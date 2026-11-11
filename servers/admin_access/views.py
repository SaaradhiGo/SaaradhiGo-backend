"""What the console needs to know about its own operator.

WHY THIS ENDPOINT HAD TO BE WRITTEN
-----------------------------------
`ops_role` was readable from the database and nowhere else. No API returned
it, so the console could not tell a Support Agent from a Finance operator
and therefore showed every nav item to everybody — the operator found out
what they were not allowed to do by clicking it and getting a 403.

So this returns the permissions and scopes the caller holds. It is for
DRAWING THE INTERFACE ONLY. Every endpoint enforces its own permission and
its own scope server-side; hiding a button is not security, and nothing in
this response is trusted on the way back in.

THE SCOPE SWITCHER
------------------
`scopes` is the list the switcher offers. An operator with one assignment
has one entry and the switcher is a label rather than a control. The
selection is sent back as `?scope=<code>`, and `requested_scope` INTERSECTS
it with what the operator holds — it can only ever narrow. A code the
caller does not hold is refused, not silently ignored, because silently
widening to everything is how a narrowing control becomes a privilege
escalation.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.permissions import is_operator
from base.utils import error_response, success_response

from .access import (
    active_assignments, is_super_admin, scope_paths,
)
from .services import super_admin_shortfall
from .bridge import capabilities_of, is_migrated, strict_mode
from .models import GeoNode


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def admin_whoami(request):
    """The caller's own roles, permissions and scopes.

    Not gated on a permission: every operator may ask what THEY hold. It
    returns nothing about anybody else.
    """
    user = request.user
    if not is_operator(user):
        return error_response(
            code='NOT_AN_OPERATOR',
            message='This account cannot operate the platform.',
            field='general', issue='Not an operator',
            status=status.HTTP_403_FORBIDDEN,
        )

    assignments = list(active_assignments(user))

    return success_response(
        data={
            'id': user.pk,
            'email': user.email or '',
            'full_name': (user.full_name or '').strip(),
            'is_superuser': bool(user.is_superuser),
            'is_break_glass': bool(getattr(user, 'is_break_glass', False)),

            # The union across every assignment. The console uses it to
            # decide which nav items to draw; the server decides whether
            # the action is allowed.
            #
            # `capabilities_of` is the same answer `bridge.granted` gives
            # with no place named, legacy grants included, so an
            # unmigrated operator's console is drawn from what the API
            # will actually allow, not from an empty list.
            'permissions': sorted(capabilities_of(user)),

            # True when the account is not narrowed to any place: a Django
            # superuser, a Global assignment, or an account the migration
            # has not reached yet. The console uses it for per-city links
            # (a board row's Safety link): unrestricted accounts get it
            # wherever they hold the capability.
            'unrestricted': scope_paths(user) is None or access_is_legacy(user),

            'roles': [
                {
                    'role': a.role.code,
                    'role_name': a.role.name,
                    'scope': a.scope.code,
                    'scope_name': a.scope.name,
                    'scope_kind': a.scope.kind,
                    # Capability names and the place they hold, nothing
                    # else: what lets the console decide a per-city link.
                    'scope_path': a.scope.path,
                    'permissions': sorted(a.role.permissions or []),
                }
                for a in assignments
            ],

            # What the switcher offers: every node the operator holds
            # something over, plus its descendants, so a country-scoped
            # operator can narrow to one city.
            'scopes': _selectable_scopes(user),

            # The Super Admin floor, for the console's warning banner.
            #
            # Shown only to Super Admins, because it is a prompt to ACT and
            # anybody else can do nothing about it. Not a secret -- it is a
            # count, not a list -- but noise to everyone else.
            'super_admin_shortfall': (
                super_admin_shortfall() if is_super_admin(user) else 0
            ),

            # Active console maintenance notices for this operator's scope: the banner (module 15).
            'notices': _console_notices(user),

            # Honest about which model is answering for this account, so
            # the console can say "unscoped (not yet migrated)" rather
            # than implying a scope that is not being enforced.
            'access_model': (
                'assignments' if is_migrated(user)
                else ('denied' if strict_mode() else 'legacy_unscoped')
            ),
        },
        status_code=status.HTTP_200_OK,
    )


def _console_notices(user):
    from servers.platform_ops.notices import console_banner

    try:
        return console_banner(user)
    except Exception:  # noqa: BLE001 - a banner must never stop the console loading
        return []


def access_is_legacy(user):
    """An unmigrated account in a deployment that still has the legacy path."""
    return not strict_mode() and not is_migrated(user)


def _selectable_scopes(user):
    """Every node the operator may narrow to, shallowest first."""
    paths = scope_paths(user)
    if paths is None:
        nodes = GeoNode.objects.all()
    elif not paths:
        return []
    else:
        from django.db.models import Q

        covered = Q()
        for path in paths:
            covered |= Q(path__startswith=path)
        nodes = GeoNode.objects.filter(covered)

    return [
        {
            'code': node.code,
            'name': node.name,
            'kind': node.kind,
            'status': node.status,
            'depth': node.path.count('/') - 1,
            'path': node.path,
        }
        for node in nodes.order_by('path')
    ]
