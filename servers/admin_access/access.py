"""Enforcing scope: the permission class, and the queryset filter.

TWO HALVES, AND THE SECOND IS THE ONE THAT MATTERS
---------------------------------------------------
A permission class answers "may this admin call this endpoint?". That alone
leaves the endpoint free to return every row in the world. The hole it
leaves is not theoretical: a list view with a correct permission check and
an unfiltered queryset hands an India-scoped operator the USA's drivers,
and nothing in the response says so.

So every list must also pass its queryset through `scope_queryset`. The two
are deliberately separate functions with separate tests, because they fail
in different ways and a reviewer should be able to see both.

HOW COVERAGE IS DECIDED
-----------------------
An assignment at `/IN/` covers anything whose path starts with `/IN/`.
Prefix matching on an indexed column, so the question costs one `OR` of
`startswith` clauses rather than a recursive walk per row.

SUPERUSER
---------
`is_superuser` is break-glass and bypasses scope. It is deliberate — an
account locked out of its own platform is worse than one with too much —
and every such access lands in the audit log like any other.
"""

from django.db.models import Q
from rest_framework.permissions import BasePermission

from base.permissions import is_operator

from .models import GLOBAL, AdminAssignment
from .roles import ALL_PERMISSIONS, SUPER_ADMIN


def active_assignments(user):
    """Every live assignment this admin holds, with role and scope loaded."""
    if not user or not getattr(user, 'is_authenticated', False):
        return AdminAssignment.objects.none()
    return (
        AdminAssignment.objects
        .filter(admin=user, is_active=True, role__is_active=True)
        .select_related('role', 'scope')
    )


def permissions_at(user, node=None):
    """The permissions this admin holds, optionally only at `node`.

    With no node, the union over every assignment — which answers "could
    this admin do X anywhere?", the right question for showing a nav item.
    Acting on a specific object must always pass the node.
    """
    if getattr(user, 'is_superuser', False):
        return set(ALL_PERMISSIONS)

    granted = set()
    for assignment in active_assignments(user):
        if node is not None and not assignment.scope.covers(node):
            continue
        granted.update(assignment.role.permissions or [])
    return granted


def has_permission_at(user, permission, node=None):
    """Does this admin hold `permission`, at `node` if one is given?"""
    if permission not in ALL_PERMISSIONS:
        # A permission that is not in the catalogue is a typo. Refusing is
        # the only safe reading: the alternative is a check that silently
        # passes because nothing grants an unknown string.
        raise ValueError(f'Unknown permission {permission!r}')
    return permission in permissions_at(user, node)


def scopes_of(user):
    """The geo nodes this admin holds anything over."""
    return [a.scope for a in active_assignments(user)]


def scope_paths(user):
    """The path prefixes covering everything this admin may see.

    `None` means unrestricted. A superuser is unrestricted; so is an admin
    holding any assignment AT GLOBAL, because the Global node is the root
    of the tree and "everything under the root" is everything -- including
    rows that have no node at all. Before this, a Global assignment was
    expressed as `path__startswith='GLOBAL/'`, which a NULL anchor never
    matches, so a Super Admin assigned at Global (as opposed to a Django
    superuser) could not see a driver with no home city, a zone drawn
    before its node existed, or an SOS raised by a rider outside any trip.
    The docstrings on those columns promised they were "visible at Global
    scope"; this is what makes that true.
    """
    if getattr(user, 'is_superuser', False):
        return None  # None means "unrestricted"; callers must check for it
    assignments = list(active_assignments(user))
    if any(a.scope.kind == GLOBAL for a in assignments):
        return None
    return sorted({a.scope.path for a in assignments})


def scope_queryset(user, queryset, path_field, also=None):
    """Restrict `queryset` to what this admin's scopes cover.

    `also` is an optional `Q` of rows that are in scope for EVERY scoped
    admin regardless of their paths (`scoping.UNSCOPED_UNION`: an SOS
    with no anchor). It is ORed onto the coverage, never used instead of
    it, and an admin with no assignments still gets nothing.

    `path_field` is the lookup reaching a `GeoNode.path` from the model
    being filtered, e.g. `'city__path'` or `'driver__home_city__path'`.
    It may also be a tuple of such lookups for a model with more than one
    possible anchor (an SOS event is anchored by its trip's zone if it has
    a trip, else by the raiser's home city): a row is in scope if ANY of
    its anchors is covered.

    An admin with no assignments gets nothing. That is the correct default
    and the one that fails safe: a new operator sees an empty list until
    somebody grants them something, rather than seeing the world until
    somebody remembers to restrict them.
    """
    paths = scope_paths(user)
    if paths is None:
        return queryset
    if not paths:
        return queryset.none()

    path_fields = (path_field,) if isinstance(path_field, str) else tuple(path_field)
    covered = Q()
    for path in paths:
        for field in path_fields:
            covered |= Q(**{f'{field}__startswith': path})
    if also is not None:
        covered |= also
    return queryset.filter(covered)


def is_super_admin(user):
    if getattr(user, 'is_superuser', False):
        return True
    return any(
        a.role.code == SUPER_ADMIN and a.scope.kind == GLOBAL
        for a in active_assignments(user)
    )


class HasAdminPermission(BasePermission):
    """DRF permission: holds `permission`, somewhere.

    Used as `HasAdminPermission('driver.kyc')`. It deliberately does NOT
    check scope — the view does not yet know which object is involved. Scope
    is enforced on the object (`has_object_permission`) and on the queryset
    (`scope_queryset`), and a view that lists without the latter is the bug
    this module's docstring is about.
    """

    message = 'You do not have permission to do that.'

    def __init__(self, permission):
        if permission not in ALL_PERMISSIONS:
            raise ValueError(f'Unknown permission {permission!r}')
        self.permission = permission

    def __call__(self):
        # DRF instantiates permission classes. Being already an instance,
        # this returns itself so `permission_classes=[HasAdminPermission(x)]`
        # works without a factory.
        return self

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if not is_operator(user):
            return False
        return has_permission_at(user, self.permission)

    def has_object_permission(self, request, view, obj):
        user = getattr(request, 'user', None)
        if not is_operator(user):
            return False
        node = getattr(obj, 'geo_node', None) or getattr(obj, 'city', None)
        return has_permission_at(user, self.permission, node)


def can_see_pii(user, node=None):
    """One question, asked by the masking layer of every caller."""
    return has_permission_at(user, 'pii.view', node)
