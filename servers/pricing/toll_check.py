"""Google's toll price as a cross-check on the catalogue (owner, 26 Nov). Never on every quote.

Routes TOLLS is called only when a quoted route crosses a catalogue toll plaza or tolled road, once per
route per 24 h (shared cache), off the request path (a Celery task), and not while the country's Maps
budget is spent. A nightly sample of the most common routes finds tolls missing from the catalogue.
Each call is metered as `routes_tolls`, so toll spend shows in the existing Maps cost page.
"""

import logging
from datetime import timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

SKU = 'routes_tolls'
TTL_SECONDS = 24 * 3600
MISMATCH_RATIO = Decimal('0.10')
#: Cars only: Google prices tolls for a car, so the check compares a car class's catalogue amount.
CAR_CLASSES = ('sedan', 'mini', 'suv')


def _key(country, pickup, drop):
    from servers.ride.route_cache import route_cache_key

    return 'toll_check:' + route_cache_key(pickup[0], pickup[1], drop[0], drop[1], country)


def _price_minor(toll_info, currency):
    from base.money import normalize_currency, to_minor

    for price in (toll_info or {}).get('estimatedPrice') or []:
        if normalize_currency(price.get('currencyCode') or '') == normalize_currency(currency):
            units = Decimal(str(price.get('units') or 0)) + Decimal(int(price.get('nanos') or 0)) / Decimal(10 ** 9)
            return to_minor(units, currency)
    return None


def google_tolls(pickup, drop, currency, country=''):
    """{has_tolls, price_minor (None: Google has no price), polyline} or None when Maps cannot answer."""
    from servers.ride import maps_meter, maps_utils

    if maps_utils.maps_fake() or not maps_utils.maps_configured():
        return None
    try:
        maps_meter.record(SKU, country=country)
        response = requests.post(
            'https://routes.googleapis.com/directions/v2:computeRoutes',
            headers={'Content-Type': 'application/json', 'X-Goog-Api-Key': maps_utils._api_key(),
                     'X-Goog-FieldMask': 'routes.travelAdvisory.tollInfo,routes.polyline.encodedPolyline'},
            json={'origin': {'location': {'latLng': {'latitude': float(pickup[0]), 'longitude': float(pickup[1])}}},
                  'destination': {'location': {'latLng': {'latitude': float(drop[0]), 'longitude': float(drop[1])}}},
                  'travelMode': 'DRIVE', 'routingPreference': 'TRAFFIC_UNAWARE',
                  'extraComputations': ['TOLLS'],
                  'routeModifiers': {'vehicleInfo': {'emissionType': 'GASOLINE'}}},
            timeout=6)
        response.raise_for_status()
        route = ((response.json() or {}).get('routes') or [None])[0]
    except (requests.RequestException, ValueError):
        logger.warning('toll check: Google did not answer', exc_info=True)
        return None
    if not route:
        return None
    info = (route.get('travelAdvisory') or {}).get('tollInfo')
    return {'has_tolls': info is not None, 'price_minor': _price_minor(info, currency),
            'polyline': (route.get('polyline') or {}).get('encodedPolyline', '')}


def cached_tolls(country, pickup, drop, currency):
    key = _key(country, pickup, drop)
    hit = cache.get(key)
    if hit is not None:
        return hit
    result = google_tolls(pickup, drop, currency, country)
    if result is not None:
        result['checked_at'] = timezone.now().isoformat()
        cache.set(key, result, TTL_SECONDS)
    return result


def _budget_spent(country):
    try:
        from servers.ride.maps_budget import degrade_level

        return degrade_level(country) > 0
    except Exception:  # noqa: BLE001
        return False


def schedule(zone, pickup, drop):
    """From a quote that crosses a catalogue toll: queue one check per route per 24 h. Never raises."""
    try:
        from servers.pricing.commission import country_code_of_zone

        country = country_code_of_zone(zone)
        if _budget_spent(country) or not cache.add(_key(country, pickup, drop) + ':queued', 1, TTL_SECONDS):
            return False
        from servers.pricing.tasks import check_route_tolls

        check_route_tolls.delay(zone.id, [float(pickup[0]), float(pickup[1])], [float(drop[0]), float(drop[1])])
        return True
    except Exception:  # noqa: BLE001
        logger.warning('toll check: could not queue', exc_info=True)
        return False


