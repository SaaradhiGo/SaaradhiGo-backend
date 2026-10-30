"""Capture the RIDE lifecycle's real response envelopes, for both apps.

WHY
---
`test_auth_contract_capture.py` exists because the rebuilt driver app read
`data['access']` while the server sends `token`, and its own tests agreed with
the app because they were fixtures written from the same wrong assumption. The
fix was to generate the contract from the real views and make both apps assert
against it.

Authentication was only the first boundary. Everything after it -- the fare
quote, the trip record, history, ratings, earnings, SOS -- has the same shape
of risk: two Flutter apps parsing envelopes nobody has pinned, with no test
that would notice a renamed field until a rider or a driver saw a blank
screen.

This captures those envelopes from the real views, the same way, into
`contracts/ride_contracts.json`. The apps assert against the copy they are
given; if a view's shape changes, that file changes and their tests fail --
and nobody can "fix" them by editing a fixture to match the app.

It also captures the REFUSALS, because object-level authorization is part of
the contract: a rider must not be able to read another rider's trip, and the
status code and envelope that says so is something the apps have to handle.

SAFETY
------
Everything runs against the test database with fabricated rows. No external
service is called: fare estimation uses the zone rate card and the haversine
fallback, and SOS fan-out is mocked.
"""

import json
import os
from decimal import Decimal as D
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from tests.contract_normalise import normalise

pytestmark = pytest.mark.django_db

CONTRACT_PATH = os.path.join('contracts', 'ride_contracts.json')

_captured = {}


