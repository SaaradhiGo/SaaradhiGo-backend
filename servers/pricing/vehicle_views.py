"""Which vehicle categories a rider can actually be quoted at a location.

  GET /pricing/vehicle-types/?lat=&lng=

WHY NOT JUST LIST VehicleType
-----------------------------
`VehicleType` is a catalogue of every category the platform knows about.
Offering all of them to a rider is how a client ends up showing a category
that cannot be priced: a fare needs an active `RateCard` for
(zone, vehicle_type), and a zone that has no card for "suv" cannot quote one.
The rider taps it, `estimate-fare` fails, and the product looks broken for a
category it should never have shown.

So this answers the question a booking screen actually has -- "what can I
offer at this pickup point?" -- by starting from the zone and returning only
the categories with a card active right now.

MARKET-AWARE BY CONSTRUCTION
The zone carries the currency and distance unit, so the response says which
market the rider is standing in. A client that renders categories without
that has to guess whether a price is rupees or dollars.

Out of service is a first-class answer: no zone at the point returns an empty
list and `serviceable: false`, which is what a booking screen needs to show
an honest empty state instead of an error.
"""

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from base.utils import error_response, success_response

from .models import RateCard
from base.money import round_fare

from .services import find_zone_for_point


@api_view(['GET'])
@permission_classes([AllowAny])
def list_vehicle_types(request):
    """Vehicle categories priced at a point, with the market they price in."""
    lat = request.query_params.get('lat')
    lng = request.query_params.get('lng')

    if lat is None or lng is None:
        return error_response(
            code='MISSING_FIELDS',
            message='lat and lng are required.',
            field='lat',
            issue='A pickup point is needed to know which zone prices apply',
            status=status.HTTP_400_BAD_REQUEST,
        )

    zone = find_zone_for_point(lat, lng)
    if zone is None:
        # Not an error. "We do not operate here yet" is a real answer, and a
        # booking screen must be able to say so rather than show a failure.
        return success_response(
            {
                'serviceable': False,
                'zone': None,
                'vehicle_types': [],
            },
            status.HTTP_200_OK,
        )

    now = timezone.now()
    cards = (
        RateCard.objects
        .filter(zone=zone, is_active=True, effective_from__lte=now)
        .filter(models_q_effective_to(now))
        .select_related('vehicle_type')
        # The newest live version first, so `seen` keeps it.
        .order_by('vehicle_type__type', '-effective_from')
    )
    currency = zone.currency or zone.country_profile.currency

    seen = {}
    for card in cards:
        vt = card.vehicle_type
        if vt is None or vt.type in seen:
            continue
        seen[vt.type] = {
            'type': vt.type,
            'description': vt.description or '',
            # Rider seats, or None when the class has none set.
            'seats': vt.seats,
            # "from ₹X" on the public site: the card's minimum fare, rounded as fares are.
            'min_fare': str(round_fare(card.min_fare, currency)) if card.min_fare is not None else None,
        }

    return success_response(
        {
            'serviceable': True,
            'zone': {
                'code': zone.code,
                'name': zone.name,
                'country': zone.country,
                # RESOLVED, not raw. `currency`, `distance_unit` and
                # `timezone_name` are blank on a zone that inherits them from
                # its country, and a client reading the blank would have no
                # currency at all. `/pricing/zones/` resolves them the same
                # way; the two must not disagree about the same zone.
                'currency': currency,
                'distance_unit': (
                    zone.distance_unit or zone.country_profile.distance_unit
                ),
                'timezone': (
                    zone.timezone_name
                    or zone.country_profile.default_timezone
                ),
                # 14 Nov: what a visitor may pay with at this pickup (Idaho: card only), the
                # same answer a signed-in rider gets, before any account restriction.
                'payment_methods': _visitor_methods(lat, lng),
            },
            'vehicle_types': list(seen.values()),
        },
        status.HTTP_200_OK,
    )


def _visitor_methods(lat, lng):
    from servers.ride.rider_booking import payment_options

    return payment_options(None, lat, lng)['methods']


def models_q_effective_to(now):
    """`effective_to IS NULL OR effective_to > now`, the card's own rule."""
    from django.db.models import Q
    return Q(effective_to__isnull=True) | Q(effective_to__gt=now)
