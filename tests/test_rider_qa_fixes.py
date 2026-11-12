"""Rider QA fixes (11 Nov): email-only sign-up, the mobile at first booking, booking options,
the per-city driver search time, state-aware cancel reasons and the invoice's vehicle line."""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from servers.auth_user import phone_proof
from servers.ride import rider_booking as rb
from servers.ride.models import Trip, TripStatus
from tests.conftest import ensure_zone_covering

User = get_user_model()
pytestmark = pytest.mark.django_db

REGISTER = '/api/v1/auth/email/register/'
LOGIN = '/api/v1/auth/email/login/'
FORGOT = '/api/v1/auth/password/forgot/'
PASSWORD = 'Monsoon-Ferry-2026!'
HYD = (17.445, 78.38)


def _signup(email='new.rider@deeptrics.com', **extra):
    body = {'full_name': 'Asha Rao', 'email': email, 'password': PASSWORD, 'role': 'rider', **extra}
    return APIClient().post(REGISTER, body, format='json')


class TestEmailOnlySignUp:
    def test_it_creates_a_working_account_and_signs_in(self):
        r = _signup()
        assert r.status_code == 201, r.content.decode()
        data = r.json()['data']
        assert data['token'] and data['refresh_token']
        assert data['user']['phone_number'] is None
        assert data['user']['phone_verified'] is False
        user = User.objects.get(email__iexact='new.rider@deeptrics.com')
        assert user.role == 'rider' and hasattr(user, 'rider')

    def test_an_unconfirmed_rider_can_sign_in(self, monkeypatch):
        monkeypatch.setattr('base.auth_email.delivery_is_configured', lambda: (True, ''))
        _signup()
        r = APIClient().post(LOGIN, {'email': 'new.rider@deeptrics.com', 'password': PASSWORD},
                             format='json')
        assert r.status_code == 200, r.content.decode()
        assert r.json()['data']['user']['email_verified'] is False

    def test_a_taken_email_is_said_so_on_the_field(self):
        _signup()
        r = _signup()
        assert r.status_code == 409
        assert r.json()['error']['code'] == 'AUTH_EMAIL_IN_USE'
        assert r.json()['error']['details']['field'] == 'email'

    def test_a_short_password_is_refused(self):
        r = _signup(password='short-pw')
        assert r.status_code == 400 and r.json()['error']['details']['field'] == 'password'

    def test_an_unconfirmed_rider_can_reset_by_email(self, monkeypatch):
        monkeypatch.setattr('base.auth_email.delivery_is_configured', lambda: (True, ''))
        _signup()
        mail.outbox.clear()
        APIClient().post(FORGOT, {'email': 'new.rider@deeptrics.com'}, format='json')
        assert len(mail.outbox) == 1


class TestTheMobileAtFirstBooking:
    def test_an_email_rider_must_verify_before_booking(self):
        _signup()
        user = User.objects.get(email__iexact='new.rider@deeptrics.com')
        assert rb.phone_required(user)

        client = APIClient()
        client.force_authenticate(user)
        phone = '+919870004242'
        r = client.post('/api/v1/auth/phone/change/',
                        {'phone_number': phone, 'phone_proof': phone_proof.issue(phone, 'rider')},
                        format='json')
        assert r.status_code == 200, r.content.decode()
        user.refresh_from_db()
        assert user.phone_number == phone and user.username == phone
        assert not rb.phone_required(user)

    def test_a_rider_with_a_number_is_not_asked(self):
        user = User.objects.create_user(phone_number='+919870004243', username='+919870004243',
                                        role='rider')
        assert not rb.phone_required(user)


@pytest.fixture
def rider():
    u = User.objects.create_user(phone_number='+919870004250', username='+919870004250',
                                 role='rider', full_name='Harish Goud')
    from servers.rider.models import Rider
    Rider.objects.create(user_id=u)
    return u


def _trip(rider, status='requested', **kw):
    st, _ = TripStatus.objects.get_or_create(status_code=status)
    return Trip.objects.create(
        user_id=rider, status_id=st, pickup_lat=Decimal('17.445'), pickup_long=Decimal('78.38'),
        destination_lat=Decimal('17.455'), destination_long=Decimal('78.39'),
        estimated_fare=Decimal('150.00'), **kw)


