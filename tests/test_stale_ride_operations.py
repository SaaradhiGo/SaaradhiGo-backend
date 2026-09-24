"""An operator must be able to resolve a stranded ride without an engineer.

Recovering QA trip 42 took a developer opening the driver's WebSocket and sending
`complete`. That is the thing this page removes, and these tests are about the
shape of what replaced it as much as whether it works.

The two safe actions:

    mark_reviewed   a human looked and judged the ride legitimate
    release_driver  repair the ephemeral availability marker from the database

The two that are deliberately absent, and must stay absent until someone decides
the money policy for abandoned rides:

    admin_complete  creates settlement, commission, wallet movement, receipt
    admin_cancel    raises the question of what the rider owes

Several tests here assert that absence. A control that invents money policy is
worse than no control, so "the button does not exist" is a property worth locking.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride import liveness
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

STALE_URL = '/stale-rides/'


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def operator(client):
    admin = User.objects.create_user(
        phone_number='+919544000001', role='admin',
        is_staff=True, is_superuser=True, password='not-a-real-password',
    )
    client.force_login(admin)
    return admin


@pytest.fixture
def stranded(settings):
    settings.TRIP_STALE_AFTER_SECONDS = 60
    rider_u = User.objects.create_user(phone_number='+919544000002', role='rider')
    Rider.objects.create(user_id=rider_u)
    driver_u = User.objects.create_user(phone_number='+919544000003', role='driver')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    driver = Driver.objects.create(user_id=driver_u, approved=True, status='online')
    v = Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt,
                               vehicle_number='TS09GG4444')
    driver.active_vehicle = v
    driver.save(update_fields=['active_vehicle'])

    trip = Trip.objects.create(
        user_id=rider_u, driver_id=driver, status_id=_status('in_progress'),
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.4550000'), destination_long=Decimal('78.3900000'),
        estimated_fare=Decimal('150.00'), payment_method='cash',
    )
    past = timezone.now() - timezone.timedelta(seconds=3600)
    Trip.objects.filter(id=trip.id).update(
        requested_at=past, accepted_at=past, started_at=past,
        last_driver_activity_at=past,
    )
    trip.refresh_from_db()
    liveness.flag_stale_trips()
    trip.refresh_from_db()
    return {'trip': trip, 'driver': driver}


class _FakeRedisState:
    def __init__(self, value=None):
        self.value = value

    def get(self, key):
        return None if self.value is None else str(self.value).encode()

    def set(self, key, value):
        self.value = value
        return True

    def delete(self, *keys):
        self.value = None
        return 1


# ---------------------------------------------------------------------------
# The queue is reachable and shows what an operator needs
# ---------------------------------------------------------------------------

def test_the_queue_requires_an_operator(client, stranded):
    """Unauthenticated access must not reach a page that can release drivers."""
    resp = client.get(STALE_URL)

    assert resp.status_code in (301, 302), (
        f'got {resp.status_code}; the stale-ride queue must not be public'
    )


def test_an_operator_sees_the_stranded_ride(operator, client, stranded):
    resp = client.get(STALE_URL)

    assert resp.status_code == 200
    body = resp.content.decode()
    assert f'#{stranded["trip"].id}' in body
    assert 'in_progress' in body


def test_the_queue_shows_the_evidence_an_operator_needs_to_judge(
    operator, client, stranded,
):
    """Enough to distinguish a long ride from a disconnect from an abandonment."""
    body = client.get(STALE_URL).content.decode()

    for needed in ('Silent', 'Ride length', 'Last driver activity',
                   'Last rider activity', 'Assessment'):
        assert needed in body, f'the queue does not show {needed!r}'


def test_the_queue_carries_no_rider_pii(operator, client, stranded):
    """A queue does not need a phone number in it.

    An operator who must call someone opens the ride; the list itself should not
    be a directory.
    """
    rider_phone = stranded['trip'].user_id.phone_number
    body = client.get(STALE_URL).content.decode()

    assert rider_phone not in body
    assert f'R{stranded["trip"].user_id_id}' in body, (
        'the queue should carry a stable non-identifying rider reference'
    )


def test_the_queue_shows_no_coordinates(operator, client, stranded):
    body = client.get(STALE_URL).content.decode()

    assert '17.445' not in body
    assert '78.38' not in body


def test_a_healthy_ride_is_not_listed(operator, client, stranded):
    """The control: the queue must stay meaningful."""
    liveness.record_driver_activity(stranded['trip'].id)

    body = client.get(STALE_URL).content.decode()

    assert 'No stale rides' in body


# ---------------------------------------------------------------------------
# mark_reviewed
# ---------------------------------------------------------------------------

def test_marking_reviewed_clears_the_flag_and_nothing_else(
    operator, client, stranded,
):
    trip = stranded['trip']
    before = (trip.status_id_id, trip.estimated_fare, trip.final_fare,
              trip.completed_at, trip.cancelled_at)

    resp = client.post(STALE_URL, {
        'action': 'mark_reviewed', 'trip_id': trip.id,
        'reason': 'Called the driver, ride is genuinely in traffic',
    })

    assert resp.status_code in (301, 302)
    trip.refresh_from_db()
    assert trip.stale_flagged_at is None
    assert (trip.status_id_id, trip.estimated_fare, trip.final_fare,
            trip.completed_at, trip.cancelled_at) == before


def test_marking_reviewed_is_audited(operator, client, stranded):
    client.post(STALE_URL, {
        'action': 'mark_reviewed', 'trip_id': stranded['trip'].id,
        'reason': 'Spoke to the rider, journey continuing',
    })

    row = AdminAuditLog.objects.filter(
        action='stale_ride_marked_reviewed').first()
    assert row is not None, 'no audit row was written'
    assert row.actor_id == operator.id
    assert row.target_type == 'Trip'
    assert row.target_id == str(stranded['trip'].id)
    assert 'journey continuing' in row.reason


def test_an_action_without_a_reason_is_refused(operator, client, stranded):
    trip = stranded['trip']

    client.post(STALE_URL, {'action': 'mark_reviewed', 'trip_id': trip.id,
                            'reason': '   '})

    trip.refresh_from_db()
    assert trip.stale_flagged_at is not None, (
        'the flag was cleared without a recorded reason'
    )
    assert not AdminAuditLog.objects.filter(
        action='stale_ride_marked_reviewed').exists()


# ---------------------------------------------------------------------------
# release_driver -- a cache repair, never a business decision
# ---------------------------------------------------------------------------

def test_releasing_a_driver_whose_trip_is_finished_clears_the_marker(
    operator, client, stranded,
):
    """The trip-42 recovery, performed by an operator instead of an engineer."""
    trip, driver = stranded['trip'], stranded['driver']
    Trip.objects.filter(id=trip.id).update(
        status_id=_status('completed'), completed_at=timezone.now())
    fake = _FakeRedisState(value=trip.id)

    with mock.patch('servers.redis_client.redis_client', fake):
        client.post(STALE_URL, {
            'action': 'release_driver', 'trip_id': trip.id,
            'reason': 'Ride completed by the driver on a second device',
        })

    assert fake.value is None, (
        'the stale availability marker survived, so the driver would still never '
        'be dispatched to'
    )


def test_releasing_a_driver_mid_ride_changes_nothing(operator, client, stranded):
    """The safety property of the action.

    The ride is still genuinely active, so the database and Redis agree and there
    is nothing to repair. An operator must not be able to free a driver who is
    actually carrying a passenger.
    """
    trip, driver = stranded['trip'], stranded['driver']
    fake = _FakeRedisState(value=trip.id)

    with mock.patch('servers.redis_client.redis_client', fake):
        client.post(STALE_URL, {
            'action': 'release_driver', 'trip_id': trip.id,
            'reason': 'Trying to free the driver',
        })

    assert str(fake.value) == str(trip.id), (
        'an operator released a driver who is still on an active trip'
    )
    trip.refresh_from_db()
    assert trip.status_id.status_code == 'in_progress'


def test_releasing_a_driver_is_audited(operator, client, stranded):
    trip = stranded['trip']
    Trip.objects.filter(id=trip.id).update(status_id=_status('completed'))

    with mock.patch('servers.redis_client.redis_client',
                    _FakeRedisState(value=trip.id)):
        client.post(STALE_URL, {
            'action': 'release_driver', 'trip_id': trip.id,
            'reason': 'Confirmed complete with the driver by phone',
        })

    row = AdminAuditLog.objects.filter(
        action='driver_availability_reconciled').first()
    assert row is not None
    assert row.target_type == 'Driver'
    assert row.after.get('outcome') == 'repaired_cleared'
    assert 'by phone' in row.reason


def test_releasing_a_driver_never_touches_money(operator, client, stranded):
    trip = stranded['trip']
    Trip.objects.filter(id=trip.id).update(status_id=_status('completed'))
    trip.refresh_from_db()
    before = (trip.estimated_fare, trip.final_fare, trip.payment_status)

    with mock.patch('servers.redis_client.redis_client',
                    _FakeRedisState(value=trip.id)):
        client.post(STALE_URL, {
            'action': 'release_driver', 'trip_id': trip.id,
            'reason': 'cache repair',
        })

    trip.refresh_from_db()
    assert (trip.estimated_fare, trip.final_fare, trip.payment_status) == before
    assert trip.final_fare is None


# ---------------------------------------------------------------------------
# The absent actions must stay absent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('unsafe', ['admin_complete', 'admin_cancel',
                                    'force_complete', 'terminate'])
def test_no_lifecycle_terminating_action_is_accepted(
    operator, client, stranded, unsafe,
):
    """Money policy for abandoned rides is undecided, so no such control exists.

    If one is added later it must arrive with the policy, a confirmation, an audit
    entry and its own tests -- not by widening this handler.
    """
    trip = stranded['trip']

    client.post(STALE_URL, {'action': unsafe, 'trip_id': trip.id,
                            'reason': 'attempting to terminate'})

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'in_progress'
    assert trip.completed_at is None
    assert trip.cancelled_at is None


def test_the_page_states_that_termination_is_unavailable(
    operator, client, stranded,
):
    """An operator should learn the boundary from the page, not from a dead end."""
    body = client.get(STALE_URL).content.decode()

    assert 'not available here' in body.lower()


def test_the_page_warns_that_silence_is_not_abandonment(
    operator, client, stranded,
):
    """The judgement the operator has to make is stated on the page.

    Without this the queue reads as a list of broken rides, and the safe default
    becomes "cancel them all".
    """
    body = client.get(STALE_URL).content.decode()

    assert 'not proof' in body.lower() or 'not a verdict' in body.lower()
