"""D — a list endpoint must not hand back everything it has.

WHY THIS IS NOT ANSWERED BY THE SETTING
---------------------------------------
`REST_FRAMEWORK['DEFAULT_PAGINATION_CLASS']` is set to `LimitOffsetPagination` with
`PAGE_SIZE: 20`, which reads like the question is settled. It is not. That default is
applied by DRF's *generic* views and viewsets, through `self.paginate_queryset`.
Almost every list endpoint in this project is a function-based `@api_view`, and a
function-based view builds its own response. The setting does nothing for it.

So the only way to know is to put a lot of rows behind each endpoint and count what
comes back. That is what these tests do, at the HTTP boundary, with a real token.

WHAT UNBOUNDED ACTUALLY COSTS
-----------------------------
Not just latency. A rider with two years of history, or an operator opening the trip
list, pulls every matching row into Python, serialises all of it, and holds it in
memory in one web worker while doing so. On a small container that is the request
that takes the process down, and it takes every concurrent request with it. It also
gets slowly worse in a way nobody notices until it is bad, because it is fine for
every account that is new.

HOW A FAILURE READS
-------------------
A failing test here names the endpoint and the number of rows it returned. It does
not assert a specific page size -- 20, 50 and 100 are all defensible -- only that the
count does not grow with the data.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Notification, Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

# Enough rows that an unbounded endpoint is unmistakable, few enough that the test
# stays fast. A bounded endpoint returns a page; an unbounded one returns all of it.
MANY = 120

# The largest page any of these endpoints could defensibly return. Above this, the
# response is growing with the data rather than being paged.
MAX_DEFENSIBLE_PAGE = 100


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _client_for(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


@pytest.fixture
def rider_with_history():
    u = User.objects.create_user(phone_number='+919544000001', role='rider',
                                 username='+919544000001')
    Rider.objects.create(user_id=u)
    du = User.objects.create_user(phone_number='+919644000001', role='driver',
                                  username='+919644000001')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    d = Driver.objects.create(user_id=du, approved=True, status='online')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number='TS09PG1111')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])

    now = timezone.now()
    completed = _status('completed')
    Trip.objects.bulk_create([
        Trip(
            user_id=u, driver_id=d, status_id=completed,
            pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
            destination_lat=Decimal('17.4550000'),
            destination_long=Decimal('78.3900000'),
            estimated_fare=Decimal('150.00'), payment_method='cash',
            accepted_at=now, started_at=now, completed_at=now,
        )
        for _ in range(MANY)
    ])
    return {'rider': u, 'driver': d, 'driver_user': du}


def _rows(body):
    """Pull the list out of whatever envelope the endpoint uses."""
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return None
    for key in ('results', 'data', 'trips', 'notifications', 'history',
                'earnings', 'items'):
        v = body.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ('results', 'trips', 'history', 'items'):
                if isinstance(v.get(k2), list):
                    return v[k2]
    # A single list value anywhere in the envelope.
    lists = [v for v in body.values() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else None


def _assert_bounded(client, url, label):
    resp = client.get(url)
    assert resp.status_code == 200, f'{label}: HTTP {resp.status_code} {resp.content[:200]}'
    rows = _rows(resp.json())
    assert rows is not None, (
        f'{label}: could not find the list in the response envelope; keys were '
        f'{sorted(resp.json())[:12]}'
    )
    assert len(rows) <= MAX_DEFENSIBLE_PAGE, (
        f'{label} returned {len(rows)} rows for an account with {MANY} records. '
        f'The response grows with the data, so this endpoint gets slower and '
        f'heavier for every account for as long as the account exists.'
    )
    return len(rows)


def test_rider_history_is_paged(rider_with_history):
    """The rider app home screen. Every rider hits this on every launch."""
    client = _client_for(rider_with_history['rider'])
    _assert_bounded(client, '/api/v1/ride/ride-history/', 'rider ride-history')


def test_driver_history_is_paged(rider_with_history):
    """A full-time driver accumulates history faster than any rider."""
    client = _client_for(rider_with_history['driver_user'])
    _assert_bounded(client, '/api/v1/ride/driver-history/', 'driver-history')


def test_notifications_are_paged(rider_with_history):
    """Notifications are append-only and nothing prunes them."""
    rider = rider_with_history['rider']
    Notification.objects.bulk_create([
        Notification(user_id=rider, title=f'n{i}', message='m')
        for i in range(MANY)
    ])
    client = _client_for(rider)
    _assert_bounded(client, '/api/v1/rider/notifications/', 'notifications')


def test_driver_earnings_is_bounded(rider_with_history):
    """Earnings is the screen a driver refreshes most.

    The settlement rows have to exist for this to mean anything. The first version
    of this test asserted against an empty feed -- the trips were bulk-created with
    no ledger rows behind them -- and passed while proving nothing. So the ledger
    is seeded here, and the seeding is asserted before the bound is.
    """
    from servers.rider.models import WalletTransaction

    driver_user = rider_with_history['driver_user']
    trips = list(Trip.objects.filter(driver_id=rider_with_history['driver'])
                 .values_list('id', flat=True))
    assert len(trips) >= MANY, 'setup: not enough trips to page'

    WalletTransaction.objects.bulk_create([
        WalletTransaction(
            user_id=driver_user, amount=Decimal('30.00'), txn_type='debit',
            status='completed', purpose='trip_commission',
            reference_id=f'TRIP_{tid}', idempotency_key=f'settle-{tid}',
        )
        for tid in trips
    ])

    client = _client_for(driver_user)
    resp = client.get('/api/v1/driver/earnings/')
    assert resp.status_code == 200, resp.content[:200]
    rows = _rows(resp.json())
    assert rows is not None, f'no list in the envelope: {sorted(resp.json())}'
    assert len(rows) > 0, (
        'the earnings feed came back empty even with settlement rows seeded, so '
        'this test would pass whatever the page size was'
    )
    assert len(rows) <= MAX_DEFENSIBLE_PAGE, (
        f'driver earnings returned {len(rows)} rows for a driver with '
        f'{len(trips)} settled trips'
    )


def test_the_pagination_default_is_not_mistaken_for_coverage():
    """Documentation, enforced.

    The DRF default exists and is correct, and it applies to generic views. This
    project's list endpoints are function-based, so the default does not reach
    them. Asserting the setting here would be the kind of test that passes while
    the endpoints above return everything -- so instead this records why the tests
    above go through HTTP.
    """
    from django.conf import settings
    cfg = settings.REST_FRAMEWORK
    assert cfg.get('DEFAULT_PAGINATION_CLASS'), (
        'the DRF pagination default was removed; generic views and viewsets are '
        'now unbounded too'
    )
    assert int(cfg.get('PAGE_SIZE', 0)) > 0
