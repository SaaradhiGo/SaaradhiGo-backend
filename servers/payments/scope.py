"""Which money an operator may see: ONE rule, used by every finance surface.

THE ANCHORS (owner ruling for the Finance module)
-------------------------------------------------
  * Payment, settlement and refund:   the CITY OF THE TRIP the money was for
    (the pickup zone stamped on the trip), not where the driver lives.
  * Withdrawal, driver wallet and driver ledger:   the DRIVER'S HOME CITY.
    A payout is an obligation to a person, not to a place.
  * Rider wallet transaction (and a rider's own wallet):   the rider rule,
    `servers.rider.scope.riders_visible_to`.
  * A reconciliation item:   the city its subject sits in, stamped on the item
    when it was raised.
  * Anything that cannot be placed (a payment on a trip with no zone, a driver
    with no home city, a webhook with no payment):   UNRESTRICTED callers only.
    Never visible to a city.

`money_visible_to(user, model, node=None, queryset=None, permission=None)` is the
only function that decides this. Every finance list, detail, export and
dashboard aggregate calls it, so what a list shows, what its detail opens, what
an export contains and what a total adds up are the same rows.

ONE RULE FOR "OUT OF SCOPE" AND "NOT ALLOWED"
---------------------------------------------
A row outside the caller's scope is a 404, identical to a missing row (a 403
would confirm it exists). A row INSIDE the scope for which the caller does not
hold the capability AT THAT PLACE is a 403. `fetch_one` returns both answers so
no endpoint re-implements the order.

`permission=` NARROWS THE LIST TO WHERE THE CAPABILITY IS HELD
--------------------------------------------------------------
An operator can hold Finance in one city and Support in another. `scoped()` uses
every assignment, so a list guarded by `finance.read` would show the second
city's money to someone who may only do support work there. With `permission`,
only the assignments whose role carries it count. Unmigrated accounts and
unrestricted callers behave exactly as they do everywhere else.

COST
----
A prefix match (`startswith`) on the indexed `GeoNode.path` through the same
joins the existing scoped lists use; the rider family adds one `EXISTS`.
"""

from django.db.models import Q

from servers.admin_access.scoping import ModelIsNotScopable

#: Drivers are paid from the settlement balance; these wallet-ledger purposes
#: belong to the driver's wallet and follow the DRIVER rule. Every other
#: wallet transaction is a rider's and follows the rider rule.
DRIVER_LEDGER_PURPOSES = (
    'trip_earnings', 'trip_commission', 'withdrawal',
    'refund_failed_withdrawal', 'refund_rejected_withdrawal',
    # A manual correction on the driver's balance (Part B).
    'adjustment',
)

#: `model label -> ORM lookup(s) reaching GeoNode.path`. One table, so a view
#: cannot spell a path wrong. A model not here raises rather than leaking.
ANCHORS = {
    # --- the trip's city ---------------------------------------------------
    'payments.Payment': 'trip_id__zone__geo_node__path',
    'payments.TripSettlement': 'trip__zone__geo_node__path',
    'ride.TripMoneyAction': 'trip__zone__geo_node__path',
    # --- the driver's home city ---------------------------------------------
    'driver.WithdrawalRequest': 'driver__home_city__path',
    'driver.Driver': 'home_city__path',
    # A transaction row may be a payout with no trip, so it follows the driver.
    'payments.TransactionHistory': 'driver_id__home_city__path',
    # --- reconciliation -----------------------------------------------------
    'payments.ReconciliationItem': 'anchor_node__path',
}

#: Models handled by their own function below.
SPECIAL = ('rider.WalletTransaction', 'rider.Wallet', 'payments.ReconciliationRun')


def _unrestricted(user):
    from servers.rider.scope import unrestricted

    return unrestricted(user)


def paths_for(user, permission=None):
    """Path prefixes the caller may see for `permission`, or None = unrestricted.

    Without `permission` this is `access.scope_paths`. With it, only
    assignments whose role carries the permission count (a Global assignment is
    Super Admin only, which carries everything).
    """
    from servers.admin_access.access import active_assignments, scope_paths
    from servers.admin_access.models import GLOBAL

    if _unrestricted(user):
        return None
    if permission is None:
        return scope_paths(user)
    held = [a for a in active_assignments(user) if permission in (a.role.permissions or [])]
    if any(a.scope.kind == GLOBAL for a in held):
        return None
    return sorted({a.scope.path for a in held})


def _covered(paths, fields):
    q = Q()
    for path in paths:
        for field in fields:
            q |= Q(**{f'{field}__startswith': path})
    return q


def _effective_paths(user, node, permission):
    """The prefixes to filter on: the caller's, narrowed by a validated node.

    Returns `(paths, restricted)`. `restricted` False means "no filter".
    """
    paths = paths_for(user, permission)
    if node is None:
        return paths, paths is not None
    if paths is None:
        return [node.path], True
    # Narrowing never widens: the node must sit inside something the caller holds.
    if any(node.path.startswith(p) for p in paths):
        return [node.path], True
    return [], True


