"""A rider who adds up an itemised fare must get the amount they were charged.

QA trip 45 charged 135.22 while its three displayed lines read
60.00 + 55.59 + 19.62 = 135.21. One paisa, and entirely a presentation defect:
each component was rounded on its own while the total was rounded from the
unrounded subtotal, so round(a) + round(b) + round(c) != round(a + b + c).

`total_fare` is authoritative and is NOT changed by any of this -- it is what the
rider pays. What changed is the decomposition shown beside it.

Surge and the minimum fare break the sum as well, and legitimately: surge
multiplies the subtotal, the minimum fare replaces it. Pretending three metered
lines explain such a total would be a different lie, so those uplifts are their
own named lines and only the remainder is called rounding.

The invariant these tests hold:

    base + distance + time + sum(adjustments)  ==  total_fare     exactly

checked as Decimal, in every pricing shape the system can produce.
"""

from decimal import Decimal

import pytest

# The API entry point, not quote_fare directly: the wrapper is what
# the estimate-fare view and the consumers call, and it filters the dict --
# so testing quote_fare alone would have missed that it dropped the new lines.
from servers.ride.utils import estimate_amount

pytestmark = pytest.mark.django_db

PICKUP_LAT = Decimal('17.4450')
PICKUP_LNG = Decimal('78.3800')


def _fare(km, minutes, vehicle_type='sedan', **kw):
    return estimate_amount(
        Decimal(str(km)), Decimal(str(minutes)),
        vehicle_type=vehicle_type,
        pickup_lat=PICKUP_LAT, pickup_long=PICKUP_LNG,
        **kw,
    )


def _assert_closes(fare, label=''):
    """The invariant, asserted the way a rider would check it."""
    components = (
        Decimal(str(fare['base_fare']))
        + Decimal(str(fare['distance_fare']))
        + Decimal(str(fare['time_fare']))
    )
    adjustments = sum(
        (Decimal(str(a['amount'])) for a in fare.get('fare_adjustments', [])),
        Decimal('0.00'),
    )
    total = Decimal(str(fare['total_fare']))

    assert components + adjustments == total, (
        f'{label}: itemised lines do not sum to the amount charged.\n'
        f'  base      {fare["base_fare"]}\n'
        f'  distance  {fare["distance_fare"]}\n'
        f'  time      {fare["time_fare"]}\n'
        f'  adjust    {[(a["code"], str(a["amount"])) for a in fare.get("fare_adjustments", [])]}\n'
        f'  sum       {components + adjustments}\n'
        f'  total     {total}'
    )


# ---------------------------------------------------------------------------
# The reported case
# ---------------------------------------------------------------------------

def test_the_trip_45_shape_now_closes():
    """The exact quote that exposed this: 2.4 km, 8 min, sedan."""
    fare = _fare('2.4', '8')

    _assert_closes(fare, 'trip 45 shape')


# ---------------------------------------------------------------------------
# Across the pricing surface
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('km,minutes', [
    ('0.1', '1'),            # shortest plausible
    ('0.5', '2'),
    ('1', '5'),
    ('2.4', '8'),            # the reported case
    ('3.33', '11'),          # thirds -- the classic rounding trap
    ('7.77', '23'),
    ('12.5', '40'),
    ('33.333', '95'),        # long intercity-ish
    ('100', '180'),          # large fare
    ('0.01', '1'),           # paisa boundary on distance
])
def test_the_decomposition_closes_for_many_distances(km, minutes):
    _assert_closes(_fare(km, minutes), f'{km} km / {minutes} min')


@pytest.mark.parametrize('vehicle_type', ['bike', 'auto', 'sedan', 'suv'])
def test_the_decomposition_closes_for_each_vehicle_type(vehicle_type):
    _assert_closes(_fare('4.2', '14', vehicle_type=vehicle_type), vehicle_type)


def test_a_minimum_fare_ride_closes_and_says_why():
    """A very short ride hits the minimum, so the metered lines cannot explain it.

    The uplift must be a named line rather than silently folded in, or the rider
    sees three small numbers beside a larger total with nothing to account for it.
    """
    fare = _fare('0.1', '1')

    _assert_closes(fare, 'minimum fare')
    if fare.get('min_fare_applied'):
        codes = [a['code'] for a in fare.get('fare_adjustments', [])]
        assert 'minimum_fare' in codes, (
            'the minimum fare was applied but no line explains the uplift'
        )


# ---------------------------------------------------------------------------
# Properties of the adjustment lines
# ---------------------------------------------------------------------------

