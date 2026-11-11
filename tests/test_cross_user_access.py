"""A5 — one authenticated user must not be able to read another's records.

WHY A SEPARATE FILE
-------------------
`test_admin_dashboard_authz.py` proves the console refuses anonymous callers, and
`test_operator_privilege_boundary.py` proves the operator API refuses non-operators.
Neither asks the question this file asks, which is the one an ordinary logged-in user
is in a position to ask: **what happens if I change the id in the URL?**

That question has a different failure mode from a missing permission class. Every
endpoint below already requires authentication, and every one of them passes the
authentication check for the attacker — because the attacker is a real, valid rider
with a real token. The only thing standing between them and someone else's trip,
receipt or support ticket is whether the view scopes its lookup to the caller.

WHAT A SCOPING BUG LOOKS LIKE
-----------------------------
It is nearly invisible in review, because the difference is one keyword argument:

    Trip.objects.get(id=trip_id)                     # anyone's trip
    Trip.objects.get(id=trip_id, user_id=request.user)  # only mine

Both read correctly in a diff. One of them hands a stranger a rider's pickup
address, destination, fare and driver's name.

403 AND 404 ARE BOTH ACCEPTABLE
-------------------------------
A 404 for someone else's record leaks slightly less (it does not confirm the record
exists), and a 403 is clearer for a legitimate user who mistyped. Either is a pass
here. What is never a pass is a 200, and what is also never a pass is a 500 --
authorization that works by crashing is authorization nobody can reason about.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider
from servers.support.models import SupportTicket

User = get_user_model()

pytestmark = pytest.mark.django_db

REFUSED = (401, 403, 404)


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


def _rider(n):
    u = User.objects.create_user(phone_number=f'+91952{n:07d}', role='rider',
                                 username=f'+91952{n:07d}',
                                 email=f'rider{n}@example.test')
    Rider.objects.create(user_id=u)
    return u


@pytest.fixture
def victim():
    return _rider(1000001)


@pytest.fixture
def attacker():
    """A second, entirely legitimate rider. Nothing about this account is special.

    That is the point: the attacker does not need a stolen token or an elevated
    role. They need an account and the ability to type a different number.
    """
    return _rider(2000002)


@pytest.fixture
def other_driver():
    """A driver with no connection to the victim's trip."""
    u = User.objects.create_user(phone_number='+919652000001', role='driver',
                                 username='+919652000001')
    d = Driver.objects.create(user_id=u, approved=True, status='online')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number='TS09XU9999')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return u


@pytest.fixture
def assigned_driver():
    u = User.objects.create_user(phone_number='+919653000001', role='driver',
                                 username='+919653000001')
    d = Driver.objects.create(user_id=u, approved=True, status='online')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number='TS09XA1111')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return u


@pytest.fixture
def victims_trip(victim, assigned_driver):
    now = timezone.now()
    return Trip.objects.create(
        user_id=victim, driver_id=Driver.objects.get(user_id=assigned_driver),
        status_id=_status('completed'),
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.4550000'), destination_long=Decimal('78.3900000'),
        estimated_fare=Decimal('169.02'), payment_method='cash',
        accepted_at=now, started_at=now, completed_at=now,
    )


@pytest.fixture
def victims_ticket(victim, victims_trip):
    return SupportTicket.objects.create(
        user_id=victim, issue_type='payment', trip_id=victims_trip,
        description='I was charged twice for the same ride',
    )


# ===========================================================================
# Trips — the record with a home address in it
# ===========================================================================

def test_a_rider_cannot_read_another_riders_trip(attacker, victims_trip):
    """A trip carries pickup and destination coordinates, the fare, and the
    driver's identity. It is the most sensitive per-ride record there is."""
    resp = _client(attacker).get(f'/api/v1/ride/trip/{victims_trip.id}/')

    assert resp.status_code in REFUSED, (
        f'a rider read another rider\'s trip: HTTP {resp.status_code}. '
        f'Body starts: {resp.content[:200]}'
    )


