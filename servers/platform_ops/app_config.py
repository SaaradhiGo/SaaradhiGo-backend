"""The public app-config document: what the rider and driver apps need to know about a market.

    GET /api/v1/app-config/?country=XX[&city=CODE][&app=rider|driver]

Public, read-only, cacheable (`Cache-Control: public, max-age=60`), rate limited per client
address. NO PII and NO SECRET: it is the country registry, the live cities' service-area boxes,
flags, the app-version policy and active notices.

UNKNOWN COUNTRY IS A 404, NEVER INDIA. A country not in the registry, or a city that does not
exist, is `404 UNKNOWN_COUNTRY` / `UNKNOWN_CITY`; the apps must not guess a market.

With no query at all the endpoint returns the LIVE countries (code, name, dialling code) so a
sign-in screen can offer a choice before anything is known about the user.

WHAT THE APPS TAKE FROM IT (App catch-up R7, R9, D6, D9): the dialling code at login, the currency
symbol and minor units, the service-area box and onboarding city list, the emergency number label,
the version gate and the maintenance banner.
"""

from django.utils import timezone

from base import money
from base.countries import COUNTRIES, find_country
from servers.admin_access.models import ACTIVE, CITY, COUNTRY, GeoNode
from servers.pricing.models import ServiceZone
from servers.promotions import referrals, rider_promos

from . import flags, markets, notices, versions
from .errors import PlatformError

#: Languages the apps offer per country. Not in the registry (they are product copy, not market
#: arithmetic); a country with no entry offers English only.
LANGUAGES = {'IN': ('en', 'hi', 'te'), 'US': ('en',)}
EMERGENCY_LABELS = {'unified': 'Emergency services', 'police': 'Police', 'ambulance': 'Ambulance',
                    'fire': 'Fire'}
APPS = ('rider', 'driver')


def _coords(value, out):
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
            out.append((float(value[0]), float(value[1])))
        else:
            for item in value:
                _coords(item, out)


def city_area(node):
    """`{center: {lat, lng}, bounds: {south, west, north, east}}` from the city's active zones,
    or None when it has none. A bounding box only: the polygon itself is not returned."""
    points = []
    for zone in ServiceZone.objects.filter(geo_node=node, is_active=True):
        _coords((zone.polygon_geojson or {}).get('coordinates'), points)
    if not points:
        return None
    lngs, lats = [p[0] for p in points], [p[1] for p in points]
    south, north, west, east = min(lats), max(lats), min(lngs), max(lngs)
    return {'center': {'lat': round((south + north) / 2, 6), 'lng': round((west + east) / 2, 6)},
            'bounds': {'south': round(south, 6), 'west': round(west, 6),
                       'north': round(north, 6), 'east': round(east, 6)}}


def country_block(country):
    return {
        'code': country.code, 'name': country.name,
        'calling_code': country.calling_code,
        'phone': {'calling_code': country.calling_code,
                  'national_number_lengths': list(country.national_number_lengths)},
        'currency': {'code': country.currency, 'symbol': country.currency_symbol,
                     'symbol_position': country.symbol_position,
                     'minor_units': money.exponent(country.currency)},
        'distance_unit': country.distance_unit,
        'tax': {'mode': country.tax_mode, 'label': country.tax_label},
        'emergency': {
            'numbers': [{'kind': kind, 'number': number} for kind, number in country.emergency_numbers],
            'label': EMERGENCY_LABELS.get(country.emergency_numbers[0][0], 'Emergency services')
            if country.emergency_numbers else None},
    }


def _live_cities(country_code):
    out = []
    nodes = (GeoNode.objects.filter(kind=CITY, status=ACTIVE).select_related('parent__parent')
             .order_by('path'))
    for node in nodes:
        if markets.country_code_of(node) != country_code:
            continue
        area = city_area(node)
        if area is None:
            continue
        state = node.parent if node.parent_id and node.parent.kind == 'state' else None
        out.append({'code': node.code, 'name': node.name,
                    'state': {'code': state.code, 'name': state.name} if state else None,
                    'service_area': area})
    return out


def live_countries():
    out = []
    for code in markets.live_country_codes():
        country = find_country(code)
        if country is not None:
            out.append({'code': country.code, 'name': country.name,
                        'calling_code': country.calling_code})
    return out


def build(country=None, city=None, app=None):
    """The document, or a `PlatformError` (404) for an unknown country or city."""
    if app and app not in APPS:
        raise PlatformError('app must be rider or driver.', code='INVALID_APP', field='app',
                            status=400)
    audiences = (app,) if app else APPS
    city_node = None
    if city:
        city_node = GeoNode.objects.filter(kind=CITY, code=str(city).strip().upper()).first()
        if city_node is None:
            raise PlatformError(f'Unknown city {city!r}.', code='UNKNOWN_CITY', field='city',
                                status=404)
        derived = markets.country_code_of(city_node)
        if country and str(country).strip().upper() != derived:
            raise PlatformError('That city is not in that country.', code='UNKNOWN_CITY',
                                field='city', status=404)
        country = derived
    if not country:
        # No market named: the live countries, plus the platform-wide parts.
        scope_node = None
        body = {'countries': live_countries()}
    else:
        registry = find_country(country)
        if registry is None:
            raise PlatformError(f'Unknown country {country!r}.', code='UNKNOWN_COUNTRY',
                                field='country', status=404)
        scope_node = city_node or GeoNode.objects.filter(kind=COUNTRY, code=registry.code).first()
        body = {
            'country': country_block(registry),
            'status': markets.country_status(registry.code),
            'cities': _live_cities(registry.code),
            'languages': list(LANGUAGES.get(registry.code, ('en',))),
            # Additive. True only while a rider promo is live in this market; the rider app shows
            # its promo-code entry only then, and reads a missing key as false.
            'promos_available': rider_promos.available_in_market(registry.code, city_node),
        }
        if app:
            # Additive. True only while a referral offer is live for this app in this market; the
            # app shows its referral entry (and the rider's sign-up code field) only then, and
            # reads a missing key as false. Named with `app=rider|driver`, so absent otherwise.
            body['referrals_available'] = referrals.available_in_market(
                registry.code, app, city_node)
    policy = versions.public_policy()
    body.update({
        'flags': flags.resolved_for(scope_node),
        'app_versions': {a: policy[a] for a in audiences},
        'notices': [notices.public_dict(n) for aud in audiences
                    for n in notices.active_for(aud, scope_node)],
        'generated_at': timezone.now().isoformat(),
    })
    return body


# Registry iteration kept explicit for the structural test that no country is special-cased.
KNOWN_COUNTRIES = tuple(sorted(COUNTRIES))
