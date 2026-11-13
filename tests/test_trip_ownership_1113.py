"""Someone else's trip answers 404 on every rider and driver trip endpoint (13 Nov).

QA: the both-roles account opened /ride/trip/207 (another rider's completed trip) and the web
showed "Finding your driver". Whatever the web shows, the API must never hand a trip, its state,
its receipt, its chat or its share link to anyone but its rider and its driver -- and must not
even say the trip exists (403 would): 404.

The URL list is discovered, not typed: every non-admin URL that takes a trip id is called, so an
endpoint added later is covered without anyone remembering to add it here.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider

User = get_user_model()
pytestmark = pytest.mark.django_db

TRIP_KWARGS = ('trip_id', 'tripId', 'pk_trip')


def _walk(patterns, prefix=''):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern)


def trip_urls():
    """Every non-staff URL with a trip id in it, as a format string."""
    out = []
    for route in _walk(get_resolver().url_patterns):
        if not any(f'<int:{k}>' in route for k in TRIP_KWARGS):
            continue
        if '/admin/' in f'/{route}' or route.startswith('admin') or 'admin/' in route:
            continue
        url = '/' + route.lstrip('^').rstrip('$')
        for k in TRIP_KWARGS:
            url = url.replace(f'<int:{k}>', '{id}')
        if '<' in url:  # another path parameter we cannot fill in honestly
            continue
        out.append(url)
    return sorted(set(out))


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


def _status(code):
    return TripStatus.objects.get_or_create(status_code=code)[0]


@pytest.fixture
def world():
    owner = User.objects.create_user(phone_number='+919522700001', role='rider')
    Rider.objects.create(user_id=owner)
    driver_u = User.objects.create_user(phone_number='+919622700001', role='driver')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    driver = Driver.objects.create(user_id=driver_u, approved=True, status='online')
    vehicle = Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt, vehicle_number='TS09OW0001')
    now = timezone.now()
    done = Trip.objects.create(
        user_id=owner, driver_id=driver, vehicle_id=vehicle, status_id=_status('completed'),
        pickup_lat=Decimal('17.44'), pickup_long=Decimal('78.38'),
        destination_lat=Decimal('17.45'), destination_long=Decimal('78.39'),
        estimated_fare=Decimal('183.00'), payment_method='cash',
        accepted_at=now, started_at=now, completed_at=now,
    )
    live = Trip.objects.create(
        user_id=owner, status_id=_status('requested'),
        pickup_lat=Decimal('17.44'), pickup_long=Decimal('78.38'),
        destination_lat=Decimal('17.45'), destination_long=Decimal('78.39'),
        estimated_fare=Decimal('150.00'), payment_method='cash',
    )
    # Strangers: another rider, another driver, and an account that both rides and drives.
    rider_x = User.objects.create_user(phone_number='+919522700002', role='rider')
    Rider.objects.create(user_id=rider_x)
    driver_x_u = User.objects.create_user(phone_number='+919622700002', role='driver')
    Driver.objects.create(user_id=driver_x_u, approved=True)
    both = User.objects.create_user(phone_number='+919622700003', role='driver')
    Driver.objects.create(user_id=both, approved=True)
    Rider.objects.create(user_id=both)
    return {'trips': [done, live], 'strangers': {'rider': rider_x, 'driver': driver_x_u, 'both': both}}


def test_the_trip_endpoints_are_discovered():
    urls = trip_urls()
    assert '/api/v1/ride/trip/{id}/' in urls
    assert len(urls) >= 8, urls


def _answer(client, method, path):
    res = getattr(client, method)(path, {}, format='json')
    try:
        code = (res.json().get('error') or {}).get('code') or res.json().get('detail')
    except Exception:
        code = None
    return res.status_code, code, res.content.decode('utf-8', 'replace')


@pytest.mark.parametrize('who', ['rider', 'driver', 'both'])
def test_someone_elses_trip_is_404_on_every_endpoint(world, who):
    """404, answered exactly as for a trip that does not exist. The one exception is a refusal for
    the caller's ROLE (a rider calling a driver-only endpoint), which is the same for any trip id
    and so says nothing about this one."""
    client = _client(world['strangers'][who])
    missing = Trip.objects.order_by('-id').first().id + 1000
    wrong = []
    for trip in world['trips']:
        for url in trip_urls():
            for method in ('get', 'post'):
                st, code, body = _answer(client, method, url.format(id=trip.id))
                if st == 405:
                    continue
                st0, code0, _ = _answer(client, method, url.format(id=missing))
                leaked = st < 400 or str(trip.estimated_fare) in body
                same = (st, code) == (st0, code0)
                role_refusal = st == 403 and same
                if leaked or not same or (st != 404 and not role_refusal):
                    wrong.append(f'{method.upper()} {url} -> {st} {code} (nonexistent trip: {st0} {code0})')
    assert not wrong, '\n'.join(wrong)


def test_the_owner_still_sees_their_trip(world):
    trip = world['trips'][0]
    res = _client(trip.user_id).get(f'/api/v1/ride/trip/{trip.id}/')
    assert res.status_code == 200
