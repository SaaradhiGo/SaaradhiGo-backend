"""Is a city ready to go live? And the ONE way to change a city's status.

READINESS ITEMS
---------------
`city_readiness(node)` returns a list of `{key, ok, detail, status, blocking, label, href}`
(`status` is `ok`, `missing` or `warning`; `ok` is False only for `missing`; `href` is the console
page that fixes it). Items today: `safety_coverage` (enough Safety Officers for the country's configured
minimum), `commission` (an active commission rule covers every vehicle class,
module 8), `fare_card` (an active card for every vehicle class in the
city's zone, module 8) and `zone` (the city has an active service zone with a
valid polygon, module 9), and from module 15 `payout_config`, `approval_policies`,
`payment_gateway`, `sms_provider` and `tax` (a warning, blocker #15). Later modules add to the
SAME list by appending items to `ITEMS`. An item that cannot be determined is NOT ok: a country
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


# ---------------------------------------------------------------------------
# Module 15: country-level prerequisites
# ---------------------------------------------------------------------------

def _country_code(node):
    country = node.country
    return country.code if country is not None else None


def _payout_config(node):
    from servers.payments.payout_config import active_config

    code = _country_code(node)
    if code is None or active_config(code) is None:
        return {'key': 'payout_config', 'ok': False,
                'detail': f'No active payout configuration for {code or "an unknown country"}: '
                          f'fee, minimum withdrawal and schedule are unset. Set it under Finance.'}
    return {'key': 'payout_config', 'ok': True,
            'detail': f'{code} has an active payout configuration.'}


def _approval_policies(node):
    from servers.approvals.actions import ACTIONS
    from servers.approvals.service import active_policy

    code = _country_code(node)
    if code is None:
        return {'key': 'approval_policies', 'ok': False,
                'detail': 'This city is not under a country, so no approval policy can apply.'}
    missing = sorted(a.key for a in ACTIONS.values()
                     if not a.always_checker and active_policy(code, a.key) is None)
    if missing:
        return {'key': 'approval_policies', 'ok': False,
                'detail': f'{code} has no maker-checker policy for: {", ".join(missing)}. '
                          f'Run seed_approval_policies, or set them under Finance.'}
    return {'key': 'approval_policies', 'ok': True,
            'detail': f'{code} has a maker-checker policy for every action.'}


def _payment_gateway(node):
    from servers.platform_ops.integrations import payment_gateway_status

    code = _country_code(node)
    state = payment_gateway_status(code)
    if state['configured']:
        return {'key': 'payment_gateway', 'ok': True,
                'detail': f"{state['provider']} is configured for {state['currency']} "
                          f"({state['mode']})."}
    blocker = f" (go-live blocker #{state['blocker']})" if state.get('blocker') else ''
    return {'key': 'payment_gateway', 'ok': False,
            'detail': f"No payment gateway is configured for {state['currency'] or code}"
                      f"{blocker}: {state['note']}"}


def _sms_provider(node):
    """The SMS provider for the country.

    STUBBED IS NEVER A HIDDEN PASS. With live sending off the item is a visible `warning`
    ("stubbed"); it becomes a blocker (`missing`) when live sending is REQUIRED, which is
    production (`PLATFORM_REQUIRE_LIVE_PROVIDERS`, default: IS_PRODUCTION)."""
    from servers.platform_ops.integrations import live_providers_required, sms_state

    code = _country_code(node)
    state = sms_state(code)
    if state['mode'] == 'live':
        return {'key': 'sms_provider', 'ok': True,
                'detail': f"{state['provider']} sends SMS for {code}."}
    if state['mode'] == 'stubbed':
        detail = (f"SMS for {code} is stubbed ({state['provider'] or 'no provider'}; live "
                  f"sending is off). ")
    else:
        detail = f'No SMS provider is configured for {code}. '
    if live_providers_required():
        return {'key': 'sms_provider', 'ok': False,
                'detail': detail + 'Live sending is required before going live here.'}
    return {'key': 'sms_provider', 'ok': True, 'status': 'warning',
            'detail': detail + 'This does not block QA; it blocks production.'}


def _tax(node):
    return {'key': 'tax', 'ok': True, 'status': 'warning',
            'detail': 'Tax is not configured: settlements show it as "not configured". '
                      "Go-live blocker #15 (owner's CA for India, US accountant for the US). "
                      'This does not block QA.'}


#: Append here. Each is `callable(node) -> {key, ok, detail[, status]}`.
ITEMS = [_safety_coverage, _commission, _fare_card, _zone, _payout_config, _approval_policies,
         _payment_gateway, _sms_provider, _tax]

#: What the console shows for each item, and the page that fixes it.
ITEM_META = {
    'zone': ('Service zone', '/zones'),
    'fare_card': ('Fare cards', '/pricing'),
    'commission': ('Commission rule', '/pricing'),
    'safety_coverage': ('Safety coverage', '/safety-ops'),
    'payout_config': ('Payout configuration', '/finance'),
    'approval_policies': ('Maker-checker policies', '/finance'),
    'payment_gateway': ('Payment gateway', '/settings/integrations'),
    'sms_provider': ('SMS provider', '/settings/integrations'),
    'tax': ('Tax settings', '/pricing'),
}


def _decorate(raw):
    ok = bool(raw['ok'])
    status = raw.get('status') or ('ok' if ok else 'missing')
    label, href = ITEM_META.get(raw['key'], (raw['key'], ''))
    return {**raw, 'ok': ok, 'status': status, 'blocking': not ok, 'label': label, 'href': href}


def city_readiness(node):
    """Every readiness item for `node`, passing or not."""
    return [_decorate(item(node)) for item in ITEMS]


def failing(items):
    return [i for i in items if not i['ok']]


def set_city_status(node, status, actor, reason, request=None):
    """Change a city's status. Returns `(node, changed)`.

    draft -> live, paused -> live, live -> paused. Refuses ACTIVE (live) while any readiness
    item fails, checked HERE so it is re-checked when a checker approves. Raises `NotACity` for
    a node that is not a city and `ValueError` for an unknown status, a missing reason, or
    pausing a city that is not live.
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
        if status == PAUSED and locked.status != ACTIVE:
            raise ValueError(f'{locked.code} is {locked.status}, not live: only a live city '
                             f'can be paused')
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
