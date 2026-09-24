"""The classification must be honest about what it cannot reconstruct.

Backfilling `TripSettlement` for historical trips is only defensible where the
economics are actually knowable. A financial table that silently mixes recorded facts
with guesses is worse than one with a gap that says so, so this command's job is to
find that boundary BEFORE anything is written.

The tests that matter most are the negative ones: a completed trip with no evidence
that money moved must land in a DO-NOT-BACKFILL bucket rather than being quietly
counted as reconstructable. Inventing a settlement row for it would assert that a
driver was paid when nobody knows whether they were.
"""

from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.payments.models import TransactionHistory, TripSettlement
from servers.ride.models import Trip, TripStatus
from servers.rider.models import WalletTransaction

User = get_user_model()

LAT, LNG = 17.4450000, 78.3800000


def _trip(suffix, status='completed', fare='200.00', method='cash'):
    from django.utils import timezone

    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    duser = User.objects.create_user(
        phone_number=f'+9198900{suffix:05d}', role='driver')
    driver = Driver.objects.create(user_id=duser, approved=True, status='active')
    Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt,
                           vehicle_number=f'TS09CL{suffix:04d}')
    rider = User.objects.create_user(
        phone_number=f'+9197900{suffix:05d}', role='rider')
    st, _ = TripStatus.objects.get_or_create(status_code=status)
    return Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=st,
        pickup_lat=Decimal(str(LAT)), pickup_long=Decimal(str(LNG)),
        destination_lat=Decimal(str(LAT + 0.01)), destination_long=Decimal(str(LNG)),
        estimated_fare=None if fare is None else Decimal(fare),
        payment_method=method, completed_at=timezone.now(),
    )


def _keyed_ledger(trip, suffix=''):
    return WalletTransaction.objects.create(
        user_id=trip.driver_id.user_id, amount=Decimal('36.00'),
        txn_type='debit', status='completed', purpose='trip_commission',
        reference_id=f'TRIP_{trip.id}',
        idempotency_key=f'TRIP_{trip.id}_EARNING{suffix}',
    )


def _run(**kwargs):
    out = StringIO()
    call_command('classify_historical_settlements', stdout=out, **kwargs)
    return out.getvalue()


def _count(output, letter):
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == letter:
            return int(parts[1])
    raise AssertionError(f'category {letter} not found in:\n{output}')


@pytest.mark.django_db
def test_a_trip_with_a_keyed_ledger_row_is_backfillable():
    trip = _trip(1)
    _keyed_ledger(trip)
    out = _run()
    assert _count(out, 'A') == 1, out
    assert _count(out, 'C') == 0


@pytest.mark.django_db
def test_a_trip_with_only_a_history_row_is_reconstructable():
    """The amounts were recorded even without the keyed ledger row."""
    trip = _trip(2)
    TransactionHistory.objects.create(
        trip_id=trip, user_id=trip.user_id, driver_id=trip.driver_id,
        amount=Decimal('36.00'), method='cash', status='completed',
        txn_type='debit', user_name='qa',
    )
    out = _run()
    assert _count(out, 'B') == 1, out
    assert _count(out, 'A') == 0


@pytest.mark.django_db
def test_a_completed_trip_with_no_money_evidence_is_not_backfillable():
    """The case that matters.

    A completed trip with a fare but nothing recording that money moved must NOT be
    counted as reconstructable. Whether the driver was ever settled is unknown, and
    writing a settlement row would assert that they were.
    """
    _trip(3)
    out = _run()
    assert _count(out, 'C') == 1, out
    assert _count(out, 'A') == 0
    assert _count(out, 'B') == 0
    assert 'DO NOT backfill' in out


@pytest.mark.django_db
def test_a_trip_with_no_fare_has_nothing_to_reconstruct():
    _trip(4, fare=None)
    out = _run()
    assert _count(out, 'E') == 1, out


@pytest.mark.django_db
def test_incomplete_trips_are_ignored_entirely():
    """Only completed trips have economics to reconstruct."""
    _trip(6, status='cancelled')
    _trip(7, status='in_progress')
    out = _run()
    assert 'No completed trips found.' in out, out


@pytest.mark.django_db
def test_a_trip_that_already_has_a_settlement_is_reported_separately():
    """So a re-run after a partial backfill is not misread as work still to do."""
    from django.utils import timezone

    trip = _trip(8)
    txn = _keyed_ledger(trip)
    TripSettlement.objects.create(
        trip=trip, driver=trip.driver_id, wallet_transaction=txn,
        gross_fare=Decimal('200.00'), commission_percent=Decimal('18.00'),
        commission_amount=Decimal('36.00'), driver_net=Decimal('164.00'),
        payment_method='cash', settled_at=timezone.now(),
    )
    out = _run()
    assert 'Already have a settlement: 1' in out, out


@pytest.mark.django_db
def test_the_command_writes_nothing():
    """It is documented as safe to run against production, so that must be true."""
    trip = _trip(9)
    _keyed_ledger(trip)
    snapshot = {
        'settlements': TripSettlement.objects.count(),
        'ledger': WalletTransaction.objects.count(),
        'history': TransactionHistory.objects.count(),
        'trips': Trip.objects.count(),
    }
    _run(sample=3)
    after = {
        'settlements': TripSettlement.objects.count(),
        'ledger': WalletTransaction.objects.count(),
        'history': TransactionHistory.objects.count(),
        'trips': Trip.objects.count(),
    }
    assert snapshot == after, f'the command mutated data: {snapshot} -> {after}'


@pytest.mark.django_db
def test_samples_are_ids_only_and_leak_no_pii():
    """Spot-checking must not print phone numbers or amounts.

    This output is the sort of thing that gets pasted into a chat window.
    """
    trip = _trip(10)
    _keyed_ledger(trip)
    out = _run(sample=5)

    assert str(trip.id) in out
    assert '+9198900' not in out, 'a driver phone number reached the output'
    assert '+9197900' not in out, 'a rider phone number reached the output'
    assert '200.00' not in out, 'a fare amount reached the output'


@pytest.mark.django_db
def test_the_output_states_that_a_backfill_must_be_marked_reconstructed():
    """The instruction has to travel with the numbers, not live only in a doc."""
    trip = _trip(11)
    _keyed_ledger(trip)
    out = _run()
    assert 'source=reconstructed' in out
    assert 'DERIVED' in out, (
        'the output must say the commission rate would be derived rather than the '
        'rate that was applied'
    )
