"""Multi-country support, asserted where a client actually reads it.

WHY THIS FILE EXISTS SEPARATELY FROM test_multi_country.py
----------------------------------------------------------
`tests/test_multi_country.py` has 77 passing tests and `qa/multi_country_drill.py`
priced and receipted a real ride in each market with 33/33 checks green. Then a
quote against deployed QA came back like this:

    estimated_fare   '135.22'
    currency         None
    tax_mode         None
    tax_percent      None
    tax_label        None
    tax_amount       None
    per_trip_fee     None
    distance_unit    None

Every one of the seven market fields was null over HTTP while the service layer
had been producing all of them correctly all along. Three layers rebuild the fare
payload field by field -- `quote_fare`, the `estimate_amount` wrapper, and the
view's own response dict -- and the service-level tests could only see the first.
This is the second time that exact shape of defect has shipped here; the first was
`fare_adjustments` (see tests/test_fare_api_decomposition.py).

So the rule these tests encode: **verify at the boundary a consumer reads, not at
the layer that is convenient to call.** An API, a websocket event, a payout
refusal, a notification string.

WHAT IS COVERED
---------------
  * the estimate-fare HTTP response, in both markets
  * the withdrawal endpoint's minimum (it had its own hardcoded Rs.500 beside the
    market-aware function, which was imported and never called)
  * the online-payment currency guard
  * the reissued receipt, whose body was a copy of the original India-only tax
    arithmetic
  * night surge resolved in the zone's timezone, not the deployment's
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.driver.models import Driver, Vehicle, VehicleType, WithdrawalRequest
from servers.pricing.models import RateCard, ServiceZone
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider, Wallet

User = get_user_model()

pytestmark = pytest.mark.django_db

QUOTE_URL = '/api/v1/ride/estimate-fare/'
WITHDRAW_URL = '/api/v1/driver/withdrawals/request/'

HYD_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[78.30, 17.40], [78.50, 17.40], [78.50, 17.50],
                     [78.30, 17.50], [78.30, 17.40]]],
}
SF_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-122.52, 37.70], [-122.35, 37.70], [-122.35, 37.84],
                     [-122.52, 37.84], [-122.52, 37.70]]],
}

# Pickup and a drop a little north of it, inside the same polygon.
HYD = ('17.4450', '78.3800', '17.4660', '78.3800')
SF = ('37.7749', '-122.4194', '37.7900', '-122.4194')

_n = iter(range(1, 9999))


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def vehicle_type():
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return vt


@pytest.fixture
def india_zone():
    return ServiceZone.objects.create(
        code='A-IN-HYD', name='Hyderabad', country='IN', state_code='TG',
        city='Hyderabad', zone_type='city', polygon_geojson=HYD_POLYGON,
        priority=50, is_active=True,
    )


@pytest.fixture
def us_zone():
    return ServiceZone.objects.create(
        code='A-US-SFO', name='San Francisco', country='US', state_code='CA',
        city='San Francisco', zone_type='city', polygon_geojson=SF_POLYGON,
        priority=50, is_active=True, timezone_name='America/Los_Angeles',
        per_trip_fee=Decimal('0.50'), per_trip_fee_label='City access fee',
    )


def _card(zone, vt, base, per_dist, per_min, min_fare, tax):
    return RateCard.objects.create(
        zone=zone, vehicle_type=vt, base_fare=Decimal(base),
        per_km_fare=Decimal(per_dist), per_min_fare=Decimal(per_min),
        min_fare=Decimal(min_fare), commission_percent=Decimal('20.00'),
        gst_percent=Decimal(tax),
    )


def _rider_client(calling_code='91'):
    i = next(_n)
    user = User.objects.create_user(
        phone_number=f'+{calling_code}9{i:09d}'[:16], role='rider',
        username=f'ar{i}', email=f'ar{i}@example.test')
    Rider.objects.create(user_id=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client, user


def _quote(client, coords, km='8', minutes='20', vehicle_type='sedan'):
    p_lat, p_lng, d_lat, d_lng = coords
    resp = client.post(QUOTE_URL, {
        'pickup_lat': p_lat, 'pickup_long': p_lng,
        'destination_lat': d_lat, 'destination_long': d_lng,
        'distance_km': km, 'duration_min': minutes,
        'vehicle_type': vehicle_type,
    }, format='json')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body.get('data', body)


# ===========================================================================
# The quote, over HTTP
# ===========================================================================

def test_the_quote_api_states_the_market_in_india(india_zone, vehicle_type):
    _card(india_zone, vehicle_type, '30', '12', '2', '50', '5.00')
    client, _ = _rider_client('91')
    payload = _quote(client, HYD)

    assert payload['currency'] == 'INR'
    assert payload['distance_unit'] == 'km'

    fb = payload['fare_breakdown']
    assert fb['tax_mode'] == 'inclusive'
    assert fb['tax_label'] == 'GST'
    assert fb['tax_percent'] == '5.000'
    # Inclusive: stated, and already inside estimated_fare.
    assert Decimal(fb['tax_amount']) > Decimal('0')
    assert fb['per_trip_fee'] == '0.00'


def test_the_quote_api_states_the_market_in_the_us(us_zone, vehicle_type):
    _card(us_zone, vehicle_type, '2.50', '1.75', '0.35', '8.00', '8.625')
    client, _ = _rider_client('1')
    payload = _quote(client, SF)

    assert payload['currency'] == 'USD'
    assert payload['distance_unit'] == 'mi'

    fb = payload['fare_breakdown']
    assert fb['tax_mode'] == 'additive'
    assert fb['tax_label'] == 'Sales tax'
    assert fb['tax_percent'] == '8.625'
    assert Decimal(fb['tax_amount']) > Decimal('0')
    assert fb['per_trip_fee'] == '0.50'


def test_no_market_field_arrives_null_over_http(us_zone, vehicle_type):
    """The literal regression. Every one of these was null against live QA."""
    _card(us_zone, vehicle_type, '2.50', '1.75', '0.35', '8.00', '8.625')
    client, _ = _rider_client('1')
    payload = _quote(client, SF)
    fb = payload['fare_breakdown']

    for key, value in (
        ('currency', payload.get('currency')),
        ('distance_unit', payload.get('distance_unit')),
        ('tax_mode', fb.get('tax_mode')),
        ('tax_label', fb.get('tax_label')),
        ('tax_percent', fb.get('tax_percent')),
        ('tax_amount', fb.get('tax_amount')),
        ('per_trip_fee', fb.get('per_trip_fee')),
    ):
        assert value is not None, f'{key} is null at the HTTP boundary'


def test_the_additive_total_closes_in_the_http_response(us_zone, vehicle_type):
    """A rider adding up the panel must reach the amount they are charged."""
    _card(us_zone, vehicle_type, '2.50', '1.75', '0.35', '8.00', '8.625')
    client, _ = _rider_client('1')
    payload = _quote(client, SF)
    fb = payload['fare_breakdown']

    metered = sum(
        (Decimal(fb[k]) for k in ('base_fare', 'distance_fare', 'time_fare')),
        Decimal('0'),
    )
    adjustments = sum(
        (Decimal(a['amount']) for a in payload['fare_adjustments']),
        Decimal('0'),
    )
    assert metered + adjustments == Decimal(payload['estimated_fare'])
    # And the tax and the fee are among those named lines, not silent.
    codes = {a['code'] for a in payload['fare_adjustments']}
    assert 'tax' in codes and 'per_trip_fee' in codes


def test_india_quotes_over_http_exactly_what_it_quoted_before(
        india_zone, vehicle_type):
    """The pinned Indian arithmetic, through the API this time.

    `estimated_fare` is NOT pinned to a literal here, deliberately: the fare is
    computed from the SERVER-validated distance, not the client's claim, which
    is the fare-tampering fix and must stay that way. So the assertion is
    30 + 12/km + 2/min against the distance the server itself returned.
    """
    _card(india_zone, vehicle_type, '30', '12', '2', '50', '5.00')
    client, _ = _rider_client('91')
    payload = _quote(client, HYD, km='10', minutes='20')

    km = Decimal(str(payload['validated_km']))
    minutes = Decimal(str(payload['validated_min']))
    fb = payload['fare_breakdown']
    assert fb['base_fare'] == '30.00'
    assert Decimal(fb['distance_fare']) == (km * Decimal('12')).quantize(
        Decimal('0.01'))
    assert Decimal(fb['time_fare']) == (minutes * Decimal('2')).quantize(
        Decimal('0.01'))

    codes = {a['code'] for a in payload['fare_adjustments']}
    assert 'tax' not in codes, 'India gained an additive tax line over HTTP'
    assert 'per_trip_fee' not in codes


# ===========================================================================
# Payouts, at the endpoint rather than the service
# ===========================================================================

def _driver_client(vt, calling_code='91', currency='INR', balance='50.00'):
    i = next(_n)
    user = User.objects.create_user(
        phone_number=f'+{calling_code}8{i:09d}'[:16], role='driver',
        username=f'ad{i}')
    driver = Driver.objects.create(user_id=user, approved=True, status='online')
    vehicle = Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt,
                                     vehicle_number=f'AC{i:05d}')
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    Wallet.objects.create(user_id=user, scope=Wallet.SCOPE_DRIVER,
                          balance=Decimal(balance), currency=currency)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client, driver


def test_the_withdrawal_endpoint_uses_the_us_minimum_not_indias(vehicle_type):
    """The endpoint had its own `amount < 500` and an ₹ sign.

    `validate_withdrawal_amount` -- which resolves both the threshold and the
    symbol from the driver's market -- was imported at the top of the view and
    never called. So a US driver was refused anything under $500 and shown a
    rupee sign beside a dollar balance. $25 is above the $10 US minimum and far
    below Rs.500, so only the old code refuses it for being too small.
    """
    client, _ = _driver_client(vehicle_type, calling_code='1', currency='USD',
                               balance='500.00')
    resp = client.post(WITHDRAW_URL, {'amount': '25.00'}, format='json')

    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert WithdrawalRequest.objects.count() == 1


def test_the_withdrawal_endpoint_still_enforces_indias_minimum(vehicle_type):
    """The control. Rs.100 is below India's Rs.500 and must still be refused,
    in rupees."""
    client, _ = _driver_client(vehicle_type, calling_code='91', currency='INR',
                               balance='5000.00')
    resp = client.post(WITHDRAW_URL, {'amount': '100.00'}, format='json')

    assert resp.status_code == 400, resp.content
    message = str(resp.json())
    assert '500' in message
    assert '₹' in message, 'India lost its own currency symbol'
    assert WithdrawalRequest.objects.count() == 0


def test_a_us_withdrawal_is_refused_by_the_upi_rail_rather_than_mispaid(
        vehicle_type):
    """UPI settles in INR and the payout call hardcodes currency="INR".

    A dollar-denominated withdrawal routed down that path would dispatch the
    NUMBER as rupees. It must be refused before the gateway is touched.
    """
    from servers.driver.services import initiate_upi_payout

    _, driver = _driver_client(vehicle_type, calling_code='1', currency='USD',
                               balance='500.00')
    driver.upi_id = 'someone@okhdfcbank'
    driver.save(update_fields=['upi_id'])

    withdrawal = WithdrawalRequest.objects.create(
        driver=driver, amount=Decimal('100.00'), status='approved')
    assert withdrawal.currency == 'USD'

    assert initiate_upi_payout(withdrawal, driver) is False
    withdrawal.refresh_from_db()
    assert 'USD' in withdrawal.failure_reason
    assert not withdrawal.payout_reference_id


# ===========================================================================
# Online payment: the gateway must not be handed a currency it cannot settle
# ===========================================================================

def test_an_online_order_is_refused_when_the_gateway_cannot_settle_it(
        us_zone, vehicle_type):
    """A $30.83 fare sent to a rupee-only gateway as `currency='INR'` charges
    about a ninetieth of what is owed, silently. Refusing is the only safe
    answer while no US gateway exists."""
    from servers.payments.views import _online_payment_currency

    class _RupeeGateway:
        def get_name(self):
            return 'cashfree'

        def supported_currencies(self):
            return ('INR',)

    rider_client, rider = _rider_client('1')
    trip = Trip.objects.create(
        user_id=rider, status_id=_status('completed'), zone=us_zone,
        pickup_lat=Decimal(SF[0]), pickup_long=Decimal(SF[1]),
        destination_lat=Decimal(SF[2]), destination_long=Decimal(SF[3]),
        estimated_fare=Decimal('30.83'), payment_method='online',
    )
    assert trip.currency == 'USD'

    currency, error = _online_payment_currency(_RupeeGateway(), trip)
    assert currency is None
    assert error is not None and error.status_code == 503


def test_an_indian_online_order_is_unaffected(india_zone, vehicle_type):
    """The control: the live market must still reach the gateway, in INR."""
    from servers.payments.views import _online_payment_currency

    class _RupeeGateway:
        def get_name(self):
            return 'cashfree'

        def supported_currencies(self):
            return ('INR',)

    _, rider = _rider_client('91')
    trip = Trip.objects.create(
        user_id=rider, status_id=_status('completed'), zone=india_zone,
        pickup_lat=Decimal(HYD[0]), pickup_long=Decimal(HYD[1]),
        destination_lat=Decimal(HYD[2]), destination_long=Decimal(HYD[3]),
        estimated_fare=Decimal('190.00'), payment_method='online',
    )
    currency, error = _online_payment_currency(_RupeeGateway(), trip)
    assert error is None
    assert currency == 'INR'


# ===========================================================================
# The reissued receipt -- the copy of the tax arithmetic
# ===========================================================================

def test_a_reissued_us_receipt_taxes_the_way_the_original_did(
        us_zone, vehicle_type, settings):
    """`reissue_receipt` carried its own copy of the inclusive-GST arithmetic
    with India's 5% as the fallback rate. A reissued US receipt therefore
    stated a tax that was never charged, at the wrong rate, with no currency.
    """
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    from servers.ride.receipts import issue_receipt, reissue_receipt

    _card(us_zone, vehicle_type, '2.50', '1.75', '0.35', '8.00', '8.625')
    _, rider = _rider_client('1')
    now = timezone.now()
    trip = Trip.objects.create(
        user_id=rider, status_id=_status('completed'), zone=us_zone,
        pickup_lat=Decimal(SF[0]), pickup_long=Decimal(SF[1]),
        destination_lat=Decimal(SF[2]), destination_long=Decimal(SF[3]),
        estimated_fare=Decimal('29.33'), payment_method='cash',
        accepted_at=now, started_at=now, completed_at=now,
    )

    original = issue_receipt(trip)
    reissued = reissue_receipt(trip)

    assert reissued.version == original.version + 1
    assert reissued.currency == 'USD'
    assert reissued.gst_amount == original.gst_amount, (
        'the reissued receipt states a different tax than the original'
    )
    assert 'Sales tax' in reissued.html_body
    assert 'GST' not in reissued.html_body


# ===========================================================================
# Night surge belongs to the zone's clock
# ===========================================================================

@pytest.mark.parametrize('utc_hour,expect_night', [
    # 19:00 UTC == 12:00 noon in San Francisco, and 00:30 in Kolkata.
    # Resolved against the deployment timezone this was "night" and the rider
    # paid the multiplier at midday.
    (19, False),
    # 09:00 UTC == 02:00 in San Francisco: genuinely night, and NOT night in
    # Kolkata (14:30), so the old code missed it.
    (9, True),
])
def test_night_surge_follows_the_zones_clock_not_the_deployments(
        us_zone, vehicle_type, utc_hour, expect_night):
    from datetime import datetime, timezone as dt_timezone

    from servers.pricing.services import _is_night_now

    card = _card(us_zone, vehicle_type, '2.50', '1.75', '0.35', '8.00', '8.625')
    at = datetime(2026, 3, 20, utc_hour, 0, tzinfo=dt_timezone.utc)
    assert _is_night_now(card, at=at) is expect_night


def test_night_surge_in_india_is_exactly_what_it_was(india_zone, vehicle_type):
    """India's zone timezone IS the deployment timezone, so nothing moves:
    23:30 IST is night, 12:00 IST is not."""
    from datetime import datetime, timezone as dt_timezone

    from servers.pricing.services import _is_night_now

    card = _card(india_zone, vehicle_type, '30', '12', '2', '50', '5.00')
    # 18:00 UTC == 23:30 IST
    assert _is_night_now(
        card, at=datetime(2026, 3, 20, 18, 0, tzinfo=dt_timezone.utc)) is True
    # 06:30 UTC == 12:00 IST
    assert _is_night_now(
        card, at=datetime(2026, 3, 20, 6, 30, tzinfo=dt_timezone.utc)) is False


# ===========================================================================
# The offer a driver decides on
# ===========================================================================

def test_the_dispatch_offer_carries_a_currency_and_a_distance_unit(
        us_zone, vehicle_type):
    """A driver accepts or declines on a number. Without a currency it is not
    a price, and "12.4 km" is not a distance an American driver reads."""
    from servers.ride.dispatch import _offer_payload

    _, rider = _rider_client('1')
    trip = Trip.objects.create(
        user_id=rider, status_id=_status('requested'), zone=us_zone,
        pickup_lat=Decimal(SF[0]), pickup_long=Decimal(SF[1]),
        destination_lat=Decimal(SF[2]), destination_long=Decimal(SF[3]),
        estimated_fare=Decimal('29.33'),
        estimated_distance_km=Decimal('12.4'),
        estimated_duration_min=Decimal('18'),
        payment_method='cash',
    )
    payload = _offer_payload(trip, 'A Rider', 'sedan')

    assert payload['currency'] == 'USD'
    assert payload['distance_unit'] == 'mi'
    # And the shape the driver app already reads is untouched.
    assert payload['estimated_fare'] == '29.33'
    assert payload['distance_km'] == '12.4'


def test_the_indian_offer_is_unchanged_apart_from_the_additions(
        india_zone, vehicle_type):
    from servers.ride.dispatch import _offer_payload

    _, rider = _rider_client('91')
    trip = Trip.objects.create(
        user_id=rider, status_id=_status('requested'), zone=india_zone,
        pickup_lat=Decimal(HYD[0]), pickup_long=Decimal(HYD[1]),
        destination_lat=Decimal(HYD[2]), destination_long=Decimal(HYD[3]),
        estimated_fare=Decimal('190.00'),
        estimated_distance_km=Decimal('10'),
        estimated_duration_min=Decimal('20'),
        payment_method='cash',
    )
    payload = _offer_payload(trip, 'A Rider', 'sedan')

    assert payload['currency'] == 'INR'
    assert payload['distance_unit'] == 'km'
    assert payload['estimated_fare'] == '190.00'


# ===========================================================================
# The fallback road-network factors
# ===========================================================================

def test_the_fallback_duration_is_not_hyderabad_traffic_in_california(
        us_zone, india_zone):
    """Reached only when the Maps API is unavailable -- but duration is a
    priced component, so 25 km/h applied to a US trip inflates the time fare
    by about 60%."""
    from servers.ride.utils import _road_network_factors

    us_factor, us_speed = _road_network_factors(
        Decimal(SF[0]), Decimal(SF[1]))
    in_factor, in_speed = _road_network_factors(
        Decimal(HYD[0]), Decimal(HYD[1]))

    assert (in_factor, in_speed) == (1.4, 25.0), (
        'India\'s fallback factors moved, which reprices every fallback quote'
    )
    assert us_speed > in_speed
    assert us_factor < in_factor


def test_an_unknown_point_falls_back_to_the_deployment_defaults():
    """No zone, no country -- and a failed quote is worse than an approximate
    one, so this must not raise."""
    from servers.ride.utils import (
        FALLBACK_AVG_SPEED_KMH, ROAD_DISTANCE_FACTOR, _road_network_factors,
    )

    assert _road_network_factors(Decimal('0'), Decimal('0')) == (
        ROAD_DISTANCE_FACTOR, FALLBACK_AVG_SPEED_KMH)
