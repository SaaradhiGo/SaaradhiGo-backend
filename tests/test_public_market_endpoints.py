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

    def test_it_names_the_default_so_a_form_need_not_guess(self):
        from base.countries import DEFAULT_COUNTRY_CODE

        _, data = _get()
        assert data['default_country'] == DEFAULT_COUNTRY_CODE

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