def catalogue_toll_minor(zone, pickup, drop, polyline):
    """(class, minor) for the first car class with toll items on this route; (None, 0) when none."""
    from servers.pricing.commission import country_code_of_zone

    from . import fee_plans, trip_charges
    from .trip_charge_models import TOLL_KINDS

    country = country_code_of_zone(zone)
    items = [i for i in trip_charges.items_in_force(country, fee_plans.local_day(country, None, zone)).values()
             if i.kind in TOLL_KINDS]
    if not items:
        return None, 0
    points = trip_charges.route_points(polyline, pickup, drop)
    currency = items[0].currency
    for vt in CAR_CLASSES:
        lines = trip_charges.match(items, points, pickup, drop, vt, currency)
        if lines:
            return vt, sum(x['amount_minor'] for x in lines)
    return None, 0


def check_route(zone, pickup, drop, *, source='quote'):
    """Compare Google with the catalogue for one route; raise an ops alert on a gap. Returns the finding."""
    from base.countries import get_country
    from servers.pricing.commission import city_node_of, country_code_of_zone

    country = country_code_of_zone(zone)
    currency = getattr(zone, 'currency', None) or get_country(country).currency
    google = cached_tolls(country, pickup, drop, currency)
    if google is None:
        return {'status': 'no_answer'}
    vt, ours = catalogue_toll_minor(zone, pickup, drop, google.get('polyline') or '')
    theirs = google.get('price_minor')
    finding = {'status': 'ok', 'class': vt, 'catalogue_minor': ours, 'google_minor': theirs,
               'google_has_tolls': google.get('has_tolls'), 'source': source}
    if google.get('has_tolls') and not ours:
        finding['status'] = 'missing'
    elif theirs is not None and ours and abs(theirs - ours) > MISMATCH_RATIO * ours:
        finding['status'] = 'mismatch'
    elif ours and not google.get('has_tolls'):
        finding['status'] = 'mismatch'
    if finding['status'] in ('missing', 'mismatch'):
        _alert(zone, city_node_of(getattr(zone, 'geo_node', None)), country, currency, pickup, drop, finding)
    return finding


def _alert(zone, city, country, currency, pickup, drop, finding):
    from servers.command_centre.models import TOLL_MISMATCH, TOLL_MISSING, WARNING
    from servers.command_centre.services import raise_alert

    from . import fee_plans

    money = (lambda m: fee_plans._money(m, currency) if m is not None else 'no price')
    if finding['status'] == 'missing':
        kind, title = TOLL_MISSING, (f'Google sees a toll ({money(finding["google_minor"])}) on a common route '
                                     f'with no toll in the catalogue')
    else:
        kind, title = TOLL_MISMATCH, (f'Toll differs by more than 10%: catalogue {money(finding["catalogue_minor"])}'
                                      f', Google {money(finding["google_minor"])}')
    try:
        raise_alert(kind, title=title, severity=WARNING, city=city, source_type='toll_route',
                    source_id=_key(country, pickup, drop)[len('toll_check:'):][:120],
                    payload={'pickup': [round(pickup[0], 4), round(pickup[1], 4)],
                             'drop': [round(drop[0], 4), round(drop[1], 4)], 'zone': zone.code, **finding})
    except Exception:  # noqa: BLE001
        logger.exception('toll check: could not raise the alert')


def nightly_sample(limit=None, days=7):
    """The most common booked routes per country, checked against Google. Returns {country: [findings]}."""
    from django.db.models import Count
    from django.db.models.functions import Round

    from servers.pricing.commission import country_code_of_zone
    from servers.pricing.models import ServiceZone
    from servers.ride.models import Trip

    from .trip_charges import _enabled

    limit = limit or int(getattr(settings, 'TOLL_SAMPLE_ROUTES', 20))
    since = timezone.now() - timedelta(days=days)
    rows = (Trip.objects.filter(requested_at__gte=since, zone__isnull=False)
            .annotate(plat=Round('pickup_lat', 3), plng=Round('pickup_long', 3),
                      dlat=Round('destination_lat', 3), dlng=Round('destination_long', 3))
            .values('zone_id', 'plat', 'plng', 'dlat', 'dlng').annotate(n=Count('id')).order_by('-n')[:limit * 5])
    zones = {z.id: z for z in ServiceZone.objects.filter(id__in={r['zone_id'] for r in rows})}
    out, taken = {}, {}
    for r in rows:
        zone = zones.get(r['zone_id'])
        if zone is None or not _enabled(zone):
            continue
        country = country_code_of_zone(zone)
        if taken.get(country, 0) >= limit or _budget_spent(country):
            continue
        taken[country] = taken.get(country, 0) + 1
        pickup, drop = (float(r['plat']), float(r['plng'])), (float(r['dlat']), float(r['dlng']))
        out.setdefault(country, []).append(check_route(zone, pickup, drop, source='nightly'))
    return out
