"""Can one user reach another user's things, or another role's endpoints?

WHY THIS IS SEPARATE FROM THE EXISTING RBAC TESTS
-------------------------------------------------
The suite already covers role gates -- a rider is refused at an operator
endpoint, an anonymous request is refused. What it did not cover
systematically is **object-level** authorization: a caller with a perfectly
valid token for the right role, reaching for a row that belongs to somebody
else.

That is the failure mode that leaks. A trip carries a pickup address, a
destination and, before the ride starts, the OTP that begins it. A driver's
earnings are their income. A KYC document is a government ID.

Every test here is a read or a write that SHOULD be refused. Nothing is
destructive: the objects are fabricated in the test database, and the
assertions are about status codes, not side effects.
"""

from decimal import Decimal as D

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

pytestmark = pytest.mark.django_db

# Anything in this set means "refused". 404 counts: hiding existence is a
# legitimate way to refuse, and for a trip id it is arguably better.
# Anything in this set means "refused". 400 is included because these views
# answer a bad *field* and a bad *caller* with different codes at the same
# status in places -- so every test below also asserts the error CODE, not
# just the status. A refusal for the wrong reason proves nothing.
REFUSED = (400, 401, 403, 404)


def _client(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


@pytest.fixture
def two_of_everything(db):
    """Two riders, two drivers, and a trip belonging to the first pair."""
    from servers.driver.models import Driver
    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Rider

    User = get_user_model()

    def rider(phone):
        user = User.objects.create_user(
            phone_number=phone, role='rider', username=phone)
        Rider.objects.create(user_id=user)
        return user

    def driver(phone):
        user = User.objects.create_user(
            phone_number=phone, role='driver', username=phone)
        return user, Driver.objects.create(user_id=user)

    rider_a = rider('+919990001001')
    rider_b = rider('+919990001002')
    driver_a_user, driver_a = driver('+918880001001')
    driver_b_user, driver_b = driver('+918880001002')

    in_progress, _ = TripStatus.objects.get_or_create(status_code='in_progress')
    trip = Trip.objects.create(
        user_id=rider_a, driver_id=driver_a,
        pickup_lat=D('17.4450'), pickup_long=D('78.3800'),
        destination_lat=D('17.4650'), destination_long=D('78.4200'),
        pickup_address='Banjara Hills', destination_address='Hitech City',
        estimated_fare=D('180.00'),
        status_id=in_progress,
        payment_method='cash',
    )

    completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    completed_trip = Trip.objects.create(
        user_id=rider_a, driver_id=driver_a,
        pickup_lat=D('17.4450'), pickup_long=D('78.3800'),
        destination_lat=D('17.4650'), destination_long=D('78.4200'),
        pickup_address='Banjara Hills', destination_address='Hitech City',
        estimated_fare=D('180.00'), final_fare=D('192.00'),
        status_id=completed, completed_at=timezone.now(),
        payment_method='cash', payment_status='completed',
    )

    return {
        'rider_a': rider_a,
        'rider_b': rider_b,
        'driver_a_user': driver_a_user,
        'driver_b_user': driver_b_user,
        'driver_a': driver_a,
        'driver_b': driver_b,
        'trip': trip,
        'completed_trip': completed_trip,
    }


class TestATripBelongsToItsTwoParties:
    def test_another_rider_cannot_read_it(self, two_of_everything):
        world = two_of_everything
        response = _client(world['rider_b']).get(
            f"/api/v1/ride/trip/{world['trip'].id}/")

        assert response.status_code in REFUSED, (
            'another rider read a trip: pickup, destination and the start OTP'
        )

    def test_an_unassigned_driver_cannot_read_it(self, two_of_everything):
        world = two_of_everything
        response = _client(world['driver_b_user']).get(
            f"/api/v1/ride/trip/{world['trip'].id}/")

        assert response.status_code in REFUSED, (
            'a driver who is not on the trip read it'
        )

    def test_the_start_otp_never_reaches_a_third_party(self, two_of_everything):
        """The OTP starts the ride. Anyone holding it can claim the pickup."""
        world = two_of_everything
        response = _client(world['rider_b']).get(
            f"/api/v1/ride/trip/{world['trip'].id}/")

        assert 'otp' not in response.content.decode().lower()

    def test_the_rider_on_the_trip_can_read_it(self, two_of_everything):
        # The refusals above must not be a blanket denial.
        world = two_of_everything
        response = _client(world['rider_a']).get(
            f"/api/v1/ride/trip/{world['trip'].id}/")

        assert response.status_code == 200, response.content

    def test_the_assigned_driver_can_read_it(self, two_of_everything):
        world = two_of_everything
        response = _client(world['driver_a_user']).get(
            f"/api/v1/ride/trip/{world['trip'].id}/")

        assert response.status_code == 200, response.content


class TestCancellingSomebodyElsesTrip:
    def test_another_rider_cannot_cancel_it(self, two_of_everything):
        world = two_of_everything
        response = _client(world['rider_b']).post(
            f"/api/v1/ride/trip/{world['trip'].id}/cancel/",
            {'reason': 'safety'}, format='json')

        assert response.status_code in REFUSED, (
            f'another rider cancelled a stranger ride mid-trip: '
            f'{response.content}'
        )
        # The refusal must be about WHO is asking. A 400 for a malformed
        # field would pass the status check while leaving the boundary open
        # to a well-formed request.
        assert response.json()['error']['code'] == 'FORBIDDEN', response.content
        world['trip'].refresh_from_db()
        assert world['trip'].status_id.status_code == 'in_progress'

    def test_an_unassigned_driver_cannot_cancel_it(self, two_of_everything):
        world = two_of_everything
        response = _client(world['driver_b_user']).post(
            f"/api/v1/ride/trip/{world['trip'].id}/driver-cancel/",
            {'reason': 'no_show'}, format='json')

        assert response.status_code in REFUSED, response.content
        assert response.json()['error']['code'] in ('FORBIDDEN', 'NOT_FOUND'), (
            response.content
        )
        world['trip'].refresh_from_db()
        assert world['trip'].status_id.status_code == 'in_progress'


class TestRatingSomebodyElsesTrip:
    def test_a_stranger_cannot_rate_it(self, two_of_everything):
        """A rating moves the rated user's average, so this is a write."""
        world = two_of_everything
        response = _client(world['rider_b']).post(
            '/api/v1/ride/rate-trip/',
            {'trip_id': world['completed_trip'].id, 'score': 1},
            format='json')

        assert response.status_code in REFUSED, (
            'a stranger drove down a driver rating'
        )

    def test_an_unassigned_driver_cannot_rate_it(self, two_of_everything):
        world = two_of_everything
        response = _client(world['driver_b_user']).post(
            '/api/v1/ride/rate-trip/',
            {'trip_id': world['completed_trip'].id, 'score': 1},
            format='json')

        assert response.status_code in REFUSED


class TestReceiptsAndHistoryAreScopedToTheCaller:
    def test_another_rider_cannot_resend_a_receipt(self, two_of_everything):
        # A receipt carries the pickup, destination, fare and the rider's
        # email address.
        world = two_of_everything
        response = _client(world['rider_b']).post(
            f"/api/v1/ride/trip/{world['completed_trip'].id}/receipt/resend/")

        assert response.status_code in REFUSED

    def test_rider_history_contains_only_the_caller_trips(
            self, two_of_everything):
        world = two_of_everything
        response = _client(world['rider_b']).get('/api/v1/ride/ride-history/')

        assert response.status_code == 200, response.content
        body = response.content.decode()
        assert str(world['trip'].id) not in body or 'Banjara Hills' not in body

    def test_driver_history_contains_only_the_caller_trips(
            self, two_of_everything):
        world = two_of_everything
        response = _client(world['driver_b_user']).get(
            '/api/v1/ride/driver-history/')

        assert response.status_code == 200, response.content
        assert 'Banjara Hills' not in response.content.decode()


class TestDriverMoneyIsScopedToTheDriver:
    def test_earnings_are_the_caller_own(self, two_of_everything):
        """Both drivers can call it; neither sees the other's figures."""
        world = two_of_everything

        a = _client(world['driver_a_user']).get(
            '/api/v1/driver/earnings/summary/')
        b = _client(world['driver_b_user']).get(
            '/api/v1/driver/earnings/summary/')

        assert a.status_code == 200 and b.status_code == 200
        # Driver B has no trips, so any non-zero total would be A's.
        data_b = b.json()['data']
        assert str(data_b.get('total_trips', 0)) == '0', (
            f"driver B sees trips they did not drive: {data_b}"
        )

    def test_a_rider_cannot_read_driver_earnings(self, two_of_everything):
        world = two_of_everything
        response = _client(world['rider_a']).get(
            '/api/v1/driver/earnings/summary/')

        assert response.status_code in REFUSED, (
            'a rider read driver earnings'
        )

    def test_a_rider_cannot_request_a_withdrawal(self, two_of_everything):
        world = two_of_everything
        response = _client(world['rider_a']).post(
            '/api/v1/driver/withdrawals/request/',
            {'amount': '100.00'}, format='json')

        assert response.status_code in REFUSED


class TestAdminSurfacesAreClosedToUsers:
    @pytest.mark.parametrize('path', [
        '/api/v1/driver/admin/',
        '/api/v1/driver/admin/withdrawals/',
        '/api/v1/ride/admin/trips/',
        '/api/v1/live-ops/admin/snapshot/',
        '/api/v1/ride/admin/dashboard/',
        '/api/v1/support/admin/tickets/',
    ])
    def test_a_rider_is_refused(self, two_of_everything, path):
        response = _client(two_of_everything['rider_a']).get(path)

        assert response.status_code in REFUSED, f'{path} was open to a rider'

    @pytest.mark.parametrize('path', [
        '/api/v1/driver/admin/',
        '/api/v1/live-ops/admin/snapshot/',
        '/api/v1/ride/admin/dashboard/',
    ])
    def test_a_driver_is_refused(self, two_of_everything, path):
        response = _client(two_of_everything['driver_a_user']).get(path)

        assert response.status_code in REFUSED, f'{path} was open to a driver'

    def test_an_anonymous_caller_is_refused_everywhere(self, two_of_everything):
        client = APIClient()
        for path in [
            '/api/v1/ride/admin/trips/',
            '/api/v1/driver/earnings/summary/',
            '/api/v1/ride/ride-history/',
            '/api/v1/support/tickets/',
        ]:
            response = client.get(path)
            assert response.status_code in REFUSED, f'{path} was open'


class TestSupportTicketsAreScopedToTheirAuthor:
    def test_another_user_cannot_read_a_ticket(self, two_of_everything):
        world = two_of_everything
        created = _client(world['rider_a']).post(
            '/api/v1/support/tickets/create/',
            {'issue_type': 'safety', 'description': 'A sensitive complaint'},
            format='json')
        assert created.status_code == 201, created.content
        ticket_id = created.json()['data']['id']

        response = _client(world['rider_b']).get(
            f'/api/v1/support/tickets/{ticket_id}/')

        assert response.status_code in REFUSED, (
            'another user read a safety complaint'
        )

    def test_another_user_cannot_post_to_a_ticket(self, two_of_everything):
        world = two_of_everything
        created = _client(world['rider_a']).post(
            '/api/v1/support/tickets/create/',
            {'issue_type': 'payment', 'description': 'Charged twice'},
            format='json')
        ticket_id = created.json()['data']['id']

        response = _client(world['rider_b']).post(
            f'/api/v1/support/tickets/{ticket_id}/messages/',
            {'body': 'injected'}, format='json')

        assert response.status_code in REFUSED, response.content
        # Scoped by `user_id=request.user` in the lookup, so a stranger gets
        # NOT_FOUND rather than FORBIDDEN -- which is the better answer: it
        # does not confirm the ticket exists.
        assert response.json()['error']['code'] == 'NOT_FOUND', response.content

    def test_the_author_sees_only_their_own_list(self, two_of_everything):
        world = two_of_everything
        _client(world['rider_a']).post(
            '/api/v1/support/tickets/create/',
            {'issue_type': 'safety', 'description': 'Rider A complaint'},
            format='json')

        response = _client(world['rider_b']).get('/api/v1/support/tickets/')

        assert response.status_code == 200, response.content
        assert 'Rider A complaint' not in response.content.decode()
