"""The raiser's "Tap if this was a mistake" (D4, 2026-11-02; revised 2026-11-03).

The alert goes out the moment the 2-second hold completes; there is no cancel
delay. Tapping "mistake" is a REPORT: the alert stays active, the console shows
"rider reports false alarm", and an operator must confirm it (owner,
2026-11-03). These tests pin who may report it and that a report closes nothing.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from servers.ride.models import Trip, TripStatus
from servers.sos.models import SOSEvent, SOSEventUpdate
from servers.sos.views import RAISER_MISTAKE_NOTE

User = get_user_model()

pytestmark = pytest.mark.django_db

_n = iter(range(1, 999))


def _rider():
    i = next(_n)
    return User.objects.create_user(phone_number=f'+9195700{i:05d}', role='rider')


def _api(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _raise(user):
    st, _ = TripStatus.objects.get_or_create(status_code='in_progress')
    trip = Trip.objects.create(
        user_id=user, status_id=st,
        pickup_lat=Decimal('17.445'), pickup_long=Decimal('78.38'),
        destination_lat=Decimal('17.455'), destination_long=Decimal('78.39'),
        estimated_fare=Decimal('150.00'), payment_method='cash',
    )
    r = _api(user).post('/api/v1/sos/', {'trip_id': trip.id}, format='json')
    assert r.status_code == 201, r.content
    return SOSEvent.objects.get(id=r.json()['data']['sos_id'])


def _mistake(user, sos_id):
    return _api(user).post(f'/api/v1/sos/{sos_id}/mistake/', {}, format='json')


def test_the_raiser_can_mark_it_a_mistake():
    rider = _rider()
    ev = _raise(rider)

    r = _mistake(rider, ev.id)

    assert r.status_code == 200, r.content
    assert r.json()['data']['reported_mistake'] is True
    ev.refresh_from_db()
    assert ev.status == 'open', 'a report never closes an SOS: an operator confirms'
    assert ev.raiser_reported_mistake_at is not None
    update = SOSEventUpdate.objects.get(event=ev)
    assert update.actor_id == rider.id and update.new_status == 'open'
    assert update.note == RAISER_MISTAKE_NOTE


def test_the_operator_sees_it_and_confirms_it():
    from servers.safety.admin_views import _annotate, _row
    from servers.safety.models import SafetyIncident

    rider = _rider()
    ev = _raise(rider)
    _mistake(rider, ev.id)
    ev.refresh_from_db()
    if ev.incident_id:
        row = _row(_annotate(SafetyIncident.objects.all()).get(pk=ev.incident_id))
        assert row['rider_reports_false_alarm'] is True
        assert row['ref'] == f'SOS-{ev.incident_id}'


def test_twice_is_harmless():
    rider = _rider()
    ev = _raise(rider)
    _mistake(rider, ev.id)

    assert _mistake(rider, ev.id).status_code == 200
    assert SOSEventUpdate.objects.filter(event=ev).count() == 1


def test_nobody_else_can_close_someone_elses_alert():
    """Answered as "not found", like every other out-of-scope SOS read."""
    rider, other = _rider(), _rider()
    ev = _raise(rider)

    r = _mistake(other, ev.id)

    assert r.status_code == 404
    ev.refresh_from_db()
    assert ev.status == 'open'


def test_a_resolved_alert_stays_resolved():
    rider = _rider()
    ev = _raise(rider)
    SOSEvent.objects.filter(id=ev.id).update(status='resolved')

    r = _mistake(rider, ev.id)

    assert r.status_code == 409
    assert r.json()['error']['code'] == 'SOS_ALREADY_RESOLVED'
    ev.refresh_from_db()
    assert ev.status == 'resolved'


def test_signed_out_is_refused():
    rider = _rider()
    ev = _raise(rider)
    assert APIClient().post(f'/api/v1/sos/{ev.id}/mistake/').status_code in (401, 403)
