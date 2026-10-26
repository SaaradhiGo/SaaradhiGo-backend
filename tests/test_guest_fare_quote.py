"""A visitor who has not signed in can be quoted a fare, within a cap.

Opening `/ride/estimate-fare/` to anonymous callers is a deliberate product
decision -- a rider should see the price before being asked to create an
account -- and it is also the kind of change that quietly becomes an open,
billed distance API if nobody pins it down. Each distinct trip costs one
Google Distance Matrix call.

So these tests assert both halves:

  * a guest really is served (the permission change works), and
  * a guest really is bounded (the spend cap works, and the un-evadable
    global ceiling works even when the per-IP key is rotated).

The previous guest endpoint shipped with a throttle that counted nothing for
weeks, because DRF's default `get_ident` joins the whole X-Forwarded-For
chain and Railway's edge varies a hop per request. That is exactly the sort
of failure these tests exist to catch, which is why the rotating-IP case is
here rather than only the simple one.
"""
import pytest
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


TRIP = {
    'pickup_lat': 17.4169,
    'pickup_long': 78.4387,
    'destination_lat': 17.4401,
    'destination_long': 78.3489,
    'distance_km': 9.9,
    'duration_min': 22,
    'vehicle_type': 'sedan',
}


@pytest.fixture(autouse=True)
def _clear_throttle_history():
    """Each test starts with an empty bucket, and leaves one behind."""
    cache.clear()
    yield
    cache.clear()


def _url():
    return '/api/v1/ride/estimate-fare/'


def test_anonymous_caller_is_not_rejected_for_being_anonymous():
    """The point of the change: no 401/403 for a guest.

    The fare itself depends on a service zone and a rate card that a bare
    test database may not have, so this asserts what the PERMISSION layer
    does rather than what the pricing layer returns. 401 or 403 would mean
    the guest never reached the view at all.
    """
    client = APIClient()
    response = client.post(_url(), TRIP, format='json')

    assert response.status_code not in (401, 403), (
        'a guest was refused by the permission layer; estimate-fare is '
        'supposed to answer anonymous callers'
    )


def test_a_guest_is_capped_and_the_cap_says_so():
    """The per-IP cap bites, and the answer is 429 rather than a silent miss."""
    client = APIClient()
    statuses = []
    for _ in range(140):  # guest_fare is 120/hour
        r = client.post(_url(), TRIP, format='json', REMOTE_ADDR='203.0.113.7')
        statuses.append(r.status_code)
        if r.status_code == 429:
            break

    assert 429 in statuses, (
        'no 429 in 140 anonymous quotes against a 120/hour cap -- the '
        'throttle is not counting. Check the cache backend and get_ident.'
    )


def test_rotating_the_client_address_does_not_defeat_the_ceiling():
    """The global bucket is the one that actually bounds the bill.

    The per-IP key comes from `X-Forwarded-For`, which the client supplies,
    so a caller willing to rotate it walks around the per-IP cap. The global
    ceiling is shared by every anonymous request and cannot be walked
    around; without it the per-IP cap alone is decorative.
    """
    client = APIClient()
    saw_429 = False
    for i in range(420):  # guest_fare_global is 400/hour
        r = client.post(
            _url(), TRIP, format='json',
            HTTP_X_FORWARDED_FOR=f'198.51.100.{i % 254 + 1}',
        )
        if r.status_code == 429:
            saw_429 = True
            break

    assert saw_429, (
        'a caller rotating X-Forwarded-For was never refused -- the global '
        'ceiling is not holding, so the per-IP cap is the only control and '
        'it is trivially evaded'
    )


def test_a_signed_in_rider_skips_the_guest_cap(django_user_model):
    """The cap is for guests. A rider who signed in is unaffected by it."""
    user = django_user_model.objects.create_user(
        phone_number='+919999000091',
        email='rider.fare@saaradhigo.com',
        password='x' * 14,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    # Well past the anonymous cap; none of these should be throttled.
    for _ in range(130):
        r = client.post(_url(), TRIP, format='json', REMOTE_ADDR='203.0.113.7')
        assert r.status_code != 429, (
            'a signed-in rider hit the guest throttle; get_cache_key is '
            'supposed to return None for an authenticated request'
        )


# ---------------------------------------------------------------------------
# The route line, which is part of the same picture as the markers.
# ---------------------------------------------------------------------------
#
# `/ride/maps/directions/` was `IsAuthenticated` while place search and place
# details had already been opened to guests, so a signed-out visitor got the
# map and both markers and NO line between them -- plus a 401 in the console
# of every booking page. Two halves of one picture disagreeing about who may
# see it. Found on deployed QA, not in review.

# NO TRAILING SLASH. `servers/ride/urls.py` registers this one as
# `maps/directions`, unlike every other route in the project. With a
# slash these requests 404, the throttle is never consulted, and the
# permission assertion below passes without the guest reaching the view.
DIRECTIONS = '/api/v1/ride/maps/directions'

ROUTE = {
    'origin_lat': 17.4844,
    'origin_lng': 78.3893,
    'dest_lat': 17.4335,
    'dest_lng': 78.3865,
}


def test_a_guest_is_not_refused_a_route_for_being_anonymous():
    """What the PERMISSION layer does, not what Google returns.

    Without a Maps key the proxy answers 502, which is fine here: the thing
    under test is that the guest reached the view at all. 401 or 403 means
    the gate is back.
    """
    response = APIClient().post(DIRECTIONS, ROUTE, format='json')

    assert response.status_code not in (401, 403), (
        'a guest was refused the route for their own trip; they already '
        'have the two markers it joins'
    )


def test_anonymous_route_lookups_are_capped():
    """Opening it must not mean uncapping it.

    Driven against the configured rate rather than a patched one: DRF
    resolves throttle rates through its own cached settings object, so a
    test that lowers the rate at runtime can measure the old one and pass
    for the wrong reason.

    Each request uses a DIFFERENT destination, or the server-side cache
    answers without the throttle ever being consulted -- which would make
    this assert nothing.
    """
    client = APIClient()
    statuses = []
    for i in range(140):  # guest_route is 120/hour
        r = client.post(
            DIRECTIONS,
            {**ROUTE, 'dest_lat': 17.40 + i / 10000},
            format='json',
            REMOTE_ADDR='203.0.113.9',
        )
        statuses.append(r.status_code)
        if r.status_code == 429:
            break

    assert 429 in statuses, (
        'no 429 in 140 anonymous route lookups against a 120/hour cap -- '
        'the throttle is not counting. Check the cache backend and get_ident.'
    )


def test_a_signed_in_rider_skips_the_guest_route_cap(django_user_model):
    """The cap is for guests, and inherits that rule from GuestMapsThrottle."""
    user = django_user_model.objects.create_user(
        phone_number='+919999000092',
        email='rider.route@saaradhigo.com',
        password='x' * 14,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    for i in range(10):
        r = client.post(
            DIRECTIONS, {**ROUTE, 'dest_lat': 17.40 + i / 10000}, format='json')
        assert r.status_code != 429, (
            'a signed-in rider hit the guest route throttle; get_cache_key '
            'is supposed to return None for an authenticated caller'
        )
