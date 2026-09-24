"""The fare snapshot: additive today, immutable when it matters.

`FarePricing` already behaved as a snapshot — one row written inside the
trip-creation transaction, never updated, already described as "the FarePricing
snapshot" by `receipts.py`. These tests pin the properties the added columns are
supposed to provide, and, just as importantly, pin that adding them changed
nothing about how a fare is produced today.

Nothing here writes a final snapshot in anger. Fare finalisation is a separate,
separately reviewed change; this is the shape it will write into.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from servers.driver.models import VehicleType
from servers.ride.models import FarePricing, Trip, TripStatus

User = get_user_model()

LAT = Decimal('17.4450000')
LNG = Decimal('78.3800000')


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def rider(db):
    return User.objects.create_user(phone_number='+919700000601', role='rider')


@pytest.fixture
def trip(db, rider):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return Trip.objects.create(
        user_id=rider, status_id=_status('completed'),
        requested_vehicle_type=vt,
        pickup_lat=LAT, pickup_long=LNG,
        destination_lat=LAT, destination_long=LNG,
        estimated_fare=Decimal('150.00'),
    )


def _snapshot(trip, **kw):
    base = dict(
        trip_id=trip, base_fare=Decimal('30.00'), distance_fare=Decimal('60.00'),
        time_fare=Decimal('30.00'), surge_multiplier=Decimal('1.00'),
        total_fare=Decimal('120.00'),
    )
    base.update(kw)
    return FarePricing.objects.create(**base)


# ---------------------------------------------------------------------------
# Nothing changed for existing behaviour
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_snapshot_can_still_be_written_with_only_the_original_columns(trip):
    """Trip creation writes exactly these five amounts and must keep working.

    Every added column is nullable or defaulted, so the existing call site in
    `_create_trip` compiles and behaves identically. If this fails, the migration
    is not additive.
    """
    fp = _snapshot(trip)
    fp.refresh_from_db()

    assert fp.total_fare == Decimal('120.00')
    assert fp.snapshot_type == FarePricing.SNAPSHOT_QUOTE
    assert fp.version == 1
    assert fp.fare_basis == FarePricing.BASIS_ESTIMATE_LEGACY
    assert fp.finalized_at is None
    assert fp.is_final is False
    # The money columns are UNSET, not zero. A quote that never recorded a
    # gross/payable split must not claim it recorded one.
    assert fp.gross_fare is None
    assert fp.rider_payable is None
    assert fp.discount_amount is None


@pytest.mark.django_db
def test_receipts_still_read_the_latest_snapshot(trip):
    """`receipts.py` orders by -id and takes the first. A final snapshot written
    later must therefore be picked up with no change to the receipt code."""
    _snapshot(trip, total_fare=Decimal('120.00'))
    final = _snapshot(
        trip, total_fare=Decimal('131.00'), version=2,
        snapshot_type=FarePricing.SNAPSHOT_FINAL, finalized_at=timezone.now(),
    )

    latest = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
    assert latest.id == final.id
    assert latest.total_fare == Decimal('131.00')


# ---------------------------------------------------------------------------
# Immutability of the final snapshot
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_only_one_final_snapshot_per_trip(trip):
    """Enforced by the database, not by the one service that writes it.

    Trip completion has retries; a second finalisation must be refused by
    PostgreSQL rather than by remembering to check.
    """
    _snapshot(trip, snapshot_type=FarePricing.SNAPSHOT_FINAL,
              finalized_at=timezone.now(), version=2)

    with pytest.raises(IntegrityError), transaction.atomic():
        _snapshot(trip, snapshot_type=FarePricing.SNAPSHOT_FINAL,
                  finalized_at=timezone.now(), version=3)


@pytest.mark.django_db
def test_many_quote_snapshots_are_allowed(trip):
    """The constraint is partial: only FINAL is unique per trip.

    Re-quoting during booking, or a corrected quote, must not be blocked.
    """
    _snapshot(trip)
    _snapshot(trip, version=2)
    assert FarePricing.objects.filter(trip_id=trip).count() == 2


@pytest.mark.django_db
def test_a_negative_discount_is_refused(trip):
    """A discount that increases the fare is not a discount."""
    with pytest.raises(IntegrityError), transaction.atomic():
        _snapshot(trip, discount_amount=Decimal('-1.00'))


# ---------------------------------------------------------------------------
# The facts that make a historical fare explainable
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_final_snapshot_explains_itself_without_a_rate_card(trip):
    """The point of the whole exercise: read one row, explain the fare.

    Includes the two cases where the components do NOT sum to the total, which
    is why the flags exist.
    """
    fp = _snapshot(
        trip,
        snapshot_type=FarePricing.SNAPSHOT_FINAL, version=2,
        finalized_at=timezone.now(), fare_basis=FarePricing.BASIS_METERED,
        quoted_distance_km=Decimal('5.00'), quoted_duration_min=Decimal('15.00'),
        actual_distance_km=Decimal('6.40'), actual_duration_min=Decimal('19.00'),
        min_fare_applied=True, surge_cap_applied=True,
        night_surge_applied=True, night_surge_multiplier=Decimal('1.25'),
        rate_card_version=7, zone_code='HYD', vehicle_type='sedan',
        pricing_source='db',
        gross_fare=Decimal('180.00'), discount_amount=Decimal('30.00'),
        rider_payable=Decimal('150.00'),
    )
    fp.refresh_from_db()

    assert fp.is_final is True
    # Rate identity is recorded as values, so a later RateCard edit cannot
    # rewrite what this trip was priced under.
    assert (fp.rate_card_version, fp.zone_code, fp.vehicle_type, fp.pricing_source) \
        == (7, 'HYD', 'sedan', 'db')
    # The flags explain why base+distance+time != total.
    assert fp.min_fare_applied and fp.surge_cap_applied
    # The dynamic surge component is derivable rather than stored twice.
    assert fp.night_surge_multiplier == Decimal('1.25')
    # The three amounts hold the identity the money chain depends on.
    assert fp.gross_fare - fp.discount_amount == fp.rider_payable


@pytest.mark.django_db
def test_actual_quantities_on_the_snapshot_survive_a_later_trip_revision(trip):
    """Deliberately duplicated from Trip, and this is why.

    `record_actuals(force=True)` can revise the trip's measured distance. A fare
    must keep the quantities it was actually computed from, or it stops being an
    explanation of itself.
    """
    fp = _snapshot(
        trip, snapshot_type=FarePricing.SNAPSHOT_FINAL, version=2,
        finalized_at=timezone.now(),
        actual_distance_km=Decimal('6.40'), actual_duration_min=Decimal('19.00'),
    )

    Trip.objects.filter(id=trip.id).update(
        actual_distance_km=Decimal('9.90'), actual_duration_min=Decimal('31.00'),
    )

    fp.refresh_from_db()
    assert fp.actual_distance_km == Decimal('6.40')
    assert Trip.objects.get(id=trip.id).actual_distance_km == Decimal('9.90')


@pytest.mark.django_db
def test_the_default_basis_describes_every_historical_trip(trip):
    """Existing rows migrate to `estimate_legacy`, which is what they are.

    Backfilling them as `metered` would claim a measurement that never happened.
    """
    fp = _snapshot(trip)
    assert fp.fare_basis == FarePricing.BASIS_ESTIMATE_LEGACY


# ---------------------------------------------------------------------------
# Still observation only
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_writing_a_final_snapshot_does_not_touch_the_trip_or_bill_anyone(trip):
    """The schema is inert. Only a later reviewed change makes anything read it."""
    before = Trip.objects.filter(id=trip.id).values(
        'estimated_fare', 'final_fare', 'payment_status').first()

    _snapshot(trip, snapshot_type=FarePricing.SNAPSHOT_FINAL, version=2,
              finalized_at=timezone.now(), rider_payable=Decimal('150.00'))

    after = Trip.objects.filter(id=trip.id).values(
        'estimated_fare', 'final_fare', 'payment_status').first()
    assert after == before
    assert Trip.objects.get(id=trip.id).final_fare is None


# ---------------------------------------------------------------------------
# The constraints, against real PostgreSQL
# ---------------------------------------------------------------------------
#
# The two constraints that carry the guarantees are a PARTIAL unique index and a
# CHECK. SQLite's behaviour around both is close enough to mislead and different
# enough to matter, so the versions above (which run on SQLite in the default
# suite) prove the policy while these prove the constraint. The difference is the
# whole point of having both.

@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_postgres_itself_refuses_a_second_final_snapshot(trip):
    """Enforced by the database, not by the one service that writes finals.

    A management command, a data migration or a second worker bypasses application
    logic. "At most one final fare per trip" is the sort of claim that has to be
    true even then, because the alternative is two different answers to "what was
    this rider charged".
    """
    from django.db import IntegrityError, transaction

    FarePricing.objects.create(
        trip_id=trip, base_fare=Decimal('30.00'),
        distance_fare=Decimal('17.40'), time_fare=Decimal('2.00'),
        total_fare=Decimal('120.00'),
        snapshot_type=FarePricing.SNAPSHOT_FINAL,
        finalized_at=timezone.now(),
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        FarePricing.objects.create(
            trip_id=trip, base_fare=Decimal('30.00'),
        distance_fare=Decimal('17.40'), time_fare=Decimal('2.00'),
        total_fare=Decimal('999.00'),
            snapshot_type=FarePricing.SNAPSHOT_FINAL,
            finalized_at=timezone.now(),
        )

    assert FarePricing.objects.filter(
        trip_id=trip, snapshot_type=FarePricing.SNAPSHOT_FINAL).count() == 1


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_postgres_allows_many_quotes_beside_one_final(trip):
    """The partial index must not accidentally cap quote snapshots.

    A rider who changes vehicle type three times produces three quotes, and the
    index is conditioned on `snapshot_type='final'` precisely so that stays legal.
    """
    from django.db import transaction

    with transaction.atomic():
        for i in range(3):
            FarePricing.objects.create(
                trip_id=trip, base_fare=Decimal('30.00'),
                distance_fare=Decimal('17.40'), time_fare=Decimal('2.00'),
                total_fare=Decimal(f'1{i}0.00'),
                snapshot_type=FarePricing.SNAPSHOT_QUOTE, version=i + 1,
            )
        FarePricing.objects.create(
            trip_id=trip, base_fare=Decimal('30.00'),
            distance_fare=Decimal('17.40'), time_fare=Decimal('2.00'),
            total_fare=Decimal('140.00'),
            snapshot_type=FarePricing.SNAPSHOT_FINAL,
            finalized_at=timezone.now(),
        )

    assert FarePricing.objects.filter(trip_id=trip).count() == 4


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_postgres_refuses_a_negative_discount(trip):
    """A negative discount is a surcharge wearing a discount's name."""
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        FarePricing.objects.create(
            trip_id=trip, base_fare=Decimal('30.00'),
            distance_fare=Decimal('17.40'), time_fare=Decimal('2.00'),
            total_fare=Decimal('120.00'),
            discount_amount=Decimal('-5.00'),
        )


