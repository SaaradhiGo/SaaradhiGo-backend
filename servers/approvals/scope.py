"""Where an approval request sits, and who may see or act on it.

WHY THIS IS NOT A LINE IN `admin_access.scoping.SCOPE_PATHS`
------------------------------------------------------------
That table is the one place every admin list's anchor lives, and the right
long-term home for `approvals.ApprovalRequest -> scope_node__path`. It is
not added there in this change because the access model's files are being
edited concurrently for other rulings; the anchor is spelled here, once,
with the same rules `scoping.scoped` applies, and moving it is a one-line
follow-up.

THE ANCHOR
----------
A request is anchored at `scope_node`: the node the maker acted in, or the
country node when the caller named nothing narrower. A request with no
node at all (a country that has no GeoNode yet) is visible at Global only
-- the same rule as a driver with no home city, and reported the same way.

DURING THE MIGRATION
--------------------
An unmigrated account (no assignments, strict mode off) sees every request,
exactly as it sees every driver today; that exemption lives in `bridge`
and ends with `ADMIN_ACCESS_STRICT`. Note that it never makes anybody a
checker: `approval.check` has no legacy equivalent, so acting as a checker
needs a real assignment from day one.
"""

from servers.admin_access.access import scope_paths, scope_queryset
from servers.admin_access.bridge import granted, is_migrated, strict_mode
from servers.admin_access.models import GeoNode

PATH_FIELD = 'scope_node__path'


def country_node(country_code):
    """The GeoNode for an ISO country code, or None if the tree lacks it."""
    return GeoNode.objects.filter(kind='country', code=country_code).first()


def anchor_for(scope_node, country_code):
    """The node a request is stored against: the given one, else the country's."""
    if scope_node is not None:
        return scope_node
    return country_node(country_code)


def visible(user, queryset):
    """Restrict a request queryset to what this admin's scopes cover."""
    if not strict_mode() and not is_migrated(user):
        return queryset
    return scope_queryset(user, queryset, PATH_FIELD)


def may_act(user, permission, node):
    """Does `user` hold `permission` over `node`, by the bridge's rules?

    `node=None` means the request has no anchor, and only an unrestricted
    account (superuser, or any assignment at Global) reaches it: asking
    `granted(user, permission, None)` would instead answer "held anywhere",
    which is the one reading that must not apply to money.
    """
    if not strict_mode() and not is_migrated(user):
        # Unmigrated accounts hold no `approval.check` by either model, so
        # this branch only ever widens READ-shaped checks (`report.read`,
        # `platform.settings`) the way the rest of the console is widened.
        allowed, _how = granted(user, permission)
        return allowed
    if node is None:
        if scope_paths(user) is not None:
            return False
        allowed, _how = granted(user, permission)
        return allowed
    allowed, _how = granted(user, permission, node)
    return allowed
