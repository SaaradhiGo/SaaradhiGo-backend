"""Is a city ready to go live? And the ONE way to change a city's status.

READINESS ITEMS
---------------
`city_readiness(node)` returns a list of `{key, ok, detail}`. Items today:
`safety_coverage` (enough Safety Officers for the country's configured
minimum), `commission` (an active commission rule covers every vehicle class,
module 8), `fare_card` (an active card for every vehicle class in the
city's zone, module 8) and `zone` (the city has an active service zone with a
valid polygon, module 9). Later modules add providers to the SAME list by
appending items to `ITEMS`; nothing else in this file changes. An item that cannot be determined is NOT ok: a country
with no safety config is "not configured", never a pass.

THE ONE DOOR
------------
`set_city_status(node, status, actor, reason)` is the ONLY code path that
changes `GeoNode.status` after the node exists. Going ACTIVE is refused while
any readiness item fails (`CityNotReady`, code `CITY_NOT_READY`, carrying the
failing items); pausing is always allowed. Every change is audited with the
actor and the reason (the structural `GeoNode` audit row, renamed
`city_status_changed`). `tests/test_safety_followups.py` greps the source tree
for any other writer.

SEEDS
-----
A seed may CREATE a city already active (`seed_admin_access` creates
Hyderabad live so a fresh QA stack is usable) because creation is not a status
change and a seed never flips an existing city (its `GEO` loop does not touch
`status` of a node that exists). The coverage sweep
(`safety.coverage`) is the backstop: a city that is live with nobody covering
it raises a critical alert within minutes.
"""

from django.db import transaction

from servers.admin_audit.context import audit_context

from .models import ACTIVE, CITY, PAUSED, GeoNode

CITY_NOT_READY = 'CITY_NOT_READY'


class CityNotReady(Exception):
    """Going live was refused. `items` is the list of failing readiness items."""

    code = CITY_NOT_READY

    def __init__(self, node, items):
        self.node, self.items = node, items
        keys = ', '.join(i['key'] for i in items)
        super().__init__(f'{node.code} is not ready to go live: {keys}')


class NotACity(ValueError):
    pass


def _safety_coverage(node):
    from servers.safety.coverage import coverage

    result = coverage(node)
    if not result['configured']:
        country = result['country'] or 'unknown country'
        return {'key': 'safety_coverage', 'ok': False,
                'detail': f'Safety is not configured for {country}: '
                          f'no minimum number of Safety Officers is set.'}
    detail = (f"{result['count']} Safety Officer(s) cover this city; "
              f"{result['minimum']} required.")
    return {'key': 'safety_coverage', 'ok': result['ok'], 'detail': detail}


def _city_zones(node):
    from servers.pricing.models import ServiceZone

    return list(ServiceZone.objects.filter(geo_node=node, is_active=True))


def _commission(node):
    """A commission rule governs every vehicle class in this city (module 8)."""
    from django.utils import timezone

    from servers.driver.models import VehicleType
    from servers.pricing.commission import resolve

    country = node.country
    if country is None:
        return {'key': 'commission', 'ok': False,
                'detail': 'This city is not under a country, so no commission rule can apply.'}
    now = timezone.now()
    classes = list(VehicleType.objects.all())
    missing = [vt.type for vt in classes if resolve(country.code, node.id, vt.id, now) is None]
    if not classes:
        missing = [] if resolve(country.code, node.id, None, now) else ['(any vehicle class)']
    if missing:
        return {'key': 'commission', 'ok': False,
                'detail': f'No active commission rule for {country.code} covers: '
                          f'{", ".join(missing)}. Approve one under Pricing.'}
    return {'key': 'commission', 'ok': True,
            'detail': f'An active commission rule covers every vehicle class in {node.code}.'}


def _fare_card(node):
    """An active fare card for every enabled vehicle class in the city's zone.

    Every VehicleType row counts as enabled: there is no enable flag today."""
    from django.utils import timezone

    from servers.driver.models import VehicleType
    from servers.pricing.services import get_active_rate_card

    zones = _city_zones(node)
    if not zones:
        return {'key': 'fare_card', 'ok': False,
                'detail': f'{node.code} has no active service zone, so it has no fare cards.'}
    now = timezone.now()
    classes = list(VehicleType.objects.all())
    if not classes:
        return {'key': 'fare_card', 'ok': False, 'detail': 'No vehicle classes are configured.'}
    gaps = []
    for zone in zones:
        for vt in classes:
            if get_active_rate_card(zone, vt, at=now) is None:
                gaps.append(f'{zone.code}/{vt.type}')
    if gaps:
        return {'key': 'fare_card', 'ok': False,
                'detail': f'No active fare card for: {", ".join(gaps)}.'}
    return {'key': 'fare_card', 'ok': True,
            'detail': f'Every vehicle class has an active fare card in {node.code}.'}


def _zone(node):
    """The city has at least one ACTIVE service zone with a usable polygon (module 9).

    Without one no pickup in the city resolves to a zone, so nobody could be quoted:
    a city cannot be live with no active zone."""
    from servers.pricing import zone_geometry
    from servers.pricing.models import ServiceZone

    zones = list(ServiceZone.objects.filter(geo_node=node, is_active=True))
    if not zones:
        return {'key': 'zone', 'ok': False,
                'detail': f'{node.code} has no active service zone. Draw and approve one '
                          f'under Zones.'}
    usable = []
    for zone in zones:
        try:
            zone_geometry.validate(zone.polygon_geojson)
            usable.append(zone.code)
        except zone_geometry.GeometryError:
            continue
    if not usable:
        return {'key': 'zone', 'ok': False,
                'detail': f'No active zone of {node.code} has a valid polygon.'}
    return {'key': 'zone', 'ok': True,
            'detail': f'{node.code} has {len(usable)} active service zone(s): '
                      f'{", ".join(sorted(usable))}.'}


#: Append here. Each is `callable(node) -> {key, ok, detail}`.
ITEMS = [_safety_coverage, _commission, _fare_card, _zone]


def city_readiness(node):
    """Every readiness item for `node`, passing or not."""
    return [item(node) for item in ITEMS]


def failing(items):
    return [i for i in items if not i['ok']]


def set_city_status(node, status, actor, reason, request=None):
    """Change a city's status. Returns `(node, changed)`.

    Refuses ACTIVE while any readiness item fails. Raises `NotACity` for a
    node that is not a city and `ValueError` for an unknown status or a
    missing reason.
    """
    if status not in (ACTIVE, PAUSED):
        raise ValueError(f'status must be {ACTIVE!r} or {PAUSED!r}')
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('reason is required')

    with transaction.atomic():
        locked = GeoNode.objects.select_for_update().get(pk=node.pk)
        if locked.kind != CITY:
            raise NotACity(f'{locked.code} is a {locked.kind}, not a city')
        if locked.status == status:
            return locked, False
        if status == ACTIVE:
            bad = failing(city_readiness(locked))
            if bad:
                raise CityNotReady(locked, bad)
        with audit_context(actor=actor, request=request, reason=reason,
                           action='city_status_changed',
                           extra_after={'status_from': locked.status, 'status_to': status}):
            locked.status = status
            locked.save()
    return locked, True