# ---------------------------------------------------------------------------
# Telemetry provenance: why a fare basis was chosen
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_metered_basis_can_record_the_telemetry_that_justifies_it(trip):
    """`metered_degraded` must be evidence, not an assertion.

    The five-ride QA rehearsal established that coverage_ratio alone cannot carry
    this: trip 13 sent four pings forty seconds apart and scored 0.75, better than
    trip 16 which sent seven, because coverage measures span rather than density.
    So the point count and the largest gap are stored alongside it -- those are what
    reveal a hole in the middle of a journey.
    """
    snap = FarePricing.objects.create(
        trip_id=trip, base_fare=Decimal('30.00'),
        distance_fare=Decimal('17.40'), time_fare=Decimal('2.00'),
        total_fare=Decimal('118.83'),
        snapshot_type=FarePricing.SNAPSHOT_FINAL,
        finalized_at=timezone.now(),
        fare_basis=FarePricing.BASIS_METERED_DEGRADED,
        actual_distance_km=Decimal('1.67'), actual_duration_min=Decimal('2.67'),
        trail_points=7,
        trail_coverage_ratio=Decimal('0.7500'),
        trail_max_gap_seconds=40,
    )
    snap.refresh_from_db()

    assert snap.fare_basis == FarePricing.BASIS_METERED_DEGRADED
    assert snap.trail_points == 7
    assert snap.trail_coverage_ratio == Decimal('0.7500')
    assert snap.trail_max_gap_seconds == 40
    # The three together are what make the basis reviewable: a 0.75 coverage with
    # only 7 points and a 40-second hole is a different story from 0.75 with 200.
    assert snap.trail_points is not None and snap.trail_max_gap_seconds is not None


