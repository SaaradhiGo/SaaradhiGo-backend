"""The decomposition must close in the HTTP RESPONSE, not just in the service.

This file exists because of a mistake worth keeping visible.

The fare fix was made in `quote_fare`, forwarded through `estimate_amount`, and
covered by 23 unit tests that all passed. Then a check against the live QA quote
showed `fare_adjustments` absent from the API entirely, with base + distance + time
coming to 135.21 beside a charged 169.02 -- the surge uplift completely unexplained.

There were THREE places that rebuild the payload, not two: the service, the
backwards-compatibility wrapper, and the view, which constructs its own response
dict field by field. Unit tests against the service and the wrapper could not see
the third one.

So these tests go through the API. The invariant is only worth anything where a
client actually reads it.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

QUOTE_URL = '/api/v1/ride/estimate-fare/'


@pytest.fixture
def rider_client():
    user = User.objects.create_user(phone_number='+919566000001', role='rider')
    Rider.objects.create(user_id=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


def _quote(client, km='2.4', minutes='8', vehicle_type='sedan'):
    resp = client.post(QUOTE_URL, {
        'pickup_lat': '17.4450', 'pickup_long': '78.3800',
        'destination_lat': '17.4660', 'destination_long': '78.3800',
        'distance_km': km, 'duration_min': minutes,
        'vehicle_type': vehicle_type,
    }, format='json')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body.get('data', body)


def _closes(payload):
    """Add it up the way a rider reading the panel would."""
    fb = payload.get('fare_breakdown') or {}
    components = sum(
        (Decimal(str(fb[k])) for k in ('base_fare', 'distance_fare', 'time_fare')
         if k in fb),
        Decimal('0.00'),
    )
    adjustments = sum(
        (Decimal(str(a['amount'])) for a in payload.get('fare_adjustments', [])),
        Decimal('0.00'),
    )
    total = Decimal(str(payload['estimated_fare']))
    return components, adjustments, total


def test_the_api_response_carries_the_adjustment_lines(rider_client):
    """The field the view was dropping."""
    payload = _quote(rider_client)

    assert 'fare_adjustments' in payload, (
        'the API response has no fare_adjustments; a client cannot render a '
        'decomposition that adds up'
    )
    assert isinstance(payload['fare_adjustments'], list)


def test_the_api_decomposition_closes_exactly(rider_client):
    """The invariant, where it actually matters."""
    components, adjustments, total = _closes(_quote(rider_client))

    assert components + adjustments == total, (
        f'the API returns lines that do not sum to the quoted fare: '
        f'components={components} adjustments={adjustments} total={total}'
    )


@pytest.mark.parametrize('km,minutes', [
    ('0.4', '2'), ('2.4', '8'), ('3.33', '11'), ('12.5', '40'), ('40', '110'),
])
def test_it_closes_across_distances_through_the_api(rider_client, km, minutes):
    components, adjustments, total = _closes(_quote(rider_client, km, minutes))

    assert components + adjustments == total, f'{km} km / {minutes} min'


def test_each_adjustment_is_json_safe_and_labelled(rider_client):
    """The response crosses JSON, so amounts must be strings a client can parse.

    Decimal does not survive JSON, and a float would put money in binary floating
    point on the way to the rider.
    """
    payload = _quote(rider_client, '0.4', '2')

    for adj in payload['fare_adjustments']:
        assert set(adj) == {'code', 'label', 'amount'}, (
            f'unexpected adjustment shape: {sorted(adj)}'
        )
        assert isinstance(adj['amount'], str), (
            'amount must be a string, as the other money fields in this response '
            'are'
        )
        assert Decimal(adj['amount']) is not None
        assert adj['label'], f'{adj["code"]} has no human-readable label'


def test_the_quoted_total_is_unchanged_by_the_new_field(rider_client):
    """Adding the lines must not have moved the price.

    Two quotes for the same journey must agree, and the total must still be the
    aggregate-then-round value rather than the sum of the rounded components.
    """
    first = _quote(rider_client)
    second = _quote(rider_client)

    assert first['estimated_fare'] == second['estimated_fare']

    components, adjustments, total = _closes(first)
    rounding = sum(
        (Decimal(str(a['amount'])) for a in first['fare_adjustments']
         if a['code'] == 'rounding'),
        Decimal('0.00'),
    )
    other = adjustments - rounding
    assert total - components - other == rounding


def test_no_internal_name_leaks_into_the_labels(rider_client):
    """A rider reads these strings."""
    payload = _quote(rider_client, '0.4', '2')

    for adj in payload['fare_adjustments']:
        label = adj['label']
        for leaked in ('FarePricing', 'RateCard', 'fare_basis', 'subtotal',
                       'quote_fare', 'min_fare_applied'):
            assert leaked not in label, (
                f'{leaked!r} is an internal name and must not reach a rider'
            )


def test_final_fare_is_not_in_the_quote(rider_client):
    """A quote must never assert a metered final fare."""
    payload = _quote(rider_client)

    assert 'final_fare' not in payload
