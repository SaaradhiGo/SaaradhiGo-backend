"""Raw GPS trails must stay unreachable through the API.

A trip's `TripLocationPoint` rows are the most sensitive data the platform holds:
a minute-by-minute record of where a named person physically went. The audit
finding is that today they are reachable by **nothing** -- no serializer, no view,
no route, no admin registration. The only readers are the writer itself and the
actual-metrics computation.

That is the right posture, so these tests exist to keep it rather than to fix it.
They fail the moment somebody adds a read path, which is exactly when a human
should look at it and decide deliberately -- the alternative is a trail quietly
appearing in a `fields = '__all__'` serializer one day.

`admin_live_locations` is the one endpoint that returns coordinates at all. It
serves live fleet positions from Redis, not the durable trail, and it is gated on
`IsAuthenticated, IsAdmin`; both facts are asserted below.
"""

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripLocationPoint, TripStatus

User = get_user_model()

LAT, LNG = 17.4450000, 78.3800000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ride(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    duser = User.objects.create_user(phone_number='+919851000001', role='driver')
    driver = Driver.objects.create(user_id=duser, approved=True, status='active')
    Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt,
                           vehicle_number='TS09AC0001')
    rider = User.objects.create_user(phone_number='+919751000001', role='rider')
    other_rider = User.objects.create_user(phone_number='+919751000002',
                                           role='rider')
    ouser = User.objects.create_user(phone_number='+919851000002', role='driver')
    other_driver = Driver.objects.create(user_id=ouser, approved=True,
                                         status='active')

    st, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=st,
        pickup_lat=Decimal(str(LAT)), pickup_long=Decimal(str(LNG)),
        destination_lat=Decimal(str(LAT + 0.01)), destination_long=Decimal(str(LNG)),
        estimated_fare=Decimal('150.00'), payment_method='cash',
    )

    from django.utils import timezone
    now = timezone.now()
    for i in range(5):
        TripLocationPoint.objects.create(
            trip=trip, driver_id=driver.id,
            latitude=Decimal(str(LAT + 0.0005 * i)),
            longitude=Decimal(str(LNG)),
            recorded_at=now, sequence=i,
            source=TripLocationPoint.SOURCE_DRIVER_WS,
            source_event_id=f'acl-{i}-0',
        )

    return {
        'trip': trip, 'rider': rider, 'driver': driver,
        'other_rider': other_rider, 'other_driver': other_driver,
        'driver_user': duser, 'other_driver_user': ouser,
    }


def _api(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user=user)
    return c


def _leaks_a_trail(body):
    """True if a response body carries anything that looks like a GPS trail.

    Looks for the durable trail's own field names rather than for coordinates: a
    trip legitimately returns its pickup and drop, and flagging those would make
    this test useless.
    """
    blob = json.dumps(body) if not isinstance(body, str) else body
    return any(marker in blob for marker in (
        'source_event_id', 'triplocationpoint', 'location_points',
        '"sequence"', 'recorded_at',
    ))


# ---------------------------------------------------------------------------
# The posture: no read path exists
# ---------------------------------------------------------------------------

def test_no_serializer_exposes_the_durable_trail():
    """A serializer over TripLocationPoint is how this data would escape.

    Checked by import rather than by grep so it holds as the codebase moves.
    """
    import importlib
    import pkgutil

    import servers

    offenders = []
    for mod in pkgutil.walk_packages(servers.__path__, prefix='servers.'):
        if not mod.name.endswith('serializers'):
            continue
        try:
            m = importlib.import_module(mod.name)
        except Exception:  # noqa: BLE001 -- an unimportable module is not a leak
            continue
        for name in dir(m):
            obj = getattr(m, name)
            meta = getattr(obj, 'Meta', None)
            if meta is not None and getattr(meta, 'model', None) is TripLocationPoint:
                offenders.append(f'{mod.name}.{name}')

    assert not offenders, (
        'a serializer now exposes the raw GPS trail: '
        f'{offenders}. Raw location history is the most sensitive data here; if '
        'this is deliberate, gate it and update this test with the reasoning.'
    )


