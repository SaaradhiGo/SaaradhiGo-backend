"""Is a city ready to go live? And the ONE way to change a city's status.

READINESS ITEMS
---------------
`city_readiness(node)` returns a list of `{key, ok, detail}`. Today there is
one item, `safety_coverage` (enough Safety Officers for the country's
configured minimum). Module 15 adds fare cards, zones, commission and
providers to the SAME list by appending items to `ITEMS`; nothing else in
this file changes. An item that cannot be determined is NOT ok: a country
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


#: Append here. Each is `callable(node) -> {key, ok, detail}`.
ITEMS = [_safety_coverage]


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
