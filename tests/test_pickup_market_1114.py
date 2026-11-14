"""The pickup point decides the market (owner, 14 Nov).

1. Place suggestions come from EVERY operating country (IN, US), ranked nearest the visitor:
   their location, else the country chosen in the header (`market`), else their account's
   market. "Boise State University" typed in Hyderabad must reach Google with both countries.
2. An India account booking from Boise gets USD, miles, card only and DeepTrics LLC: the
   market, currency, units, company, payment methods and regulation all come from the pickup.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.pricing.models import RateCard, ServiceZone
from servers.ride import maps_utils
from tests.test_maps_multi_country import (
    KEY, LEGACY_AUTOCOMPLETE_OK, NEW_AUTOCOMPLETE_OK, FakeResponse, _zone,
)
from tests.test_usa_idaho_1114 import BOISE, idaho  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db
User = get_user_model()
URL = '/api/v1/ride/maps/geocode'
HYDERABAD = '17.38500,78.48670'


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    # The maps cache is Redis, which outlives a test run locally: never read it here.
    with mock.patch('servers.ride.views.get_cached_map_data', return_value=None):
        yield


@pytest.fixture
def two_markets(idaho):  # noqa: F811
    ServiceZone.objects.filter(is_active=True).exclude(code='US-ID-TV').update(is_active=False)
    _zone('IN-TG-HYD-T', 'Hyderabad', 'IN', 17.385, 78.4867, delta=0.3)
    return idaho


def _search(data, user=None):
    client = APIClient()
    if user is not None:
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
        r = client.post(URL, {'input': 'Boise State University', **data}, format='json')
    assert r.status_code == 200, r.content
    return post.call_args.kwargs['json']


def _centre(body):
    c = body['locationBias']['circle']['center']
    return c['latitude'], c['longitude']


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_a_visitor_in_hyderabad_searches_india_and_the_usa_ranked_from_where_they_are(two_markets):
    body = _search({'location': HYDERABAD, 'market': 'IN'})
    assert body['includedRegionCodes'] == ['IN', 'US']
    assert 'locationRestriction' not in body
    assert _centre(body) == (pytest.approx(17.385), pytest.approx(78.4867))


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_with_no_location_the_header_country_ranks_the_suggestions(two_markets):
    body = _search({'market': 'US'})
    assert body['includedRegionCodes'] == ['IN', 'US']
    lat, lng = _centre(body)
    assert 42 < lat < 45 and -118 < lng < -115          # Treasure Valley, Idaho


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_with_no_location_and_no_header_choice_the_account_market_ranks_them(two_markets):
    india = User.objects.create_user(phone_number='+919000000101', role='rider')
    body = _search({}, user=india)
    assert body['includedRegionCodes'] == ['IN', 'US']
    lat, lng = _centre(body)
    assert 17 < lat < 18 and 78 < lng < 79              # Hyderabad


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_an_india_account_with_india_trips_still_finds_boise(two_markets):
    """11 Nov kept a signed-in search inside the last trip's city; that hid Boise from India."""
    india = User.objects.create_user(phone_number='+919000000102', role='rider')
    body = _search({'input': 'Boise State Broncos stadium'}, user=india)
    assert 'locationRestriction' not in body and 'US' in body['includedRegionCodes']


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='legacy')
def test_the_legacy_api_also_searches_both_countries(two_markets):
    with mock.patch.object(maps_utils.requests, 'get',
                           return_value=FakeResponse(LEGACY_AUTOCOMPLETE_OK)) as get:
        APIClient().post(URL, {'input': 'Boise State', 'location': HYDERABAD}, format='json')
    assert get.call_args.kwargs['params']['components'] == 'country:in|country:us'


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_a_zone_still_restricts_when_a_caller_asks_for_it(two_markets):
    body = _search({'zone': 'IN-TG-HYD-T', 'location': HYDERABAD})
    assert 'locationRestriction' in body and body['includedRegionCodes'] == ['IN']


def test_an_india_account_booking_from_boise_gets_usd_miles_card_only_and_deeptrics_llc(idaho):  # noqa: F811
    from base.legal_entity import entity_for_zone
    from servers.pricing.regulation import rules_for_zone
    from servers.pricing.services import find_zone_for_point, quote_fare
    from servers.ride.rider_booking import payment_options

    zone, _ = idaho
    from servers.driver.models import VehicleType

    RateCard.objects.create(zone=zone, vehicle_type=VehicleType.objects.get(type='standard'),
                            base_fare=Decimal('2.50'), per_km_fare=Decimal('1.60'),
                            per_min_fare=Decimal('0.30'), min_fare=Decimal('8.00'),
                            commission_percent=Decimal('0'), gst_percent=Decimal('0'), is_active=True)
    india = User.objects.create_user(phone_number='+919000000103', role='rider')

    at = find_zone_for_point(*BOISE)
    assert at is not None and at.code == 'US-ID-TV'
    q = quote_fare(pickup_lat=BOISE[0], pickup_lon=BOISE[1], distance_km=Decimal('16.09344'),
                   duration_min=Decimal('20'), vehicle_type='standard', record_demand=False)
    assert (q['currency'], q['distance_unit']) == ('USD', 'mi')
    with override_settings(CARD_FAKE_ALLOWED=True, ONLINE_PAYMENT_ZONES='US-ID-TV'):
        methods = payment_options(india, *BOISE)['methods']
    assert 'cash' not in methods and methods, methods
    assert entity_for_zone(at)['legal_name'] == 'DeepTrics LLC'
    assert rules_for_zone(at)['cash_allowed'] is False
