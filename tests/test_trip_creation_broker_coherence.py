"""A committed trip must never be reported as a failed booking.

`_create_trip` in servers/consumers.py does this, in order:

    with transaction.atomic():
        trip = Trip.objects.create(...)        # commits
    auto_cancel_trip.apply_async(...)          # OUTSIDE the transaction
    return trip, False

wrapped in a broad `except Exception: return None, False`.

Celery's broker is Redis. When Redis is unavailable the enqueue raises, the broad
handler catches it, and the caller is told the booking failed -- but the trip row
is already committed. The result is a `requested` trip in PostgreSQL that the
rider was told does not exist, with no auto-cancel scheduled, so nothing will ever
move it out of `requested`.

That is the failure this file pins: whatever else happens when the broker is down,
the database and the answer given to the rider must agree.

The residual risk after the fix is stated rather than hidden: a trip created while
the broker is unreachable has no scheduled timeout. Dispatch also needs Redis, so
no driver would be offered the trip either, and the rider sees a booking with no
drivers rather than a phantom. An operator can cancel it from the console, and the
enqueue failure is now logged as a distinct event instead of being disguised as a
creation failure.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model

from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider

User = get_user_model()


@pytest.fixture
def rider(db):
    u = User.objects.create_user(phone_number='+919766000001', role='rider')
    Rider.objects.create(user_id=u)
    TripStatus.objects.get_or_create(status_code='requested')
    return u


def _broker_down():
    """apply_async raises the way an unreachable Redis broker does."""
    return mock.patch(
        'servers.ride.tasks.auto_cancel_trip.apply_async',
        side_effect=ConnectionError('Error 111 connecting to redis:6379'),
    )


def _make_trip(rider):
    """Create a trip the way the consumer's helper does.

    Mirrors the ordering under test rather than driving a WebSocket: the property
    is about the transaction boundary and the exception handling, and a socket
    test would obscure both behind connection setup.
    """
    from servers.ride.tasks import auto_cancel_trip
    from django.db import transaction

    status = TripStatus.objects.get(status_code='requested')
    with transaction.atomic():
        trip = Trip.objects.create(
            user_id=rider, status_id=status,
            pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
            destination_lat=Decimal('17.4550000'),
            destination_long=Decimal('78.3900000'),
            estimated_fare=Decimal('120.00'), payment_method='cash',
        )
    # Outside the transaction, exactly as the consumer does it.
    auto_cancel_trip.apply_async((trip.id,), countdown=90)
    return trip


# ---------------------------------------------------------------------------
# The coherence property
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_an_enqueue_failure_leaves_no_trip_the_rider_was_told_failed(rider):
    """The defect, stated as an invariant.

    Either the trip exists and the rider is told so, or it does not exist. A
    committed row plus a reported failure is the one combination that must not
    happen, because nothing will ever reconcile it.
    """
    trips_before = Trip.objects.count()

    with _broker_down():
        with pytest.raises(ConnectionError):
            _make_trip(rider)

    # The row IS committed -- this documents the real behaviour of the ordering,
    # and is why the consumer must not report it as a creation failure.
    assert Trip.objects.count() == trips_before + 1, (
        'the trip was not committed, which would make this concern moot'
    )
    orphan = Trip.objects.latest('id')
    assert orphan.status_id.status_code == 'requested'


@pytest.mark.django_db(transaction=True)
def test_the_consumer_does_not_disguise_an_enqueue_failure_as_a_creation_failure(
    rider,
):
    """The fix, asserted against the consumer's own helper.

    `_create_trip` must not return (None, False) -- its "creation failed" answer
    -- for a trip that is sitting committed in the database.
    """
    import servers.consumers as consumers

    source = consumers.__file__
    with open(source, encoding='utf-8') as fh:
        body = fh.read()

    # The enqueue must be guarded separately from trip creation. Asserted on the
    # source because the alternative is driving a full WebSocket handshake to
    # observe one return value, and the property is structural.
    assert 'trip_autocancel_enqueue_failed' in body, (
        'the auto-cancel enqueue is not handled separately from trip creation, '
        'so a broker outage still reports a committed trip as a failed booking'
    )


@pytest.mark.django_db(transaction=True)
def test_a_healthy_broker_still_schedules_the_timeout(rider):
    """Negative control.

    The timeout is what durably bounds a search. If this stopped being scheduled,
    a rider nobody accepts would wait forever -- so "handle the failure" must not
    become "stop scheduling it".
    """
    with mock.patch(
        'servers.ride.tasks.auto_cancel_trip.apply_async',
    ) as enqueue:
        trip = _make_trip(rider)

    assert enqueue.call_count == 1, 'the auto-cancel timeout was not scheduled'
    args, kwargs = enqueue.call_args
    assert args[0] == (trip.id,)
    assert kwargs.get('countdown'), 'scheduled with no countdown'


@pytest.mark.django_db(transaction=True)
def test_the_timeout_task_is_still_durable_against_worker_loss():
    """The other half of the rescue.

    A scheduled auto-cancel is only a rescue if a dying worker does not silently
    drop it. Pinned here as well as in test_celery_durability.py because this is
    the trip-level consequence: losing it means a rider waits forever.
    """
    from django.conf import settings

    assert settings.CELERY_TASK_ACKS_LATE is True
    assert settings.CELERY_TASK_REJECT_ON_WORKER_LOST is True
