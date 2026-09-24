"""C2 — every redeliverable task must tolerate running twice.

WHY THIS IS THE PRICE OF THE PREVIOUS FILE
------------------------------------------
`test_celery_durability.py` asserts `acks_late` + `task_reject_on_worker_lost`, and
`qa/celery_worker_loss_drill.py` proves against a real broker that a killed worker's
task really is redelivered. The whole point of that configuration is that work is not
lost.

The bill for it is at-least-once delivery. A task interrupted anywhere -- including
after its side effects have committed but before the ack -- runs again from the top.
So "safe to lose" has been bought with "must be safe to repeat", and until something
executes each task twice and looks at the resulting rows, that second property is an
assumption.

`base/settings.py` already claims all twelve tasks tolerate re-execution. These tests
check the claim for the six on the ride/money/safety path, by executing the real task
through `.apply()` -- the task machinery, not the inner function -- twice, and
comparing the durable state after the second run with the state after the first.

WHAT "SAFE" MEANS HERE, PRECISELY
--------------------------------
Not "does nothing the second time". Three different outcomes are all acceptable, and
the tests say which one each task has:

  * **converges**  -- the second run sees the work done and stands down
                      (auto_cancel_trip, compute_trip_actuals, issue_receipt_for_trip)
  * **no-ops**     -- the second run is structurally incapable of duplicating,
                      because the database refuses it (persist_location_trail)
  * **repeats deliberately** -- the second run sends the alert again, and that is
                      the correct direction for the hazard (dispatch_sos, and push)

What is never acceptable is a second durable row: a second receipt, a second
cancellation notification, a second SOS event, a second GPS point. Each of those
either double-charges, double-alarms, or inflates a measured distance that feeds a
fare.

EXTERNAL PROVIDERS ARE MOCKED, THE DATABASE IS NOT
--------------------------------------------------
FCM and SES are patched, because the question is what the database looks like after
two runs, not whether Google accepted two pushes. Everything below the provider call
is real.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Notification, Rider

User = get_user_model()

pytestmark = pytest.mark.django_db


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def rider():
    u = User.objects.create_user(phone_number='+919533000001', role='rider',
                                 email='rider@example.test')
    Rider.objects.create(user_id=u)
    return u


@pytest.fixture
def driver():
    u = User.objects.create_user(phone_number='+919633000001', role='driver')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    d = Driver.objects.create(user_id=u, approved=True, status='online')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number='TS09FF4444')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


def _trip(rider, status, **kw):
    now = timezone.now()
    fields = dict(
        user_id=rider, status_id=_status(status),
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.4550000'), destination_long=Decimal('78.3900000'),
        estimated_fare=Decimal('169.02'), payment_method='cash',
    )
    if status in ('accepted', 'reached', 'in_progress', 'completed'):
        fields['accepted_at'] = now
    if status in ('in_progress', 'completed'):
        fields['started_at'] = now
    if status == 'completed':
        fields['completed_at'] = now
    fields.update(kw)
    return Trip.objects.create(**fields)


# ===========================================================================
# 1. auto_cancel_trip — converges
# ===========================================================================

def test_auto_cancel_twice_cancels_once_and_notifies_once(rider):
    """The second delivery must not send the rider a second cancellation.

    A redelivery here is the most likely of the six: the task is scheduled with a
    90-second countdown, so the message sits unacked in worker memory for the whole
    search. A worker restart anywhere in that window redelivers it.
    """
    from servers.ride.tasks import auto_cancel_trip
    trip = _trip(rider, 'requested')

    first = auto_cancel_trip.apply(args=(trip.id,)).get()
    trip.refresh_from_db()
    cancelled_at = trip.cancelled_at
    notifications_after_one = Notification.objects.filter(user_id=rider).count()

    second = auto_cancel_trip.apply(args=(trip.id,)).get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'cancelled'
    assert trip.cancelled_at == cancelled_at, (
        'the second run rewrote cancelled_at, so the audit trail now says the trip '
        'was cancelled later than it was'
    )
    assert Notification.objects.filter(user_id=rider).count() == \
        notifications_after_one == 1, (
        'the rider was told twice that their ride was cancelled'
    )
    assert 'already cancelled' in second.lower(), (
        f'the second run did not report standing down: {second!r} (first: {first!r})'
    )


def test_auto_cancel_redelivered_after_an_acceptance_stands_down(rider, driver):
    """The dangerous ordering, and the reason the row is locked and re-read.

    The first delivery cancels nothing because a driver accepted. A redelivery
    arriving later must reach the same conclusion rather than cancelling a ride
    that is already being driven.
    """
    from servers.ride.tasks import auto_cancel_trip
    trip = _trip(rider, 'accepted', driver_id=driver)

    auto_cancel_trip.apply(args=(trip.id,)).get()
    auto_cancel_trip.apply(args=(trip.id,)).get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'accepted'
    assert trip.driver_id_id == driver.id, (
        'driver_id was cleared — this is the trip-42-shaped defect: a broad save() '
        'writing a stale in-memory instance back over an acceptance'
    )
    assert trip.cancelled_at is None
    assert Notification.objects.filter(user_id=rider).count() == 0


def test_auto_cancel_on_a_completed_trip_never_cancels_it(rider, driver):
    """A redelivery hours later must not cancel a finished, paid ride."""
    from servers.ride.tasks import auto_cancel_trip
    trip = _trip(rider, 'completed', driver_id=driver)

    auto_cancel_trip.apply(args=(trip.id,)).get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'completed'
    assert trip.cancelled_at is None


# ===========================================================================
# 2. compute_trip_actuals — converges, and never touches money
# ===========================================================================

def test_compute_actuals_twice_leaves_one_answer(rider, driver):
    """Deterministic for a given trail, and skipped once populated."""
    from servers.ride.tasks import compute_trip_actuals
    trip = _trip(rider, 'completed', driver_id=driver)

    compute_trip_actuals.apply(args=(trip.id,)).get()
    trip.refresh_from_db()
    after_one = (trip.actual_distance_km, trip.actual_duration_min)

    compute_trip_actuals.apply(args=(trip.id,)).get()

    trip.refresh_from_db()
    assert (trip.actual_distance_km, trip.actual_duration_min) == after_one, (
        'the second run produced different actuals; a redelivery would change a '
        'number that operations and any future reconciliation read'
    )


def test_compute_actuals_never_writes_final_fare_however_many_times_it_runs(
    rider, driver,
):
    """The financial boundary. `final_fare` stays NULL and non-authoritative.

    This task is observe-only by design. Repeating it must not be the thing that
    turns an observation into a charge.
    """
    from servers.ride.tasks import compute_trip_actuals
    trip = _trip(rider, 'completed', driver_id=driver)
    estimated = trip.estimated_fare

    for _ in range(3):
        compute_trip_actuals.apply(args=(trip.id,)).get()

    trip.refresh_from_db()
    assert trip.final_fare is None, (
        'final_fare was written by a metrics task — the rider is now charged a '
        'number no one quoted them'
    )
    assert trip.estimated_fare == estimated


# ===========================================================================
# 3. issue_receipt_for_trip — converges, and the database backs it up
# ===========================================================================

@pytest.fixture
def no_receipt_delivery():
    """Patch only the delivery, so rendering and persistence stay real."""
    with mock.patch('servers.ride.receipts._send_receipt_email',
                    return_value=(True, '')) as m:
        yield m


def test_issuing_a_receipt_twice_produces_one_receipt(
    rider, driver, no_receipt_delivery,
):
    from servers.ride.models import Receipt
    from servers.ride.tasks import issue_receipt_for_trip
    trip = _trip(rider, 'completed', driver_id=driver)

    issue_receipt_for_trip.apply(args=(trip.id,)).get()
    receipts = list(Receipt.objects.filter(trip_id=trip))
    assert len(receipts) == 1, 'setup: the first run should issue exactly one'
    number, version = receipts[0].receipt_number, receipts[0].version

    issue_receipt_for_trip.apply(args=(trip.id,)).get()

    receipts = list(Receipt.objects.filter(trip_id=trip))
    assert len(receipts) == 1, (
        f'a redelivery issued a second receipt ({len(receipts)} on file); the rider '
        'now has two documents for one journey'
    )
    assert receipts[0].receipt_number == number
    assert receipts[0].version == version, (
        'the redelivery bumped the version, which is reserved for a genuine '
        'reissue after a fare adjustment'
    )


def test_the_receipt_number_is_unique_in_the_database_not_only_in_python(
    rider, driver, no_receipt_delivery,
):
    """The guard in `issue_receipt` is a read-then-create, which two workers can
    interleave. What makes concurrent duplication impossible is the unique index on
    `receipt_number`, and the number is deterministic per trip -- so assert the
    constraint is really there rather than trusting the check.
    """
    from servers.ride.models import Receipt
    from servers.ride.tasks import issue_receipt_for_trip
    trip = _trip(rider, 'completed', driver_id=driver)
    issue_receipt_for_trip.apply(args=(trip.id,)).get()
    existing = Receipt.objects.get(trip_id=trip)

    field = Receipt._meta.get_field('receipt_number')
    assert field.unique, (
        'receipt_number is not unique in the database, so two workers running this '
        'task at the same moment would each pass the existence check and insert'
    )
    assert existing.receipt_number.endswith(f'-{trip.id}-v1'), (
        'the number is not derived from the trip, so a concurrent duplicate would '
        'not collide and the unique index would not save us'
    )


# ===========================================================================
# 4. persist_location_trail — cannot duplicate, because the schema refuses
# ===========================================================================

def test_the_same_stream_event_cannot_become_two_points(rider, driver):
    """Duplicated GPS points inflate actual distance, which feeds a fare.

    This is the one task where converging is not enough: a batch interrupted
    half-way leaves some events acked and some not, so a redelivery legitimately
    re-processes entries that were already stored. The unique constraint on
    (trip, source_event_id) is what makes that a no-op.
    """
    from django.db import IntegrityError, transaction

    from servers.ride.models import TripLocationPoint
    trip = _trip(rider, 'in_progress', driver_id=driver)
    event_id = '1758623456789-0'
    common = dict(
        trip=trip, driver=driver, latitude=Decimal('17.4450000'),
        longitude=Decimal('78.3800000'), recorded_at=timezone.now(),
        source_event_id=event_id,
    )

    TripLocationPoint.objects.create(**common)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TripLocationPoint.objects.create(**common)

    assert TripLocationPoint.objects.filter(trip=trip).count() == 1


def test_draining_an_empty_stream_twice_is_harmless(rider):
    """The default path. GPS_TRAIL_ENABLED is False, so the task is a no-op --
    asserted, because a no-op that quietly became a write would be invisible."""
    from servers.ride.models import TripLocationPoint
    from servers.ride.tasks import persist_location_trail

    first = persist_location_trail.apply().get()
    second = persist_location_trail.apply().get()

    assert first == second
    assert TripLocationPoint.objects.count() == 0


# ===========================================================================
# 5 & 6. Alerts — repeat deliberately, but create no second durable record
# ===========================================================================

def test_a_redelivered_sos_dispatch_creates_no_second_sos_event(rider, driver):
    """Re-alerting is the right direction. A second row is not.

    A duplicated SOSEvent would show operations two emergencies where there is
    one, and the dedup the API already does (a second press returns `repeat_of`)
    would be undone from behind by the worker.
    """
    from servers.sos.models import SOSEvent
    trip = _trip(rider, 'in_progress', driver_id=driver)
    event = SOSEvent.objects.create(
        user=rider, trip=trip, event_type='panic',
        latitude=Decimal('17.4450000'), longitude=Decimal('78.3800000'),
    )

    # An ops user with a token, so the fan-out has somewhere to go and the
    # re-notification below is observable rather than vacuously zero.
    User.objects.create_user(
        phone_number='+919733000001', role='admin', is_staff=True,
        fcm_token='ops-device-token',
    )

    from servers.sos.tasks import dispatch_sos
    with mock.patch('servers.auth_user.services.send_push_notification',
                    return_value=True) as push:
        dispatch_sos.apply(args=(event.id,)).get()
        after_first = push.call_count
        dispatch_sos.apply(args=(event.id,)).get()
        after_second = push.call_count

    assert SOSEvent.objects.count() == 1, (
        'the dispatcher created another SOS event; operations would see two '
        'emergencies for one press'
    )
    assert after_first >= 1, (
        'setup: the first dispatch notified nobody, so this test would prove '
        'nothing about the second'
    )
    assert after_second > after_first, (
        'a redelivered SOS dispatch did not re-alert. That is the wrong direction '
        'for this hazard: if the first alert was the one that was lost with the '
        'worker, silence is the outcome nobody wants.'
    )


def test_a_redelivered_push_does_not_corrupt_the_users_token(rider):
    """The failure mode worth guarding: a repeat that clears a working token.

    `send_push_notification_task` clears the stored FCM token when the provider
    says it is permanently invalid. A redelivery of a *successful* push must not
    take that branch, or the second delivery of a routine notification would
    silently unregister the device.
    """
    from servers.auth_user.services import send_push_notification_task
    User.objects.filter(pk=rider.pk).update(fcm_token='a-token-value')

    with mock.patch('servers.auth_user.services.firebase_admin') as fb, \
            mock.patch('servers.auth_user.services.messaging') as msg:
        fb._apps = {'default': object()}
        msg.send.return_value = 'projects/x/messages/1'
        first = send_push_notification_task.apply(
            args=(rider.pk, 'Title', 'Body', {'k': 'v'})).get()
        second = send_push_notification_task.apply(
            args=(rider.pk, 'Title', 'Body', {'k': 'v'})).get()

    rider.refresh_from_db()
    assert first is True and second is True
    assert rider.fcm_token == 'a-token-value', (
        'a repeated successful push cleared the token; the device is now '
        'unregistered and will receive nothing'
    )


def test_a_push_task_never_writes_the_token_into_the_logs(rider, caplog):
    """B5's privacy rule, checked where the token is actually handled.

    A push token is a durable device credential. Anyone holding one can send
    notifications to that device, so it must not reach log storage.
    """
    import logging

    from servers.auth_user.services import send_push_notification_task
    token = 'fcm-secret-token-value-do-not-log'
    User.objects.filter(pk=rider.pk).update(fcm_token=token)

    with caplog.at_level(logging.DEBUG), \
            mock.patch('servers.auth_user.services.firebase_admin') as fb, \
            mock.patch('servers.auth_user.services.messaging') as msg:
        fb._apps = {'default': object()}
        msg.send.return_value = 'ok'
        send_push_notification_task.apply(
            args=(rider.pk, 'Title', 'Body', None)).get()

    assert token not in caplog.text, (
        'the FCM token was written to the log; log storage now holds a credential '
        'that can push to a real device'
    )
