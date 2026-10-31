"""How each admin-visible model reaches a GeoNode.

ONE TABLE, NOT A LOOKUP PER VIEW
--------------------------------
`scope_queryset` needs the ORM path from a model to `GeoNode.path`. If each
view spells its own, then one view spells it wrong and that view hands over
every row in the world — and the mistake is invisible, because the view
still returns plausible data. So the paths live here, once, next to a note
on why each is the right anchor.

A model that is NOT in this table cannot be scoped, and
`scoped(...)` raises rather than returning the queryset unfiltered. An
unscopable list is a decision somebody has to make deliberately: either
add an anchor to the model, or mark the endpoint global and say so.

WHAT "ANCHOR" MEANS FOR EACH SHAPE
----------------------------------
A trip is anchored where it HAPPENED (the pickup zone stamped on the row at
booking), not where its driver lives: a Hyderabad driver's airport run to
another city's territory is that city's incident to handle.

A driver is anchored where their operations team is (`home_city`), not their
current GPS position: a driver visiting another city must not drop off their
own city's KYC queue mid-review.

Money follows the driver, because a payout is an obligation to a person, not
to a place.

A RIDER HAS MANY ANCHORS, DECIDED IN ONE PLACE
----------------------------------------------
Riders travel, so a rider is not a `SCOPE_PATHS` row. `servers/rider/scope.py`
owns the rule (every city ridden in within `RIDER_SCOPE_TRIP_MONTHS`, plus a
home city, else the phone's country) and every rider surface uses it. See
`RIDERS_ARE_GLOBAL` for why `rider.Rider` stays out of this table.
"""

from django.db.models import Q

#: `model label -> ORM lookup reaching GeoNode.path` (or a tuple of them)
SCOPE_PATHS = {
    # Stamped at booking time, so it is stable even if zones are redrawn.
    'ride.Trip': 'zone__geo_node__path',

    # The operations team that owns the driver.
    'driver.Driver': 'home_city__path',

    # Through the driver, because a payout is owed to a person.
    'driver.WithdrawalRequest': 'driver__home_city__path',

    # A vehicle's documents are its driver's documents.
    'driver.Vehicle': 'driver_id__home_city__path',

    # Through the trip the money was for.
    'payments.Payment': 'trip_id__zone__geo_node__path',

    # Through the DRIVER, not the trip: a transaction row may be a payout
    # with no trip at all (`trip_id` is nullable), and anchoring on a
    # nullable column would make every payout row invisible.
    'payments.TransactionHistory': 'driver_id__home_city__path',

    # Zones and nodes are the spine itself.
    'pricing.ServiceZone': 'geo_node__path',
    'admin_access.GeoNode': 'path',

    # A rate card belongs to its zone.
    'pricing.RateCard': 'zone__geo_node__path',

    # A zone revision belongs to its zone (module 9).
    'pricing.ServiceZoneRevision': 'zone__geo_node__path',

    # A supply sample belongs to the city its drivers were in (module 10).
    'live_ops.SupplySample': 'city__path',

    # A commission rule is anchored at the country or city node it applies to.
    'pricing.CommissionRule': 'geo_node__path',

    # ANCHORED AT SOS TIME (owner's ruling, 2026-10-28)
    # ----------------------------------------------
    # `SOSEvent.anchor_node` is resolved when the row is created: the
    # trip's zone city if there is a trip, else the raiser's GPS position
    # -> city via the active ServiceZone polygons, else a driver's home
    # city. The trip path is kept as a second anchor so an event whose
    # zone is linked to its node AFTER the event stays reachable. An event
    # with NO anchor (`anchor_source='unanchored'`, a rider with no trip
    # and no fix) is in `UNSCOPED_UNION`: visible to EVERY scoped holder
    # of the endpoint's capability and at Global, and raises a Command
    # Centre alert. An SOS must never be invisible to its responders.
    'sos.SOSEvent': (
        'anchor_node__path',
        'trip__zone__geo_node__path',
    ),
    # A ticket is anchored by the trip it is about, else by the requester's
    # home city if the requester is a driver. THIS ROW IS NOT THE SUPPORT
    # MODULE'S RULE: a rider's trip-less ticket has no path here, and the
    # owner ruled it must be visible wherever that rider is visible. Every
    # support surface uses `servers.support.scope.tickets_visible_to`, which
    # adds the rider case; the row stays so the generic table resolves.
    'support.SupportTicket': (
        'trip_id__zone__geo_node__path',
        'user_id__driver__home_city__path',
    ),
}

#: Endpoints whose rows genuinely have no place, with the reason. Listing
#: them here is how "this one is global" stays a decision on the record
#: instead of a missing filter nobody noticed.
RIDERS_ARE_GLOBAL = (
    'A rider belongs to no single city: they travel. Use '
    'servers.rider.scope.riders_visible_to, which anchors a rider at every '
    'recent trip city, a home city, else the phone country.'
)

GLOBAL_BY_DESIGN = {
    'rider.Rider': RIDERS_ARE_GLOBAL,
    'auth_user.CustomUser': RIDERS_ARE_GLOBAL,
    'command_centre.OperatorAlert': (
        'A Command Centre alert is raised precisely because a row could '
        'not be placed (an SOS with no anchor); it is for every operator '
        'who holds the listing capability, and is not scoped.'
    ),
}

