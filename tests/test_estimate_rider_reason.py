"""The choose-a-ride demand line: rider copy only, never the word "surge" (2026-11-06).

`rider_reason` is set when the quote's demand multiplier is above 1 and is
None otherwise. It reads the multiplier; it never changes the fare.
"""

import pytest

from servers.ride.views import _rider_reason

pytestmark = pytest.mark.django_db


def test_no_reason_at_or_below_one():
    assert _rider_reason({'surge_multiplier': '1.00'}) is None
    assert _rider_reason({'surge_multiplier': '0.90'}) is None
    assert _rider_reason({}) is None


def test_the_reason_names_the_pricing_zone():
    from servers.pricing.models import ServiceZone

    zone = ServiceZone.objects.filter(is_active=True).exclude(name='').first()
    if zone is None:
        pytest.skip('no named service zone in this database')
    reason = _rider_reason({'surge_multiplier': '1.30', 'zone_code': zone.code})
    assert reason == f'Higher demand near {zone.name.strip()}'
    assert 'surge' not in reason.lower()


def test_no_zone_is_a_plain_line():
    assert _rider_reason({'surge_multiplier': '1.15', 'zone_code': ''}) == 'Higher demand right now'
    assert _rider_reason({'surge_multiplier': '1.50', 'zone_code': 'NO-SUCH-ZONE'}) == 'Higher demand right now'


def test_garbage_multiplier_is_no_reason():
    assert _rider_reason({'surge_multiplier': 'n/a'}) is None


def test_the_quote_carries_the_field(client):
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient
    from rest_framework_simplejwt.tokens import AccessToken

    from servers.rider.models import Rider

    user = get_user_model().objects.create_user(phone_number='+919566000071', role='rider')
    Rider.objects.create(user_id=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    resp = c.post('/api/v1/ride/estimate-fare/', {
        'pickup_lat': '17.4450', 'pickup_long': '78.3800',
        'destination_lat': '17.4660', 'destination_long': '78.3800',
        'distance_km': '2.4', 'duration_min': '8', 'vehicle_type': 'sedan',
    }, format='json')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    payload = body.get('data', body)
    assert 'rider_reason' in payload
    multiplier = float(payload['fare_breakdown']['surge_multiplier'])
    assert (payload['rider_reason'] is None) == (multiplier <= 1)
