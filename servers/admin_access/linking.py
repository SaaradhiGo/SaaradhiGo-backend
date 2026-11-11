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

EVERY LINK IS AUDITED, WITH THE CALLER'S NAME ON IT
---------------------------------------------------
Which geo node a zone hangs from decides which scoped admins can see the
trips in it; which city a driver belongs to decides who reviews their KYC
and approves their payout. Both are scope changes, so each link appends an
`AdminAuditLog` row through `record_admin_action` -- one per zone, one per
driver -- carrying the actor of the surrounding `audit_context` (the
operator who ran `seed_admin_access`) and the caller's name in the reason.
A failed row aborts the linking: a scope change that cannot be recorded
must not happen quietly, which is the same rule the access models follow.

THE MIGRATION PATH IS THE DOCUMENTED EXCEPTION
----------------------------------------------
`pricing/0010` and `driver/0027` pass HISTORICAL models. Inside a
migration the audit table is not guaranteed to have the shape the live
model expects (its own migrations may not have run yet, and its columns
change), and a row written through a historical `AdminAuditLog` would
carry no hash and so break `verify_audit_chain` for every row after it.
So when the models passed in are historical, this module links WITHOUT
audit rows and logs a warning naming the gap. On an existing database
those two migrations ran on 2026-10-27 and their links are unaudited; on a
fresh database they link nothing (no node exists yet) and the seed, which
is audited, does the work. `is_historical` is how the two cases are told
apart, and `tests/test_unaudited_writers.py` holds both.
"""

import logging

from django.apps import apps as live_apps

logger = logging.getLogger(__name__)

ZONE_LINKED_ACTION = 'service_zone_linked_to_geo_node'
DRIVER_HOME_CITY_ACTION = 'driver_home_city_backfilled'


class LinkingAuditFailed(RuntimeError):
    """The audit row for a link could not be written, so the link was not kept."""


def is_historical(model):
    """Is `model` a migration's frozen copy rather than the live class?

    Historical models come from a `StateApps` registry, not the global one;
    they have no signal receivers and plain managers.
    """
    return getattr(model._meta, 'apps', live_apps) is not live_apps


def _record(action, target_type, target_id, before, after, reason):
    from servers.admin_audit.services import record_admin_action

    row = record_admin_action(
        None, action=action, target_type=target_type, target_id=target_id,
        before=before, after=after, reason=reason,
    )
    if row is None:
        raise LinkingAuditFailed(
            f'audit row {action} for {target_type} {target_id} could not be '
            f'written; the link was not kept. See the log.'
        )
    return row


def link_zones_to_nodes(ServiceZone, GeoNode, *, overwrite=False, caller='seed_admin_access'):
    """Point each ServiceZone at the GeoNode sharing its code.

    Returns `(linked, unmatched)` — unmatched being the zone codes with no
    node, which is a real condition worth reporting rather than hiding: a
    zone with no node is invisible to every scoped admin.

    `overwrite=False` leaves an existing link alone. A link that was set by
    hand is a decision, and a re-run of the seed must not quietly undo it.

    `caller` names who asked, for the audit row's reason.
    """
    from django.db import transaction

    audited = not is_historical(ServiceZone)
    if not audited:
        logger.warning(
            'link_zones_to_nodes: called with historical models (a migration); '
            'links made here are NOT audited. See admin_access.linking.'
        )

    nodes = {node.code: node for node in GeoNode.objects.all()}
    linked, unmatched = 0, []

    with transaction.atomic():
        for zone in ServiceZone.objects.all():
            if zone.geo_node_id and not overwrite:
                continue
            node = nodes.get(zone.code)
            if node is None:
                unmatched.append(zone.code)
                continue
            previous = zone.geo_node_id
            zone.geo_node = node
            zone.save(update_fields=['geo_node'])
            linked += 1
            if audited:
                _record(
                    ZONE_LINKED_ACTION, 'ServiceZone', zone.pk,
                    before={'zone_code': zone.code, 'geo_node_id': previous},
                    after={'zone_code': zone.code, 'geo_node_id': node.pk,
                           'geo_node_code': node.code},
                    reason=(f'Linked by {caller}: the zone code equals the '
                            f'node code. Trips in this zone are now visible '
                            f'to admins scoped at {node.code} and above.'),
                )

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


def backfill_driver_home_city(Driver, GeoNode, *, caller='seed_admin_access'):
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

    One audit row per driver, because each one is a change to who may see
    that driver's documents and money.
    """
    from django.db import transaction

    audited = not is_historical(Driver)
    if not audited:
        logger.warning(
            'backfill_driver_home_city: called with historical models (a '
            'migration); assignments made here are NOT audited. See '
            'admin_access.linking.'
        )

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

    with transaction.atomic():
        driver_ids = list(unassigned.order_by('pk').values_list('pk', flat=True))
        if not driver_ids:
            return 0, 0
        assigned = Driver.objects.filter(pk__in=driver_ids, home_city__isnull=True).update(
            home_city=city,
        )
        if audited:
            for driver_id in driver_ids:
                _record(
                    DRIVER_HOME_CITY_ACTION, 'driver', driver_id,
                    before={'home_city_id': None},
                    after={'home_city_id': city.pk, 'home_city_code': city.code},
                    reason=(f'Backfilled by {caller}: {city.code} is the only '
                            f'active city, so every driver without one is in '
                            f'it. Their KYC and payouts are now visible to '
                            f'admins scoped at {city.code} and above.'),
                )
    return assigned, 0
