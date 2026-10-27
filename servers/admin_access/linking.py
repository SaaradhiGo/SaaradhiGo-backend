"""Attaching existing rows to the scope spine.

Two trees already named the same places. `pricing.ServiceZone` has carried
`code='IN-TG-HYD'` since the first migration; `GeoNode` now uses the same
convention. This module is the one place that joins them, so a zone's
country and an admin's scope cannot disagree.

CALLED FROM TWO PLACES ON PURPOSE
---------------------------------
The data migration (`pricing/0010`) and `seed_admin_access` both call
`link_zones_to_nodes`. Either can run first: a fresh database migrates
before any node exists, so the migration links nothing and the seed links
everything; an existing database already has zones, so the migration links
them and the seed finds nothing to do. Both are idempotent, and having one
implementation means there is one definition of "linked".

Written against `apps.get_model` so the migration can pass its historical
models in. That is why nothing here imports a model at module level.
"""

import logging

logger = logging.getLogger(__name__)


def link_zones_to_nodes(ServiceZone, GeoNode, *, overwrite=False):
    """Point each ServiceZone at the GeoNode sharing its code.

    Returns `(linked, unmatched)` — unmatched being the zone codes with no
    node, which is a real condition worth reporting rather than hiding: a
    zone with no node is invisible to every scoped admin.

    `overwrite=False` leaves an existing link alone. A link that was set by
    hand is a decision, and a re-run of the seed must not quietly undo it.
    """
    nodes = {node.code: node for node in GeoNode.objects.all()}
    linked, unmatched = 0, []

    for zone in ServiceZone.objects.all():
        if zone.geo_node_id and not overwrite:
            continue
        node = nodes.get(zone.code)
        if node is None:
            unmatched.append(zone.code)
            continue
        zone.geo_node = node
        zone.save(update_fields=['geo_node'])
        linked += 1

    if unmatched:
        logger.warning(
            'service zones with no matching geo node: %s', sorted(unmatched)
        )
    return linked, unmatched


def sole_active_city(GeoNode):
    """The one live city, or None if there is not exactly one.

    Used by the driver backfill. Deliberately refuses to guess when there
    are several: putting every existing driver in the wrong city would be
    worse than leaving them unassigned, because an unassigned driver is
    reported while a misassigned one looks correct.
    """
    cities = list(GeoNode.objects.filter(kind='city', status='active')[:2])
    return cities[0] if len(cities) == 1 else None


def backfill_driver_home_city(Driver, GeoNode):
    """Give every unassigned driver the one live city.

    Returns `(assigned, skipped)`.

    WHY A BLANKET ASSIGNMENT IS CORRECT HERE AND ONLY HERE
    ------------------------------------------------------
    No driver row has ever carried a city, so there is nothing to infer
    from. At the time this runs exactly one city is live (Hyderabad), which
    means every existing driver is in fact a Hyderabad driver — the
    assignment is not a guess, it is the only possibility. The moment a
    second city launches that stops being true, which is why
    `sole_active_city` returns None rather than picking one.
    """
    city = sole_active_city(GeoNode)
    unassigned = Driver.objects.filter(home_city__isnull=True)
    if city is None:
        count = unassigned.count()
        if count:
            logger.warning(
                'not backfilling %s driver(s): there is not exactly one '
                'active city to attribute them to', count,
            )
        return 0, count
    return unassigned.update(home_city=city), 0
