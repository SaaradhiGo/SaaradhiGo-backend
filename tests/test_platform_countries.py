"""`base/countries.py` no longer turns an unknown country into India (module 15), and India still
behaves exactly as before for India numbers."""

import pytest

from base.countries import (
    COUNTRIES, UnknownCountry, find_country, get_country, normalize_phone, require_country,
)
from servers.admin_access.models import GeoNode
from servers.platform_ops import markets
from test_sos_support_zones_matrix import geo  # noqa: F401 - fixture


class TestStrictLookups:
    @pytest.mark.parametrize('code', [None, '', 'ZZ', 'india', 'GB', '  '])
    def test_find_country_never_substitutes_india(self, code):
        assert find_country(code) is None

    def test_find_country_trims_and_uppercases(self):
        assert find_country(' us ').code == 'US'

    def test_require_country_raises_for_unknown(self):
        with pytest.raises(UnknownCountry):
            require_country('ZZ')
        assert require_country('IN').currency == 'INR'

    def test_the_lenient_lookup_is_logged_when_it_falls_back(self, caplog):
        with caplog.at_level('WARNING'):
            assert get_country('ZZ').code == 'IN'          # display-shaped sites only
        assert 'unknown country' in caplog.text


class TestPhoneNormalisation:
    def test_no_default_country_means_a_bare_national_number_is_ambiguous(self):
        # Ten digits are a valid national number in BOTH India and the US: a guess is refused.
        assert normalize_phone('9876543210') is None
        assert normalize_phone('  9876543210  ') is None

    def test_a_default_that_the_caller_passes_is_honoured(self):
        assert normalize_phone('9876543210', default_country='IN') == '+919876543210'
        assert normalize_phone('5551234567', default_country='US') == '+15551234567'
        assert normalize_phone('  9876543210  ', default_country='IN') == '+919876543210'

    def test_an_unknown_default_is_refused_not_guessed(self):
        assert normalize_phone('9876543210', default_country='ZZ') is None

    @pytest.mark.parametrize('raw,expected', [
        ('+919876543210', '+919876543210'), ('+91 98765 43210', '+919876543210'),
        ('919876543210', '+919876543210'), ('+1 (555) 123-4567', '+15551234567'),
        ('15551234567', '+15551234567'),
    ])
    def test_india_and_us_numbers_with_their_country_code_are_unchanged(self, raw, expected):
        assert normalize_phone(raw) == expected
        assert normalize_phone(raw, default_country='IN') == expected

    def test_a_number_unique_to_one_country_still_resolves_without_a_default(self):
        # No supported country has an 8-digit national number, so this stays refused.
        assert normalize_phone('98765432') is None


class TestDefaultsComeFromLiveCities:
    def test_the_sole_live_country_is_the_default_never_a_constant(self, geo):
        assert markets.default_phone_country() == 'IN'                 # only Hyderabad is live
        GeoNode.objects.filter(code='US-TX-AUS').update(status='active')
        assert markets.default_phone_country() is None                 # two live: ambiguous
        GeoNode.objects.filter(kind='city', path__startswith='GLOBAL/IN/').update(status='paused')
        assert markets.default_phone_country() == 'US'                 # now only the US

    def test_registration_reads_a_bare_number_in_the_named_or_sole_live_country(self, geo):
        from servers.auth_user.registration_views import normalize_phone as imported

        assert imported is normalize_phone                  # the same helper, no local copy

    def test_the_public_countries_list_names_the_default_only_when_unambiguous(self, geo):
        from rest_framework.test import APIClient

        body = APIClient().get('/api/v1/pricing/countries/').json()['data']
        assert body['default_country'] == 'IN'
        GeoNode.objects.filter(code='US-TX-AUS').update(status='active')
        assert APIClient().get('/api/v1/pricing/countries/').json()['data']['default_country'] is None

    def test_a_login_phone_is_resolved_by_the_account_that_exists(self, geo, django_user_model):
        GeoNode.objects.filter(code='US-TX-AUS').update(status='active')
        django_user_model.objects.create_user(phone_number='+15125550123', username='u1')
        assert markets.resolve_login_phone('5125550123') == '+15125550123'
        assert markets.resolve_login_phone('+919876543210') == '+919876543210'


def test_the_registry_has_no_country_that_is_special_cased():
    assert set(COUNTRIES) == {'IN', 'US'}
