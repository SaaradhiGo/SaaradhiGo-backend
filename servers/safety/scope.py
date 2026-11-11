"""Which incidents an operator may see: ONE rule, used by every surface.

THE RULE
--------
An incident is in scope when its `anchor_node` is inside one of the caller's
scope paths (the same prefix match every other module uses). Two kinds of
incident have no single city and follow the SOS rules instead:

* `unanchored` (nothing could place it): visible to EVERY scoped
  `safety.respond` holder, like an unanchored SOS. An emergency nobody can
  place must reach the people on shift, not only Global.
* `rider_country` (only the rider's phone says where): visible to any holder
  scoped anywhere WITHIN that country, and to anyone scoped at or above it,
  the same reading `rider.scope` gives a country-anchored rider.

WHERE IT IS USED
----------------
`incidents_visible_to(user, node=None, queryset=None)` is the only queryset
that decides this. The list, the detail, every write, the assignee list, the
reveal and the ticket-escalation answer all call it (through `_incident_for`
in the views), so an incident that one of them returns always opens (never a
404 behind a row). Out of scope is 404, in scope without the capability is
403.

`responders_for(node)` is its mirror for people: the operators who hold
`safety.respond` at a place, who an SOS push goes to.
"""

from django.db.models import Q

from .models import ANCHOR_RIDER_COUNTRY, ANCHOR_UNANCHORED, SafetyIncident

CAPABILITY = 'safety.respond'


def _paths(user, node):
    """None for unrestricted, else the scope path prefixes."""
    from servers.admin_access.access import scope_paths
    from servers.rider.scope import unrestricted

    if node is not None:
        return [node.path]
    if unrestricted(user):
        return None
    return scope_paths(user) or []


def _ancestor_prefixes(path):
    """`a/b/c/` -> [`a/`, `a/b/`, `a/b/c/`] (a leading slash is kept)."""
    lead = '/' if path.startswith('/') else ''
    parts = [p for p in path.split('/') if p]
    return [lead + '/'.join(parts[:i]) + '/' for i in range(1, len(parts) + 1)]


def incidents_visible_to(user, node=None, queryset=None):
    """Incidents in the caller's scope (see the module docstring).

    `node` is the already-validated `?scope=` node (`requested_node`) or None;
    `queryset` defaults to every incident.
    """
    incidents = queryset if queryset is not None else SafetyIncident.objects.all()
    paths = _paths(user, node)
    if paths is None:
        return incidents
    if not paths:
        return incidents.none()

    covered = Q(anchor_source=ANCHOR_UNANCHORED)
    ancestors = set()
    for path in paths:
        covered |= Q(anchor_node__path__startswith=path)
        ancestors.update(_ancestor_prefixes(path))
    covered |= Q(anchor_source=ANCHOR_RIDER_COUNTRY, anchor_node__path__in=sorted(ancestors))
    return incidents.filter(covered)


def signals_visible_to(user, node=None, queryset=None):
    """Signals in the caller's scope: by the trip's city; a trip with no city
    behind it is visible to every scoped responder, like an unanchored SOS."""
    from .models import SafetySignal

    signals = queryset if queryset is not None else SafetySignal.objects.all()
    paths = _paths(user, node)
    if paths is None:
        return signals
    if not paths:
        return signals.none()
    covered = Q(anchor_node__isnull=True)
    for path in paths:
        covered |= Q(anchor_node__path__startswith=path)
    return signals.filter(covered)


def _related(a, b):
    return a.startswith(b) or b.startswith(a)


def incident_may(user, permission, incident):
    """Does the caller hold `permission` where this incident sits?"""
    from servers.admin_access.access import active_assignments
    from servers.admin_access.bridge import granted, is_migrated, strict_mode
    from servers.rider.scope import unrestricted

    node = incident.anchor_node
    if node is None:
        # Unplaceable: "does the caller hold it anywhere" is the SOS rule.
        return granted(user, permission)[0]
    if incident.anchor_source != ANCHOR_RIDER_COUNTRY:
        return granted(user, permission, node)[0]
    # A country anchor means "anywhere within the country".
    if getattr(user, 'is_superuser', False) or unrestricted(user):
        return granted(user, permission)[0]
    if not strict_mode() and not is_migrated(user):
        return granted(user, permission)[0]
    return any(
        permission in (a.role.permissions or []) and _related(node.path, a.scope.path)
        for a in active_assignments(user)
    )


def _operators_holding(permission, node):
    """Active, named operators holding `permission` at `node` (anywhere when
    `node` is None). Assignment-based: the break-glass account and Django
    superusers without an assignment are not people to page."""
    from django.contrib.auth import get_user_model

    from servers.admin_access.models import AdminAssignment, AdminRole

    role_ids = [r.id for r in AdminRole.objects.filter(is_active=True)
                if r.grants(permission)]
    if not role_ids:
        return []
    rows = (AdminAssignment.objects.filter(is_active=True, role_id__in=role_ids)
            .select_related('scope', 'admin'))
    ids = []
    for row in rows:
        if node is not None and not row.scope.covers(node):
            continue
        ids.append(row.admin_id)
    users = get_user_model().objects.filter(id__in=set(ids), is_active=True, is_staff=True)
    return [u for u in users if not getattr(u, 'is_break_glass', False)]


def responders_for(node):
    """Operators holding `safety.respond` at `node`: the SOS push audience.

    Global holders are included (a Global scope covers every node); an
    unanchored event (`node` None) goes to every holder anywhere.
    """
    return _operators_holding(CAPABILITY, node)


def super_admins():
    from django.contrib.auth import get_user_model

    from servers.admin_access.access import is_super_admin

    users = get_user_model().objects.filter(
        is_active=True, is_staff=True, admin_assignments__is_active=True,
        admin_assignments__role__code='super_admin',
    ).distinct()
    return [u for u in users if not getattr(u, 'is_break_glass', False) and is_super_admin(u)]


def eligible_responder(candidate, incident):
    """An active operator who holds `safety.respond` where the incident sits."""
    from base.permissions import is_operator

    return (candidate is not None and candidate.is_active and is_operator(candidate)
            and not getattr(candidate, 'is_break_glass', False)
            and incident_may(candidate, CAPABILITY, incident))