def _record(name, note, response):
    """Record one response exactly as the view produced it."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = {'_non_json': True, 'content_type': response.get('Content-Type', '')}
    _captured[name] = {
        'note': note,
        'status_code': response.status_code,
        # Fixed stand-ins for timestamps and JWTs: see tests/contract_normalise.py.
        'body': normalise(body),
    }
    return body


def _client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


# The same polygon the launch-hardening tests use. A fare quote is refused
# outside every active service zone, so the capture needs one -- that refusal
# is itself captured, as `ride_estimate_fare_outside_zone`.
HYD_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[
        [78.30, 17.40], [78.50, 17.40], [78.50, 17.50],
        [78.30, 17.50], [78.30, 17.40],
    ]],
}


@pytest.fixture
def world(db):
    """A rider, a driver, a second rider, a service zone, and one trip."""
    from servers.driver.models import Driver
    from servers.pricing.models import RateCard, ServiceZone
    from servers.ride.models import VehicleType
    from servers.ride.models import FarePricing, Trip, TripStatus
    from servers.rider.models import Rider

    User = get_user_model()

    rider_user = User.objects.create_user(
        phone_number='+919990000001', role='rider', username='+919990000001')
    Rider.objects.create(user_id=rider_user)

    other_rider = User.objects.create_user(
        phone_number='+919990000002', role='rider', username='+919990000002')
    Rider.objects.create(user_id=other_rider)

    driver_user = User.objects.create_user(
        phone_number='+918880000001', role='driver', username='+918880000001')
    driver = Driver.objects.create(user_id=driver_user)

    vehicle_type, _ = VehicleType.objects.get_or_create(type='sedan')
    zone = ServiceZone.objects.create(
        code='IN-TG-HYD-CONTRACT', name='Hyderabad contract capture',
        zone_type='city', city='Hyderabad',
        polygon_geojson=HYD_POLYGON, priority=10,
    )
    RateCard.objects.create(
        zone=zone, vehicle_type=vehicle_type,
        base_fare=D('30'), per_km_fare=D('12'), per_min_fare=D('2'),
        min_fare=D('50'), commission_percent=D('22.00'),
    )

    completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver,
        pickup_lat=D('17.4450'), pickup_long=D('78.3800'),
        destination_lat=D('17.4650'), destination_long=D('78.4200'),
        pickup_address='Banjara Hills', destination_address='Hitech City',
        estimated_fare=D('180.00'), final_fare=D('192.00'),
        status_id=completed, completed_at=timezone.now(),
        payment_method='cash', payment_status='completed',
    )
    FarePricing.objects.create(
        trip_id=trip, base_fare=D('50'), distance_fare=D('110'),
        time_fare=D('32'), surge_multiplier=D('1.00'), total_fare=D('192'),
    )

    return {
        'rider': rider_user,
        'other_rider': other_rider,
        'driver_user': driver_user,
        'driver': driver,
        'trip': trip,
        'zone': zone,
    }


class TestCaptureTheRideLifecycle:
    """One test per boundary, each recording what the view really answered."""

    def test_fare_quote(self, world):
        client = _client_for(world['rider'])

        response = client.post('/api/v1/ride/estimate-fare/', {
            'pickup_lat': '17.4450', 'pickup_long': '78.3800',
            'destination_lat': '17.4650', 'destination_long': '78.4200',
            'distance_km': '5.2', 'duration_min': '18',
            'vehicle_type': 'sedan',
        }, format='json')

        body = _record('ride_estimate_fare',
                       'POST /ride/estimate-fare/ - the rider-facing quote',
                       response)
        assert response.status_code == 200, body

    def test_fare_quote_outside_every_service_zone(self, world):
        """The refusal a rider sees for an unserved city. Part of the contract."""
        client = _client_for(world['rider'])

        response = client.post('/api/v1/ride/estimate-fare/', {
            'pickup_lat': '51.5074', 'pickup_long': '-0.1278',
            'destination_lat': '51.5155', 'destination_long': '-0.1410',
            'distance_km': '5.2', 'duration_min': '18',
            'vehicle_type': 'sedan',
        }, format='json')

        body = _record('ride_estimate_fare_outside_zone',
                       'POST /ride/estimate-fare/ outside every active zone',
                       response)
        assert response.status_code == 400, body

    def test_trip_detail_as_the_rider(self, world):
        client = _client_for(world['rider'])

        response = client.get(f"/api/v1/ride/trip/{world['trip'].id}/")

        body = _record('ride_trip_detail_rider',
                       'GET /ride/trip/<id>/ as the rider on the trip',
                       response)
        assert response.status_code == 200, body

    def test_trip_detail_as_the_assigned_driver(self, world):
        client = _client_for(world['driver_user'])

        response = client.get(f"/api/v1/ride/trip/{world['trip'].id}/")

        body = _record('ride_trip_detail_driver',
                       'GET /ride/trip/<id>/ as the assigned driver',
                       response)
        assert response.status_code == 200, body

    def test_trip_detail_is_refused_to_another_rider(self, world):
        """Object-level authorization, captured because the apps must handle it.

        A rider reading another rider's trip would expose a pickup address, a
        destination and -- on some payloads -- the OTP that starts the trip.
        """
        client = _client_for(world['other_rider'])

        response = client.get(f"/api/v1/ride/trip/{world['trip'].id}/")

        body = _record('ride_trip_detail_denied_other_rider',
                       'GET /ride/trip/<id>/ as an unrelated rider - must be refused',
                       response)
        assert response.status_code in (403, 404), (
            f'another rider could read this trip: {body}'
        )

    def test_active_trip_when_there_is_none(self, world):
        client = _client_for(world['rider'])

        response = client.get('/api/v1/ride/active/')

        _record('ride_active_none',
                'GET /ride/active/ with no trip in flight - the crash-recovery call',
                response)
        assert response.status_code in (200, 404)

    def test_rider_history(self, world):
        client = _client_for(world['rider'])

        response = client.get('/api/v1/ride/ride-history/')

        body = _record('ride_history_rider',
                       'GET /ride/ride-history/ - the rider trip list',
                       response)
        assert response.status_code == 200, body

    def test_driver_history(self, world):
        client = _client_for(world['driver_user'])

        response = client.get('/api/v1/ride/driver-history/')

        body = _record('ride_history_driver',
                       'GET /ride/driver-history/ - the driver trip list',
                       response)
        assert response.status_code == 200, body

    def test_rate_a_completed_trip(self, world):
        client = _client_for(world['rider'])

        response = client.post('/api/v1/ride/rate-trip/', {
            'trip_id': world['trip'].id,
            'score': 5,
            'comments': 'Smooth ride',
        }, format='json')

        body = _record('ride_rate_trip',
                       'POST /ride/rate-trip/ - one rating per user per trip',
                       response)
        assert response.status_code in (200, 201), body

    def test_rating_the_same_trip_twice_is_refused(self, world):
        client = _client_for(world['rider'])
        client.post('/api/v1/ride/rate-trip/', {
            'trip_id': world['trip'].id, 'score': 5,
        }, format='json')

        response = client.post('/api/v1/ride/rate-trip/', {
            'trip_id': world['trip'].id, 'score': 1,
        }, format='json')

        _record('ride_rate_trip_duplicate',
                'POST /ride/rate-trip/ twice - the second must be refused',
                response)
        assert response.status_code >= 400

    def test_driver_earnings_summary(self, world):
        client = _client_for(world['driver_user'])

        response = client.get('/api/v1/driver/earnings/summary/')

        body = _record('driver_earnings_summary',
                       'GET /driver/earnings/summary/',
                       response)
        assert response.status_code == 200, body

    def test_driver_withdrawal_balance(self, world):
        # India's payout rules, as `seed_payout_config` leaves them on a deployed
        # environment: the contract below is the unchanged India response.
        from finance_world import seed_in_payout_config

        seed_in_payout_config()
        client = _client_for(world['driver_user'])

        response = client.get('/api/v1/driver/withdrawals/balance/')

        body = _record('driver_withdrawal_balance',
                       'GET /driver/withdrawals/balance/',
                       response)
        assert response.status_code == 200, body

    def test_driver_incentives(self, world):
        client = _client_for(world['driver_user'])

        response = client.get('/api/v1/driver/driver/incentives/')

        body = _record('driver_incentives',
                       'GET /driver/driver/incentives/ - note the doubled path segment',
                       response)
        assert response.status_code == 200, body

    def test_driver_elite_status(self, world):
        client = _client_for(world['driver_user'])

        response = client.get('/api/v1/driver/driver/elite-status/')

        body = _record('driver_elite_status',
                       'GET /driver/driver/elite-status/',
                       response)
        assert response.status_code == 200, body

    def test_driver_profile(self, world):
        client = _client_for(world['driver_user'])

        response = client.get('/api/v1/driver/driver/profile/')

        body = _record('driver_profile',
                       'GET /driver/driver/profile/',
                       response)
        assert response.status_code == 200, body

    def test_sos(self, world):
        """SOS, with the fan-out mocked so nothing leaves the test."""
        client = _client_for(world['rider'])

        with mock.patch('servers.sos.tasks.dispatch_sos.delay'):
            response = client.post('/api/v1/sos/', {
                'latitude': '12.9352',
                'longitude': '77.6245',
                'trip_id': world['trip'].id,
            }, format='json')

        body = _record('sos_create', 'POST /sos/ - rider emergency', response)
        assert response.status_code in (200, 201), body

    def test_receipt_resend_for_a_completed_trip(self, world, settings):
        settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
        settings.DEFAULT_FROM_EMAIL = 'no-reply@ci.saaradhigo.test'
        client = _client_for(world['rider'])

        response = client.post(
            f"/api/v1/ride/trip/{world['trip'].id}/receipt/resend/")

        _record('ride_receipt_resend',
                'POST /ride/trip/<id>/receipt/resend/',
                response)
        assert response.status_code in (200, 400, 404)


class TestWriteTheContract:
    """Runs last, writing what the tests above recorded."""

    def test_write_contract_file(self, world, settings):
        # Re-run every capture in one place so the file is complete even when
        # a single test is run in isolation. pytest gives no ordering
        # guarantee otherwise, and a half-written contract is worse than none.
        suite = TestCaptureTheRideLifecycle()
        suite.test_fare_quote(world)
        suite.test_fare_quote_outside_every_service_zone(world)
        suite.test_trip_detail_as_the_rider(world)
        suite.test_trip_detail_as_the_assigned_driver(world)
        suite.test_trip_detail_is_refused_to_another_rider(world)
        suite.test_active_trip_when_there_is_none(world)
        suite.test_rider_history(world)
        suite.test_driver_history(world)
        suite.test_rate_a_completed_trip(world)
        suite.test_rating_the_same_trip_twice_is_refused(world)
        suite.test_driver_earnings_summary(world)
        suite.test_driver_withdrawal_balance(world)
        suite.test_driver_incentives(world)
        suite.test_driver_elite_status(world)
        suite.test_driver_profile(world)
        suite.test_sos(world)
        suite.test_receipt_resend_for_a_completed_trip(world, settings)

        # The status vocabulary, from the model's own choices.
        #
        # Captured because this is the field that has already caused a blank
        # screen: the rider app did not map 'reached', so a rider whose
        # driver was waiting outside saw nothing on recovery. Both apps now
        # assert they map every code the server can produce, so a status
        # added here fails their tests rather than a rider's screen.
        from servers.ride.models import TripStatus as TripStatusModel

        # The choices live on the field, not on a class attribute.
        choices = TripStatusModel._meta.get_field('status_code').choices or []
        vocabulary = sorted({code for code, _label in choices})
        assert vocabulary, 'the status field declares no choices'

        os.makedirs(os.path.dirname(CONTRACT_PATH), exist_ok=True)
        payload = {
            'trip_status_vocabulary': vocabulary,
            '_README': (
                'GENERATED by tests/test_ride_contract_capture.py from real '
                'responses. Do not edit by hand -- regenerate. The rider and '
                'driver apps assert their parsing against this.'
            ),
            'endpoints': dict(sorted(_captured.items())),
        }
        with open(CONTRACT_PATH, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=False, default=str)

        assert len(payload['endpoints']) >= 17, (
            f"only captured {len(payload['endpoints'])} endpoints: "
            f"{sorted(payload['endpoints'])}"
        )
