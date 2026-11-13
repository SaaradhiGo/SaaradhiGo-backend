"""The country registry, readable by a client.

  GET /pricing/countries/    public: the markets this deployment supports

WHY THIS EXISTS
---------------
`base/countries.py` is already the single source of truth for everything
market-specific: calling code, currency and symbol, symbol position,
distance unit, tax mode, valid national number lengths. The backend uses it
to normalise phone numbers, stamp currencies and price zones.

No client could read it. So the website had two options: hardcode a dial code
-- which is how `+91` ended up baked into a form on a platform that is meant
to operate in more than one country -- or re-declare the registry in
TypeScript, which is the same mistake with an extra copy to drift.

This is the third option, and it is a read of data that already exists rather
than a second definition of it.

It is NOT a zone list. `/pricing/zones/` already answers "where do we
operate", from the database. This answers "what does this market look like",
from the registry. A country can be supported by the registry before a single
zone is live in it, which is exactly the state a new market starts in.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from base.countries import COUNTRIES
from base.utils import success_response


def _as_dict(country):
    return {
        'code': country.code,
        'name': country.name,
        'calling_code': country.calling_code,
        'currency': country.currency,
        'currency_symbol': country.currency_symbol,
        'symbol_position': country.symbol_position,
        'distance_unit': country.distance_unit,
        'national_number_lengths': list(country.national_number_lengths),
    }


def _default_country():
    from servers.platform_ops.markets import default_phone_country

    return default_phone_country()


@api_view(['GET'])
@permission_classes([AllowAny])
def list_countries(request):
    """Every country this deployment can operate in, and its defaults.

    Public and unauthenticated: a sign-up form needs it before anybody has an
    account. It discloses nothing -- the same registry ships in the source.
    """
    countries = [_as_dict(c) for c in COUNTRIES.values()]
    countries.sort(key=lambda c: c['name'])

    return success_response(
        {
            'countries': countries,
            # So a client does not have to guess which entry to preselect,
            # and the choice lives in one place rather than in every form.
            # The sole LIVE country, else null (module 15): never India by default. A client
            # with two live countries asks the user, or reads /app-config/.
            'default_country': _default_country(),
        },
        status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def public_fares(request):
    """GET /api/v1/pricing/fares/?country=IN (13 Nov): what the public Fares page shows.

    Per city zone (live or launching soon): each vehicle type's active rate card (base, per
    km or mile, per minute, minimum, night rate), the demand band the fare is held inside, the
    tax convention and the cancellation rule. Public figures only; never commission or costs."""
    from django.db.models import Q
    from django.utils import timezone

    from servers.admin_access.models import ACTIVE, PAUSED
    from servers.platform_ops.markets import city_of_zone

    from .models import RateCard, ServiceZone
    from .regulation import cancel_policy, limits_for_zone, state_code_of_zone

    country = str(request.query_params.get('country') or '').upper()[:2]
    zones = ServiceZone.objects.filter(is_active=True, parent__isnull=True)
    if country:
        zones = zones.filter(country=country)
    now = timezone.now()
    out = []
    for zone in zones.order_by('country', 'name'):
        # 13 Nov (owner): rates only for a city open in the console's launch gate. A zone no city
        # owns is not open anywhere, a paused city is not shown, a draft city is "Coming soon" with
        # no rates.
        city = city_of_zone(zone)
        if city is None or city.status == PAUSED:
            continue
        state, city_name = ('live' if city.status == ACTIVE else 'soon'), city.name
        if state == 'soon':
            out.append({'zone': zone.code, 'city': city_name, 'city_status': state, 'country': zone.country,
                        'currency': zone.currency or zone.country_profile.currency,
                        'distance_unit': zone.distance_unit or zone.country_profile.distance_unit,
                        'tax_mode': zone.tax_mode or zone.country_profile.tax_mode, 'tax_label': zone.tax_label,
                        'vehicles': [], 'cancellation': None})
            continue
        cards = (RateCard.objects.filter(zone=zone, is_active=True, effective_from__lte=now)
                 .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=now))
                 .select_related('vehicle_type').order_by('vehicle_type_id', '-version'))
        seen, rows = set(), []
        for card in cards:
            if card.vehicle_type_id in seen:
                continue
            seen.add(card.vehicle_type_id)
            band = limits_for_zone(zone, card.surge_cap_multiplier)
            rows.append({
                'vehicle': card.vehicle_type.type,
                'base_fare': str(card.base_fare), 'per_distance': str(card.per_km_fare),
                'per_minute': str(card.per_min_fare), 'minimum_fare': str(card.min_fare),
                'night_multiplier': str(card.night_surge_multiplier),
                'night_hours': [card.night_surge_start_hour, card.night_surge_end_hour],
                'demand_band': band.as_dict(),
            })
        policy = cancel_policy(zone.country, state_code_of_zone(zone))
        out.append({
            'zone': zone.code, 'city': city_name or zone.city or zone.name, 'city_status': state,
            'country': zone.country,
            'currency': zone.currency or zone.country_profile.currency,
            'distance_unit': zone.distance_unit or zone.country_profile.distance_unit,
            'tax_mode': zone.tax_mode or zone.country_profile.tax_mode, 'tax_label': zone.tax_label,
            'vehicles': rows,
            'cancellation': policy.as_dict() if policy else None,
        })
    return success_response({'cities': out}, status.HTTP_200_OK)