def test_an_unrelated_driver_cannot_read_a_trip_they_were_not_assigned(
    other_driver, victims_trip,
):
    """Assignment, not role, is what authorises a driver to see a trip.

    Every driver on the platform holds a valid driver token. If role alone were
    enough, any of them could read every ride anyone had ever taken.
    """
    resp = _client(other_driver).get(f'/api/v1/ride/trip/{victims_trip.id}/')

    assert resp.status_code in REFUSED, (
        f'an unassigned driver read the trip: HTTP {resp.status_code}'
    )


def test_the_assigned_driver_can_read_the_trip(assigned_driver, victims_trip):
    """The control. Scoping that refuses the assigned driver is an outage."""
    resp = _client(assigned_driver).get(f'/api/v1/ride/trip/{victims_trip.id}/')

    assert resp.status_code == 200, (
        f'the assigned driver was refused their own trip: HTTP '
        f'{resp.status_code} {resp.content[:200]}'
    )


def test_the_rider_can_read_their_own_trip(victim, victims_trip):
    """The other control."""
    resp = _client(victim).get(f'/api/v1/ride/trip/{victims_trip.id}/')

    assert resp.status_code == 200, resp.content[:200]


def test_another_riders_trip_does_not_appear_in_my_history(attacker, victims_trip):
    """The list endpoint has the same scoping obligation as the detail endpoint,
    and it is easier to get wrong because nothing in the URL names a user."""
    resp = _client(attacker).get('/api/v1/ride/ride-history/')

    assert resp.status_code == 200
    body = resp.json()
    ids = _trip_ids(body)
    assert victims_trip.id not in ids, (
        f'trip {victims_trip.id} belongs to another rider and appeared in this '
        f'rider\'s history'
    )


def _trip_ids(body):
    data = body.get('data', body)
    rows = data.get('results', data) if isinstance(data, dict) else data
    out = []
    for row in rows if isinstance(rows, list) else []:
        for key in ('id', 'trip_id'):
            if key in row:
                out.append(row[key])
    return out


# ===========================================================================
# Receipts — a document with a name, an email and an amount on it
# ===========================================================================

def test_a_rider_cannot_fetch_another_riders_receipt_pdf(attacker, victims_trip):
    resp = _client(attacker).get(
        f'/api/v1/ride/trip/{victims_trip.id}/receipt/pdf/')

    assert resp.status_code in REFUSED, (
        f'a rider fetched another rider\'s receipt: HTTP {resp.status_code}'
    )


def test_a_rider_cannot_have_another_riders_receipt_emailed(attacker, victims_trip):
    """Worse than reading it: this would send someone else's receipt somewhere.

    A resend that authorises on the trip id alone is a way to make the platform
    deliver a stranger's journey details to an address of the attacker's choosing,
    or simply to spam the victim.
    """
    resp = _client(attacker).post(
        f'/api/v1/ride/trip/{victims_trip.id}/receipt/resend/', {}, format='json')

    assert resp.status_code in REFUSED, (
        f'a rider triggered a resend of another rider\'s receipt: HTTP '
        f'{resp.status_code}'
    )


# ===========================================================================
# Support tickets — free text, where people put everything
# ===========================================================================

def test_a_rider_cannot_read_another_riders_support_ticket(attacker, victims_ticket):
    """Ticket bodies are unstructured, so they contain whatever the user typed --
    addresses, complaints about a named driver, card trouble, personal context."""
    resp = _client(attacker).get(f'/api/v1/support/tickets/{victims_ticket.id}/')

    assert resp.status_code in REFUSED, (
        f'a rider read another rider\'s support ticket: HTTP {resp.status_code}'
    )


def test_a_rider_cannot_post_into_another_riders_ticket(attacker, victims_ticket):
    """Writing into someone else's conversation with support, as them."""
    resp = _client(attacker).post(
        f'/api/v1/support/tickets/{victims_ticket.id}/messages/',
        {'body': 'please refund to my account instead'}, format='json')

    assert resp.status_code in REFUSED, (
        f'a rider added a message to another rider\'s ticket: HTTP '
        f'{resp.status_code}'
    )
    assert not victims_ticket.messages.filter(
        body__icontains='my account instead').exists(), (
        'the message was written into the victim\'s ticket'
    )