def test_the_trail_is_not_registered_in_the_django_admin():
    """Admin registration would make the whole trail browsable and exportable."""
    from django.contrib import admin

    assert TripLocationPoint not in admin.site._registry, (
        'TripLocationPoint is registered in the Django admin, which makes every '
        "rider's movement history browsable and CSV-exportable by any staff user"
    )


def test_no_route_mentions_a_trail_or_points_endpoint():
    """A route is the other way this becomes reachable."""
    from django.urls import get_resolver

    def _patterns(resolver, prefix=''):
        for p in resolver.url_patterns:
            pat = prefix + str(getattr(p, 'pattern', ''))
            if hasattr(p, 'url_patterns'):
                yield from _patterns(p, pat)
            else:
                yield pat

    suspicious = [
        r for r in _patterns(get_resolver())
        if any(w in r.lower() for w in ('location-point', 'location_point',
                                        'trail', 'gps'))
    ]
    assert not suspicious, (
        f'a route now looks like a GPS trail endpoint: {suspicious}')


# ---------------------------------------------------------------------------
# The trip endpoints must not carry a trail
# ---------------------------------------------------------------------------

def test_the_rider_reading_their_own_trip_gets_no_trail(ride):
    """The owner of the ride is the most legitimate reader, and still gets none.

    Nobody needs a raw trail to see their receipt, so the default must be absence.
    """
    resp = _api(ride['rider']).get(f"/api/v1/ride/trip/{ride['trip'].id}/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert not _leaks_a_trail(body), (
        f'the trip endpoint now returns GPS trail data: {json.dumps(body)[:400]}')


def test_the_driver_reading_their_own_trip_gets_no_trail(ride):
    resp = _api(ride['driver_user']).get(f"/api/v1/ride/trip/{ride['trip'].id}/")
    assert resp.status_code in (200, 403, 404), resp.content
    if resp.status_code == 200:
        assert not _leaks_a_trail(resp.json())


def test_another_rider_cannot_read_the_trip_at_all(ride):
    """IDOR check on the trip itself, which is where a trail would hang from."""
    resp = _api(ride['other_rider']).get(f"/api/v1/ride/trip/{ride['trip'].id}/")
    assert resp.status_code in (403, 404), (
        f'another rider read this trip: HTTP {resp.status_code} '
        f'{resp.content[:200]}')


def test_another_driver_cannot_read_the_trip_at_all(ride):
    resp = _api(ride['other_driver_user']).get(
        f"/api/v1/ride/trip/{ride['trip'].id}/")
    assert resp.status_code in (403, 404), (
        f'an unrelated driver read this trip: HTTP {resp.status_code} '
        f'{resp.content[:200]}')


def test_an_anonymous_caller_gets_nothing(ride):
    resp = _api().get(f"/api/v1/ride/trip/{ride['trip'].id}/")
    assert resp.status_code in (401, 403), resp.status_code


# ---------------------------------------------------------------------------
# The one endpoint that does return coordinates
# ---------------------------------------------------------------------------

def test_live_locations_require_admin(ride):
    """`admin_live_locations` serves live fleet positions from Redis.

    Deliberate, and admin-only. A rider or driver reaching it would be able to see
    where every driver on the platform currently is.
    """
    for label, user in (('rider', ride['rider']),
                        ('driver', ride['driver_user']),
                        ('anonymous', None)):
        resp = _api(user).get('/api/v1/ride/admin/live-locations/')
        assert resp.status_code in (401, 403), (
            f'{label} reached admin live locations: HTTP {resp.status_code}')


def test_live_locations_serve_redis_not_the_durable_trail(ride, db):
    """An admin sees current positions, not ride history.

    The distinction matters: live positions are operational and transient, while a
    trail is a permanent record of a named person's movements.
    """
    admin = User.objects.create_user(phone_number='+919851000099', role='admin')
    admin.is_staff = True
    admin.save()

    resp = _api(admin).get('/api/v1/ride/admin/live-locations/')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert not _leaks_a_trail(body), (
        f'live locations now include durable trail rows: {json.dumps(body)[:400]}')
    data = body.get('data', body)
    assert set(data.keys()) <= {'drivers', 'riders'}, data.keys()