#: `model label -> Q` of rows that are visible to EVERY scoped holder of the
#: endpoint's capability, on top of their scope. Deliberately tiny: the only
#: rows that belong here are ones whose absence of a place is itself the
#: emergency. An SOS that could not be anchored must reach every Safety
#: Officer rather than only Global, or it reaches nobody on shift.
UNSCOPED_UNION = {
    'sos.SOSEvent': Q(anchor_source='unanchored'),
}


class ModelIsNotScopable(Exception):
    """Raised when a view asks to scope a model with no anchor.

    Loud on purpose. The alternative — returning the queryset unfiltered —
    is the exact failure this module exists to prevent, and it would be
    silent.
    """


def scope_path_for(model):
    label = f'{model._meta.app_label}.{model._meta.object_name}'
    if label in SCOPE_PATHS:
        return SCOPE_PATHS[label]
    if label in GLOBAL_BY_DESIGN:
        raise ModelIsNotScopable(
            f'{label} is global by design: {GLOBAL_BY_DESIGN[label]} '
            f'Use a permission check, not scope_queryset.'
        )
    raise ModelIsNotScopable(
        f'{label} has no scope anchor. Add one to SCOPE_PATHS in '
        f'servers/admin_access/scoping.py, or add it to GLOBAL_BY_DESIGN '
        f'with the reason. Do not leave the queryset unfiltered.'
    )


def scoped(user, queryset):
    """`scope_queryset`, with the path looked up from the model.

    The form every admin list view must use:

        drivers = scoped(request.user, Driver.objects.all())

    DURING THE MIGRATION
    --------------------
    An account with no assignments is unmigrated and is NOT filtered — it
    sees what it sees today — because filtering it would mean an empty
    console for everybody until the last person is migrated. The exemption
    is logged on every use, lives only in `bridge`, and ends when
    `ADMIN_ACCESS_STRICT` is set. `access.scope_queryset` itself stays
    strict: no assignments means no rows.
    """
    import logging

    from .access import scope_queryset
    from .bridge import is_migrated, strict_mode

    path = scope_path_for(queryset.model)
    also = UNSCOPED_UNION.get(queryset.model._meta.label)

    if not strict_mode() and not is_migrated(user):
        logging.getLogger(__name__).warning(
            'admin_access: %s list served UNSCOPED to unmigrated user %s',
            queryset.model._meta.label, getattr(user, 'id', None),
        )
        return queryset

    return scope_queryset(user, queryset, path, also=also)


def scoped_or_none(user, queryset, **lookup):
    """Fetch one row, or None if it is out of scope or absent.

    Returns None rather than raising, so the caller answers with the
    project's own `error_response` envelope instead of DRF's bare 404 body.

    THE ANSWER MUST BE "NOT FOUND", NEVER "FORBIDDEN"
    -------------------------------------------------
    A 403 confirms the row exists. An out-of-scope admin could then walk
    ids and learn how many drivers another country has, and which ids are
    real, without ever being allowed to open one. "Not found" is also the
    honest answer: within that admin's world, it is not there.

    So every caller must report a missing row and an out-of-scope row
    identically — same status, same message. Two different messages
    reintroduce the leak that returning None was meant to close.
    """
    return scoped(user, queryset).filter(**lookup).first()


class ScopeNotHeld(Exception):
    """The caller asked to act in a scope they do not hold."""


def requested_node(request):
    """The `?scope=<code>` node, checked against what the caller holds.

    Returns None when no scope was requested, which means "everything I
    hold".

    REFUSES RATHER THAN IGNORES
    ---------------------------
    A code the caller does not hold raises. The tempting alternative — fall
    back to the full held scope — turns a narrowing control into a widening
    one the first time a stale tab sends a code from a revoked assignment,
    and the operator sees more than they chose rather than less.
    """
    from .access import scope_paths
    from .models import GeoNode

    code = (request.query_params.get('scope') or '').strip()
    if not code:
        return None

    node = GeoNode.objects.filter(code=code).first()
    if node is None:
        raise ScopeNotHeld(f'No such scope {code!r}.')

    paths = scope_paths(request.user)
    if paths is None:
        return node  # superuser: holds everything
    if not any(node.path.startswith(path) for path in paths):
        raise ScopeNotHeld(f'You do not hold {code!r}.')
    return node


def scoped_to_request(request, queryset):
    """`scoped`, then narrowed further by `?scope=` if one was sent.

    The form a list view should use once the console offers a switcher.
    Narrowing is applied ON TOP of the caller's own scope, never instead of
    it, so the parameter cannot be used to reach outside an assignment.
    """
    base = scoped(request.user, queryset)
    node = requested_node(request)
    if node is None:
        return base
    path_field = scope_path_for(queryset.model)
    if isinstance(path_field, str):
        path_field = (path_field,)
    narrowed = Q()
    for field in path_field:
        narrowed |= Q(**{f'{field}__startswith': node.path})
    # Rows with no place at all stay in a narrowed view too: narrowing to
    # one city is "show me Austin", and an unanchored SOS is nowhere, not
    # elsewhere. Hiding it behind the switcher is how it goes unanswered.
    also = UNSCOPED_UNION.get(queryset.model._meta.label)
    if also is not None:
        narrowed |= also
    return base.filter(narrowed)