def test_a_rider_cannot_close_another_riders_ticket(attacker, victims_ticket):
    """Closing someone's complaint is a way to make a dispute disappear."""
    resp = _client(attacker).post(
        f'/api/v1/support/tickets/{victims_ticket.id}/close/', {}, format='json')

    assert resp.status_code in REFUSED, (
        f'a rider closed another rider\'s ticket: HTTP {resp.status_code}'
    )
    victims_ticket.refresh_from_db()
    assert victims_ticket.status != 'closed', 'the victim\'s ticket was closed'


def test_the_owner_can_read_their_own_ticket(victim, victims_ticket):
    """The control."""
    resp = _client(victim).get(f'/api/v1/support/tickets/{victims_ticket.id}/')

    assert resp.status_code == 200, resp.content[:200]


def test_another_riders_ticket_does_not_appear_in_my_list(attacker, victims_ticket):
    """Parsed, not substring-matched.

    The first version of this asserted `str(ticket.id) not in response_text or
    ticket.subject not in response_text`. Both halves were weak -- a bare digit
    appears in any JSON document -- and the `or` meant the first weak half
    short-circuited the second away. It passed for the wrong reason and then raised
    an AttributeError when run in a different order, which is how it was noticed.
    """
    resp = _client(attacker).get('/api/v1/support/tickets/')

    assert resp.status_code == 200, resp.content[:200]
    returned = _ticket_ids(resp.json())
    assert victims_ticket.id not in returned, (
        f'ticket {victims_ticket.id} belongs to another rider and appeared in '
        f'this rider list (returned ids: {returned})'
    )
    assert victims_ticket.description not in resp.content.decode(), (
        'another rider ticket body appeared in the response'
    )


def _ticket_ids(body):
    data = body.get('data', body)
    rows = data.get('results', data) if isinstance(data, dict) else data
    return [r['id'] for r in rows if isinstance(r, dict) and 'id' in r]         if isinstance(rows, list) else []


def test_a_rider_cannot_open_a_ticket_against_someone_elses_trip(
    attacker, victims_trip,
):
    """Creating a ticket attached to a trip you were not on would pull that trip
    into a conversation the attacker can then read."""
    resp = _client(attacker).post(
        '/api/v1/support/tickets/create/',
        {'issue_type': 'payment', 'trip_id': victims_trip.id,
         'description': 'what happened here'},
        format='json')

    if resp.status_code == 200 or resp.status_code == 201:
        # Creating a ticket is fine; attaching someone else's trip is not.
        ticket = SupportTicket.objects.filter(user_id=attacker).first()
        assert ticket is not None
        assert ticket.trip_id_id != victims_trip.id, (
            'the ticket was attached to another rider\'s trip, which exposes it '
            'through the ticket the attacker owns'
        )
    else:
        assert resp.status_code in REFUSED + (400,), (
            f'unexpected status {resp.status_code}'
        )


# ===========================================================================
# No endpoint may authorise by crashing
# ===========================================================================

@pytest.mark.parametrize('path', [
    '/api/v1/ride/trip/{id}/',
    '/api/v1/ride/trip/{id}/receipt/pdf/',
    '/api/v1/support/tickets/{id}/',
])
def test_a_nonexistent_id_is_refused_cleanly(attacker, path):
    """A 500 on an unknown id means the refusal is an accident of the code path.

    It also tends to mean a stack trace, and a stack trace is a description of the
    system handed to whoever asked for it.
    """
    resp = _client(attacker).get(path.format(id=99999999))

    assert resp.status_code in REFUSED, (
        f'{path} returned HTTP {resp.status_code} for a nonexistent id; '
        f'authorization here works by crashing'
    )