@pytest.mark.django_db
def test_a_quote_snapshot_has_no_telemetry_because_there_is_no_trail_yet(trip):
    """Null, not zero. At booking the journey has not happened."""
    snap = FarePricing.objects.create(
        trip_id=trip, base_fare=Decimal('30.00'),
        distance_fare=Decimal('17.40'), time_fare=Decimal('2.00'),
        total_fare=Decimal('120.00'),
        snapshot_type=FarePricing.SNAPSHOT_QUOTE,
        quoted_distance_km=Decimal('1.45'), quoted_duration_min=Decimal('1.00'),
    )
    snap.refresh_from_db()

    assert snap.trail_points is None, (
        'zero points and "no trail yet" are different facts and must not collapse'
    )
    assert snap.trail_coverage_ratio is None
    assert snap.trail_max_gap_seconds is None
    assert snap.finalized_at is None
    assert snap.is_final is False


@pytest.mark.django_db
def test_the_snapshot_still_writes_with_no_telemetry_columns_supplied(trip):
    """Additive, so existing writers are unaffected.

    `_create_payment_on_complete`, the receipt task and the booking path all write
    FarePricing without knowing these columns exist.
    """
    snap = FarePricing.objects.create(
        trip_id=trip, base_fare=Decimal('30.00'),
        distance_fare=Decimal('17.40'), time_fare=Decimal('2.00'),
        total_fare=Decimal('120.00'),
    )
    assert snap.pk is not None
    assert snap.snapshot_type == FarePricing.SNAPSHOT_QUOTE
    assert snap.fare_basis == FarePricing.BASIS_ESTIMATE_LEGACY