def money_visible_to(user, model, node=None, queryset=None, permission=None):
    """Rows of `model` the caller may see.

    `node` is the already-validated `?scope=` node (`requested_node`) or None.
    `queryset` defaults to every row. See the module docstring.
    """
    label = model._meta.label
    qs = queryset if queryset is not None else model.objects.all()

    if label == 'rider.WalletTransaction':
        return _wallet_rows(user, qs, node, permission, owner='user_id')
    if label == 'rider.Wallet':
        return _wallet_rows(user, qs, node, permission, owner='user_id', is_wallet=True)
    if label == 'payments.ReconciliationRun':
        return _runs(user, qs, node, permission)

    anchor = ANCHORS.get(label)
    if anchor is None:
        raise ModelIsNotScopable(
            f'{label} has no money scope anchor. Add one to ANCHORS in '
            f'servers/payments/scope.py; do not leave the queryset unfiltered.'
        )
    paths, restricted = _effective_paths(user, node, permission)
    if not restricted:
        return qs
    if not paths:
        return qs.none()
    return qs.filter(_covered(paths, (anchor,)))


def _riders_q(user, node, owner, paths=None):
    """The rider rule, narrowed to `paths`: the places the permission is held.

    Without that narrowing an operator with Finance in one city and Support in
    another would see the second city's rider wallet rows."""
    from servers.rider.models import Rider
    from servers.rider.scope import riders_visible_to

    riders = riders_visible_to(user, node, Rider.objects.all(), paths=paths)
    return Q(**{f'{owner}__in': riders.values('user_id')})


def _wallet_rows(user, qs, node, permission, *, owner, is_wallet=False):
    """Driver-ledger rows follow the driver's home city; the rest, the rider rule."""
    paths, restricted = _effective_paths(user, node, permission)
    if not restricted:
        return qs
    driver_side = (Q(scope='driver') if is_wallet
                   else Q(purpose__in=DRIVER_LEDGER_PURPOSES))
    rider_side = ~driver_side
    driver_q = driver_side & (_covered(paths, (f'{owner}__driver__home_city__path',))
                              if paths else Q(pk__in=[]))
    # `paths` already carries both the permission narrowing and the ?scope= node.
    return qs.filter(driver_q | (rider_side & _riders_q(user, None, owner, paths=paths)))


def country_codes_visible(user, node=None, permission=None):
    """Country codes whose configuration and runs the caller may see.

    A country is visible when any held scope sits inside it or contains it.
    Unrestricted callers see every supported country.
    """
    from base.countries import COUNTRIES
    from servers.admin_access.models import COUNTRY, GeoNode

    paths, restricted = _effective_paths(user, node, permission)
    if not restricted:
        return sorted(COUNTRIES)
    if not paths:
        return []
    return sorted(
        n.code for n in GeoNode.objects.filter(kind=COUNTRY, code__in=list(COUNTRIES))
        if any(n.path.startswith(p) or p.startswith(n.path) for p in paths)
    )


def _runs(user, qs, node, permission):
    """A run is per country: visible when any held scope relates to the country."""
    return qs.filter(country__in=country_codes_visible(user, node, permission))


def node_for(obj):
    """The GeoNode a money row sits in, or None if it cannot be placed.

    Mirrors ANCHORS for single objects.
    """
    label = obj._meta.label
    if label == 'payments.Payment':
        trip = getattr(obj, 'trip_id', None)
        zone = getattr(trip, 'zone', None)
        return getattr(zone, 'geo_node', None)
    if label in ('payments.TripSettlement', 'ride.TripMoneyAction'):
        zone = getattr(obj.trip, 'zone', None)
        return getattr(zone, 'geo_node', None)
    if label == 'driver.WithdrawalRequest':
        return getattr(obj.driver, 'home_city', None)
    if label == 'driver.Driver':
        return obj.home_city
    if label == 'payments.TransactionHistory':
        return getattr(obj.driver_id, 'home_city', None)
    if label == 'payments.ReconciliationItem':
        return obj.anchor_node
    raise ModelIsNotScopable(f'{label} has no node_for rule.')


def fetch_one(user, model, permission, queryset=None, **lookup):
    """`(row, refusal)`: refusal is None, 'not_found' (404) or 'forbidden' (403).

    Out of scope is indistinguishable from missing. In scope but without the
    capability at that row's place is forbidden.
    """
    from servers.admin_access.bridge import granted

    row = money_visible_to(user, model, queryset=queryset).filter(**lookup).first()
    if row is None:
        return None, 'not_found'
    node = node_for(row)
    # An unplaceable row is only ever in scope for the unrestricted; `granted`
    # with no node asks whether the caller holds the capability anywhere.
    if not granted(user, permission, node)[0]:
        return None, 'forbidden'
    return row, None
