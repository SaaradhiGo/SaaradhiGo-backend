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

from base.countries import COUNTRIES, DEFAULT_COUNTRY_CODE
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
            'default_country': DEFAULT_COUNTRY_CODE,
        },
        status.HTTP_200_OK,
    )