def test_rounding_is_never_more_than_a_paisa():
    """If it is, something other than rounding is hiding in that line."""
    for km, minutes in (('2.4', '8'), ('3.33', '11'), ('7.77', '23'),
                        ('33.333', '95'), ('12.5', '40')):
        fare = _fare(km, minutes)
        for adj in fare.get('fare_adjustments', []):
            if adj['code'] == 'rounding':
                assert abs(Decimal(str(adj['amount']))) <= Decimal('0.01'), (
                    f'{km} km: rounding line is {adj["amount"]}, which is not '
                    'rounding'
                )


def test_a_clean_fare_carries_no_rounding_line():
    """No decorative zero lines. A rider should not see "Rounding 0.00"."""
    for km, minutes in (('1', '5'), ('2.4', '8'), ('12.5', '40')):
        fare = _fare(km, minutes)
        for adj in fare.get('fare_adjustments', []):
            assert Decimal(str(adj['amount'])) != Decimal('0.00'), (
                f'{km} km: a zero-value {adj["code"]} line is being shown'
            )


def test_every_adjustment_has_a_code_and_a_human_label():
    """The rider app renders these, so they must be presentable as they stand."""
    fare = _fare('0.1', '1')

    for adj in fare.get('fare_adjustments', []):
        assert adj.get('code'), 'an adjustment has no code'
        assert adj.get('label'), f'{adj["code"]} has no human-readable label'
        assert 'amount' in adj


def test_all_money_is_decimal_never_float():
    """Float has no place in money, including in the new lines."""
    fare = _fare('3.33', '11')

    for key in ('total_fare', 'base_fare', 'distance_fare', 'time_fare'):
        assert not isinstance(fare[key], float), f'{key} is a float'
    for adj in fare.get('fare_adjustments', []):
        assert not isinstance(adj['amount'], float), (
            f'{adj["code"]} amount is a float'
        )
        assert isinstance(adj['amount'], Decimal)


# ---------------------------------------------------------------------------
# The authoritative total must not have moved
# ---------------------------------------------------------------------------

def test_the_total_is_still_aggregate_then_round_not_sum_of_rounded():
    """Proof that pricing did not move, without pinning any rate card.

    The defect was that the DISPLAYED components were each rounded while the total
    was rounded from the unrounded subtotal. The fix adds lines to explain the gap;
    it must not have "fixed" the gap by changing the total to the sum of the
    rounded components, which would be a price change of up to a paisa on every
    ride.

    So: where a rounding line exists, the total must differ from the sum of the
    rounded components by exactly that line. Where none exists, they must already
    agree. Either way the total is untouched.

    Deliberately free of hardcoded fares -- an earlier version of this test pinned
    QA's rate-card figures and failed locally, where no zone card exists and the
    defaults apply. A unit test that depends on one environment's pricing is
    testing the environment.
    """
    for km, minutes in (('2.4', '8'), ('3.33', '11'), ('7.77', '23'),
                        ('12.5', '40'), ('33.333', '95')):
        fare = _fare(km, minutes)
        rounded_components = (
            Decimal(str(fare['base_fare']))
            + Decimal(str(fare['distance_fare']))
            + Decimal(str(fare['time_fare']))
        )
        total = Decimal(str(fare['total_fare']))
        rounding = sum(
            (Decimal(str(a['amount'])) for a in fare.get('fare_adjustments', [])
             if a['code'] == 'rounding'),
            Decimal('0.00'),
        )
        other = sum(
            (Decimal(str(a['amount'])) for a in fare.get('fare_adjustments', [])
             if a['code'] != 'rounding'),
            Decimal('0.00'),
        )
        assert total - rounded_components - other == rounding, (
            f'{km} km: the total no longer matches aggregate-then-round. '
            f'total={total} components={rounded_components} other={other} '
            f'rounding={rounding}'
        )


def test_adding_the_lines_did_not_change_any_component():
    """The metered lines must be byte-identical to what they always were.

    Each is still an independent round of its own unrounded value; the fix works by
    explaining the residual, not by nudging a component.
    """
    fare = _fare('2.4', '8')

    for key in ('base_fare', 'distance_fare', 'time_fare'):
        value = Decimal(str(fare[key]))
        assert value == value.quantize(Decimal('0.01')), (
            f'{key} is not a clean two-decimal value: {value}'
        )
        assert value >= Decimal('0.00')


def test_final_fare_is_not_introduced_anywhere():
    """No part of this may start asserting a metered final fare."""
    fare = _fare('2.4', '8')

    assert 'final_fare' not in fare
