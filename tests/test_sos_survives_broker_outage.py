"""An SOS must survive the alerting path failing.

The SOS write itself is well built: the row is committed inside a transaction, the
fan-out is deferred with `transaction.on_commit` so a rollback cannot page anyone
about a phantom emergency, and deduplication is decided in PostgreSQL rather than
Redis specifically so that a cache miss can never record a safety event twice and
a cache hit can never drop one.

What is NOT covered by that design is the enqueue itself:

    def _dispatch():
        try:
            dispatch_sos.delay(event.id)
        except Exception as e:
            logger.exception(...)

Celery's broker is Redis. If Redis is unavailable, `.delay()` raises, the handler
logs it, and the request still returns 201 with "SOS recorded. Help is on the
way." The event is durable and **nobody is notified**.

That is a defensible design -- refusing the SOS because the alerting queue is down
would be worse -- but it means the operations console is the only thing standing
between a recorded SOS and an unnoticed one. These tests pin both halves of that:
the event must survive, and it must still be visible to the operator endpoint that
reads PostgreSQL directly.

They are written as integration proofs rather than unit assertions because the
property that matters spans the view, the transaction, the broker and the operator
query.
"""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.driver.models import Driver
from servers.rider.models import Rider
from servers.sos.models import SOSEvent

User = get_user_model()


def _client_for(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


@pytest.fixture
def rider(db):
    u = User.objects.create_user(phone_number='+919777000001', role='rider')
    Rider.objects.create(user_id=u)
    return u


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        phone_number='+919777000009', role='admin',
        is_staff=True, is_superuser=True,
    )


def _broker_down():
    """Make enqueueing raise the way an unreachable Redis broker does."""
    return mock.patch(
        'servers.sos.tasks.dispatch_sos.delay',
        side_effect=ConnectionError('Error 111 connecting to redis:6379'),
    )


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_an_sos_is_recorded_even_when_the_broker_is_unreachable(rider):
    """The event must not be lost because the alerting queue is down."""
    with _broker_down():
        resp = _client_for(rider).post('/api/v1/sos/', {'event_type': 'panic'},
                                       format='json')

    assert resp.status_code == 201, resp.content
    event = SOSEvent.objects.get(user=rider)
    assert event.status == 'open'
    assert event.event_type == 'panic'


@pytest.mark.django_db(transaction=True)
def test_the_operator_can_still_see_it(rider, admin):
    """The console reads PostgreSQL, so it is the fallback when paging fails.

    This is the assertion that makes the design defensible. If the operator
    listing depended on the same Redis that just failed, a broker outage would
    make an SOS invisible rather than merely unannounced.
    """
    with _broker_down():
        _client_for(rider).post('/api/v1/sos/', {'event_type': 'medical'},
                                format='json')

    resp = _client_for(admin).get('/api/v1/sos/admin/')

    assert resp.status_code == 200, resp.content
    body = resp.json()
    payload = body.get('data', body)
    rows = payload.get('results', payload) if isinstance(payload, dict) else payload
    assert rows, 'the operator SOS list is empty while an open SOS exists'
    assert any(r.get('event_type') == 'medical' for r in rows), (
        'an SOS raised during a broker outage is not visible to the operator, '
        'so nothing would ever surface it'
    )


@pytest.mark.django_db(transaction=True)
def test_the_rider_is_not_told_help_is_coming_by_a_failed_write(rider):
    """Negative control.

    If the DB write itself fails the caller must NOT get a success. Otherwise the
    reassuring message would be the only artefact of the emergency.
    """
    with mock.patch(
        'servers.sos.models.SOSEvent.objects.create',
        side_effect=RuntimeError('database is down'),
    ):
        resp = _client_for(rider).post('/api/v1/sos/', {'event_type': 'panic'},
                                       format='json')

    assert resp.status_code == 500, (
        f'got {resp.status_code}; a failed SOS write must not report success'
    )
    assert not SOSEvent.objects.exists()


# ---------------------------------------------------------------------------
# Deduplication must not depend on the broker either
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_repeated_presses_during_an_outage_still_collapse(rider):
    """A panicking user presses repeatedly. That must not become N events.

    Dedup lives in PostgreSQL, so it must keep working while Redis is down --
    which is exactly when a frightened user is most likely to be retrying.
    """
    c = _client_for(rider)
    with _broker_down():
        first = c.post('/api/v1/sos/', {'event_type': 'panic'}, format='json')
        second = c.post('/api/v1/sos/', {'event_type': 'panic'}, format='json')
        third = c.post('/api/v1/sos/', {'event_type': 'panic'}, format='json')

    assert first.status_code == 201
    assert second.status_code == 200
    assert third.status_code == 200
    assert SOSEvent.objects.count() == 1, (
        f'{SOSEvent.objects.count()} events recorded for one emergency; '
        'operators would be paged repeatedly and learn to ignore the alert'
    )


@pytest.mark.django_db(transaction=True)
def test_a_different_emergency_type_is_never_collapsed(rider):
    """Escalation must survive dedup.

    Someone who pressed panic and then reports a medical emergency is telling
    operators something new.
    """
    c = _client_for(rider)
    with _broker_down():
        c.post('/api/v1/sos/', {'event_type': 'panic'}, format='json')
        escalation = c.post('/api/v1/sos/', {'event_type': 'medical'},
                            format='json')

    assert escalation.status_code == 201
    assert SOSEvent.objects.count() == 2, (
        'a different emergency type was collapsed into the previous event'
    )


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_no_coordinates_appear_in_the_sos_log_lines(rider, caplog):
    """SOS is where location is most sensitive, and logs are retained and shipped.

    The view records `has_location` rather than the position; the event id is
    enough for anyone entitled to the coordinates to read them from the database.
    """
    import logging
    caplog.set_level(logging.INFO)

    lat, lng = '17.4450000', '78.3800000'
    with _broker_down():
        resp = _client_for(rider).post(
            '/api/v1/sos/',
            {'event_type': 'panic', 'lat': lat, 'lng': lng},
            format='json',
        )
    assert resp.status_code == 201

    # The coordinates must have been stored...
    event = SOSEvent.objects.get(user=rider)
    assert event.latitude is not None and event.longitude is not None

    # ...and must not appear anywhere in the emitted log records.
    emitted = '\n'.join(
        [r.getMessage() for r in caplog.records]
        + [str(getattr(r, k)) for r in caplog.records
           for k in vars(r) if k not in ('args', 'msg')]
    )
    for fragment in ('17.445', '78.38'):
        assert fragment not in emitted, (
            f'{fragment!r} leaked into an SOS log line; coordinates must stay in '
            'the database, not in retained and searchable logs'
        )
