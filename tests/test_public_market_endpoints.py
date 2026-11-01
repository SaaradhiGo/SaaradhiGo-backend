"""The two public reads a market-neutral client needs before anyone signs in.

  GET /pricing/countries/   what a market looks like (from base/countries.py)
  GET /pricing/zones/       where we actually operate (from the database)

WHY THESE MATTER TOGETHER
-------------------------
`base/countries.py` has been the single source of truth for calling codes,
currencies and units since the multi-country work. No client could read it,
so the website hardcoded `+91` into a sign-up form on a platform meant to
run in more than one country. These tests pin the read that removes the
excuse, and pin that the registry -- not the client -- decides.
"""

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

COUNTRIES_URL = '/api/v1/pricing/countries/'


def _get():
    r = APIClient().get(COUNTRIES_URL)
    return r, (r.json().get('data') if r.content else {})


class TestCountriesEndpoint:
    def test_it_is_public(self):
        # A sign-up form needs it before anybody has an account.
        r, _ = _get()
        assert r.status_code == 200

    def test_it_lists_every_supported_country(self):
        from base.countries import COUNTRIES

        _, data = _get()
        codes = {c['code'] for c in data['countries']}

        assert codes == set(COUNTRIES.keys()), (
            'the endpoint must reflect the registry, not a second list'
        )

    def test_each_country_carries_what_a_form_needs(self):
        _, data = _get()

        for country in data['countries']:
            assert country['calling_code'], country
            assert country['currency'] and country['currency_symbol']
            assert country['symbol_position'] in ('prefix', 'suffix')
            assert country['distance_unit'] in ('km', 'mi')
            assert country['national_number_lengths']

    def test_the_values_come_from_the_registry(self):
        from base.countries import get_country

        _, data = _get()
        india = next(c for c in data['countries'] if c['code'] == 'IN')

        assert india['calling_code'] == get_country('IN').calling_code
        assert india['currency'] == get_country('IN').currency

    def test_it_names_the_default_only_when_exactly_one_country_is_live(self):
        # Module 15: never India by default. The default is the SOLE live country, else null.
        from servers.platform_ops.markets import default_phone_country

        _, data = _get()
        assert data['default_country'] == default_phone_country()

    def test_more_than_one_market_is_offered(self):
        # If this ever drops to one, a client that renders a selector should
        # still work -- but the platform claim of multi-market would be false.
        _, data = _get()
        assert len(data['countries']) >= 2

    def test_it_is_ordered_for_a_dropdown(self):
        _, data = _get()
        names = [c['name'] for c in data['countries']]
        assert names == sorted(names)


class TestZonesEndpointStillPublic:
    """The website's 'where we operate' depends on this staying open."""

    def test_zones_are_readable_without_an_account(self):
        r = APIClient().get('/api/v1/pricing/zones/')
        assert r.status_code == 200
        assert 'zones' in r.json().get('data', {})


class TestVehicleTypesAtAPoint:
    """What a booking screen may offer, and where it may not offer anything.

    A category without an active RateCard for the zone cannot be quoted. If a
    client shows it anyway, the rider taps it, `estimate-fare` fails, and the
    product looks broken for a category it should never have offered.
    """

    URL = '/api/v1/pricing/vehicle-types/'

    def test_a_point_outside_every_zone_is_not_an_error(self):
        # "We do not operate here" is a real answer a booking screen must be
        # able to render as an empty state rather than a failure.
        r = APIClient().get(self.URL, {'lat': 51.5074, 'lng': -0.1278})

        assert r.status_code == 200
        data = r.json()['data']
        assert data['serviceable'] is False
        assert data['zone'] is None
        assert data['vehicle_types'] == []

    def test_missing_coordinates_are_refused(self):
        r = APIClient().get(self.URL)
        assert r.status_code == 400
        assert r.json()['error']['code'] == 'MISSING_FIELDS'

    def test_it_is_public(self):
        # Needed before a rider signs in, to show what is on offer.
        assert APIClient().get(
            self.URL, {'lat': 51.5, 'lng': -0.12}).status_code == 200

    def test_a_served_point_returns_its_market(self, db):
        from django.utils import timezone as dj_tz

        from servers.driver.models import VehicleType
        from servers.pricing.models import RateCard, ServiceZone

        zone = ServiceZone.objects.filter(is_active=True).first()
        if zone is None:
            pytest.skip('no service zone in this database')

        card = (
            RateCard.objects
            .filter(zone=zone, is_active=True,
                    effective_from__lte=dj_tz.now())
            .select_related('vehicle_type')
            .first()
        )
        if card is None or card.vehicle_type is None:
            pytest.skip('no active rate card for the zone')

        # A point the zone certainly contains: its own polygon's first vertex
        # is on the boundary, so use the centroid-ish average instead.
        coords = (zone.polygon_geojson or {}).get('coordinates') or []
        if not coords or not coords[0]:
            pytest.skip('zone has no polygon')
        ring = coords[0]
        lng = sum(p[0] for p in ring) / len(ring)
        lat = sum(p[1] for p in ring) / len(ring)

        r = APIClient().get(self.URL, {'lat': lat, 'lng': lng})
        data = r.json()['data']

        assert r.status_code == 200
        assert data['serviceable'] is True
        # The market, resolved the same way /pricing/zones/ resolves it.
        assert data['zone']['currency']
        assert data['zone']['distance_unit'] in ('km', 'mi')
        offered = {v['type'] for v in data['vehicle_types']}
        assert card.vehicle_type.type in offered, (
            'a category with an active card must be offered'
        )

    def test_only_categories_with_an_active_card_are_offered(self, db):
        from servers.driver.models import VehicleType
        from servers.pricing.models import RateCard, ServiceZone

        zone = ServiceZone.objects.filter(is_active=True).first()
        if zone is None:
            pytest.skip('no service zone in this database')

        coords = (zone.polygon_geojson or {}).get('coordinates') or []
        if not coords or not coords[0]:
            pytest.skip('zone has no polygon')
        ring = coords[0]
        lng = sum(p[0] for p in ring) / len(ring)
        lat = sum(p[1] for p in ring) / len(ring)

        # A catalogue entry with no card anywhere must not be offered.
        orphan = VehicleType.objects.create(
            type='orphan-category', description='no rate card')

        r = APIClient().get(self.URL, {'lat': lat, 'lng': lng})
        offered = {v['type'] for v in r.json()['data']['vehicle_types']}

        assert orphan.type not in offered, (
            'offering an unpriced category makes estimate-fare fail on tap'
        )