class TestBookingOptions:
    def test_cash_only_without_online_payment(self, rider, settings):
        settings.ONLINE_PAYMENT_ZONES = ''
        ensure_zone_covering(*HYD)
        assert rb.payment_options(rider, *HYD) == {
            'methods': ['cash'], 'default': 'cash', 'online_unavailable_reason': ''}

    def test_online_needs_the_city_and_a_gateway(self, rider, settings):
        ensure_zone_covering(*HYD)
        from servers.pricing.services import find_zone_for_point
        settings.ONLINE_PAYMENT_ZONES = find_zone_for_point(*HYD).code
        with mock.patch('servers.payments.payment_gateways.factory.get_payment_gateway_for_payments',
                        return_value=False):
            out = rb.payment_options(rider, *HYD)
        assert out['methods'] == ['cash'] and out['online_unavailable_reason'] == 'gateway_not_configured'
        with mock.patch('servers.payments.payment_gateways.factory.get_payment_gateway_for_payments',
                        return_value=True):
            _trip(rider, payment_method='upi')
            out = rb.payment_options(rider, *HYD)
        assert out['methods'] == ['upi', 'card', 'cash'] and out['default'] == 'upi'

    def test_the_endpoint_answers_for_the_choose_screen(self, rider):
        ensure_zone_covering(*HYD)
        client = APIClient()
        client.force_authenticate(rider)
        with mock.patch('servers.redis_client.nearby_drivers', return_value=[('driver:7', 800.0)]):
            r = client.get('/api/v1/ride/booking-options/?lat=17.445&lng=78.38&types=auto,sedan')
        assert r.status_code == 200, r.content.decode()
        data = r.json()['data']
        assert data['pickup_min'] == {'auto': 4, 'sedan': 4}
        assert data['search_timeout_seconds'] == 180
        assert data['phone_required'] is False


class TestTheSearchTime:
    def test_the_default_is_three_minutes(self):
        assert rb.search_timeout_seconds(None) == 180

    def test_a_city_sets_its_own_and_its_areas_inherit_it(self):
        city = ensure_zone_covering(*HYD)
        city.dispatch_timeout_seconds = 240
        city.save(update_fields=['dispatch_timeout_seconds'])
        from servers.pricing.models import ServiceZone
        area = ServiceZone.objects.create(
            code='TEST-AREA', name='Area', zone_type='zone', country='IN', parent=city,
            polygon_geojson=city.polygon_geojson, priority=50, is_active=True)
        assert rb.search_timeout_seconds(city) == 240
        assert rb.search_timeout_seconds(area) == 240

    def test_the_trip_detail_says_when_the_search_ends(self, rider):
        trip = _trip(rider)
        client = APIClient()
        client.force_authenticate(rider)
        data = client.get(f'/api/v1/ride/trip/{trip.id}/').json()['data']
        assert data['search_expires_at']


class TestCancelReasons:
    def test_a_driver_reason_is_refused_before_assignment(self, rider):
        trip = _trip(rider)
        client = APIClient()
        client.force_authenticate(rider)
        r = client.post(f'/api/v1/ride/trip/{trip.id}/cancel/', {'reason': 'no_show'}, format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'INVALID_REASON'

    def test_changed_plans_cancels_before_assignment(self, rider):
        trip = _trip(rider)
        client = APIClient()
        client.force_authenticate(rider)
        r = client.post(f'/api/v1/ride/trip/{trip.id}/cancel/', {'reason': 'changed_plans'},
                        format='json')
        assert r.status_code == 200, r.content.decode()


def test_the_vehicle_line_names_the_car():
    from servers.ride.receipts import vehicle_line
    v = mock.Mock(color='White', brand='Maruti Suzuki', model='Dzire', vehicle_number='TS 09 QT 0059')
    assert vehicle_line(v) == 'White Maruti Suzuki Dzire · TS 09 QT 0059'


def test_a_generic_pickup_label_needs_a_street_address():
    assert rb.needs_street_address('Current location')
    assert not rb.needs_street_address('Plot 12, Road 4, Madhapur')
    with mock.patch('servers.redis_client.get_cached_map_data',
                    return_value={'results': [{'formatted_address': 'Road 4, Madhapur, Hyderabad'}]}):
        assert rb.street_address(17.445, 78.38) == 'Road 4, Madhapur, Hyderabad'
    assert timezone.now()
