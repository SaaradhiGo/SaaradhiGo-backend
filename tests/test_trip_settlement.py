"""TripSettlement explains the money. It must never become a second source of it.

`WalletTransaction` stays authoritative: it says what moved. `TripSettlement` says
why — gross, rate as applied, commission, net, method, timestamp — so a dispute can
be answered from one row instead of recomputed from today's rate card.

The properties that matter, and why each is asserted here:

  * **one settlement per trip**, enforced by PostgreSQL rather than by the one
    function that writes it, because a management command, a data migration or a
    second worker all bypass application logic;
  * **it shares the ledger's idempotency boundary**, so a retried completion cannot
    produce a second settlement — the `TRIP_<id>_EARNING` key refuses the ledger row
    first and the settlement is never reached;
  * **gross − commission = net**, asserted at write time rather than trusted;
  * **a later RateCard change cannot alter it**, which is the entire point;
  * **it creates no money**, so wallet balances are identical with and without it.

Concurrency is tested with real threads against real PostgreSQL. Two workers
settling the same trip is the case a single-threaded test cannot reach and the one
that actually happens when a webhook and a completion race.
"""

import threading
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.payments.models import TripSettlement
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Wallet, WalletTransaction

User = get_user_model()

LAT, LNG = 17.4450000, 78.3800000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_settled_trip(suffix, payment_method='cash', fare='200.00'):
    """A completed trip ready to be settled, with its driver wallet in place."""
    from django.utils import timezone

    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    duser = User.objects.create_user(
        phone_number=f'+9198700{suffix:05d}', role='driver')
    driver = Driver.objects.create(user_id=duser, approved=True, status='active')
    Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt,
                           vehicle_number=f'TS09ST{suffix:04d}')
    rider = User.objects.create_user(
        phone_number=f'+9197700{suffix:05d}', role='rider')

    st, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=st,
        pickup_lat=Decimal(str(LAT)), pickup_long=Decimal(str(LNG)),
        destination_lat=Decimal(str(LAT + 0.02)), destination_long=Decimal(str(LNG)),
        estimated_fare=Decimal(fare), payment_method=payment_method,
        completed_at=timezone.now(),
    )
    Wallet.objects.get_or_create(
        user_id=duser, scope=Wallet.SCOPE_DRIVER,
        defaults={'balance': Decimal('0.00')},
    )
    return trip


def _settle(trip):
    from servers.driver.utils import credit_driver_wallet
    return credit_driver_wallet(trip)


def _driver_balance(trip):
    from servers.rider.models import get_wallet
    return Decimal(str(get_wallet(trip.driver_id.user_id,
                                  Wallet.SCOPE_DRIVER).balance))


# ---------------------------------------------------------------------------
# It is written, once, and it adds up
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_settling_a_trip_writes_one_settlement_that_explains_itself():
    trip = _make_settled_trip(1, payment_method='cash', fare='200.00')
    _settle(trip)

    settlements = list(TripSettlement.objects.filter(trip=trip))
    assert len(settlements) == 1, settlements
    s = settlements[0]

    assert s.gross_fare == Decimal('200.00')
    assert s.commission_percent > 0, (
        'a zero rate would mean the platform took nothing, which was a real '
        'historical defect and must not be reintroduced silently'
    )
    assert s.driver_net == s.gross_fare - s.commission_amount, (
        'gross - commission = net is the identity this row exists to record'
    )
    assert s.payment_method == 'cash'
    assert s.settled_at is not None
    assert s.version == 1
    assert s.source == TripSettlement.SOURCE_NATIVE
    assert s.is_reconstructed is False
    # The link to the money that actually moved is what makes this an explanation
    # rather than a duplicate.
    assert s.wallet_transaction is not None
    assert s.wallet_transaction.idempotency_key == f'TRIP_{trip.id}_EARNING'


@pytest.mark.django_db
@pytest.mark.parametrize('method', ['cash', 'online', 'wallet'])
def test_the_identity_holds_for_every_payment_method(method):
    """The ledger's direction changes with the method; the economics do not."""
    trip = _make_settled_trip(abs(hash(method)) % 900 + 10,
                              payment_method=method, fare='300.00')
    _settle(trip)

    s = TripSettlement.objects.get(trip=trip)
    assert s.gross_fare == Decimal('300.00')
    assert s.driver_net == s.gross_fare - s.commission_amount
    assert s.payment_method == method
    # Cash means the driver holds the fare and owes commission; online/wallet means
    # the platform holds it and owes the net. Either way gross and net are the same
    # facts about the ride.
    txn = s.wallet_transaction
    assert txn is not None
    if method == 'cash':
        assert txn.txn_type == 'debit'
        assert Decimal(str(txn.amount)) == s.commission_amount
    else:
        assert txn.txn_type == 'credit'
        assert Decimal(str(txn.amount)) == s.driver_net


@pytest.mark.django_db
def test_a_retried_settlement_creates_no_second_settlement_and_no_second_credit():
    """The property the whole design rests on.

    The settlement has no idempotency logic of its own. It relies on the ledger's
    existing `TRIP_<id>_EARNING` key refusing the duplicate first, so a second call
    returns before the settlement is ever reached. If that reasoning is wrong, this
    test is where it shows.
    """
    trip = _make_settled_trip(2, payment_method='online', fare='250.00')

    _settle(trip)
    balance_after_first = _driver_balance(trip)
    first = TripSettlement.objects.get(trip=trip)

    for _ in range(5):
        _settle(trip)

    assert TripSettlement.objects.filter(trip=trip).count() == 1, (
        'a retried settlement created another settlement row')
    assert WalletTransaction.objects.filter(
        idempotency_key=f'TRIP_{trip.id}_EARNING').count() == 1
    assert _driver_balance(trip) == balance_after_first, (
        'a retried settlement moved money a second time')
    assert TripSettlement.objects.get(trip=trip).pk == first.pk


@pytest.mark.django_db
def test_the_settlement_creates_no_money_of_its_own():
    """Adding this table must not change a single balance.

    Compared against the arithmetic rather than against a hardcoded number, so the
    test still means something if the commission rate changes.
    """
    trip = _make_settled_trip(3, payment_method='online', fare='400.00')
    before = _driver_balance(trip)

    _settle(trip)

    s = TripSettlement.objects.get(trip=trip)
    after = _driver_balance(trip)
    assert after == before + s.driver_net, (
        f'balance moved by {after - before}, settlement says net is {s.driver_net}'
    )


# ---------------------------------------------------------------------------
# A later pricing change cannot rewrite history
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_later_ratecard_change_cannot_alter_a_settled_trip():
    """The reason this table exists.

    `admin_dashboard` computes commission at render time, so editing a rate card
    today silently changes last month's reported revenue. A settlement stores the
    rate AS APPLIED, so it cannot move.
    """
    from servers.pricing.models import RateCard

    trip = _make_settled_trip(4, payment_method='cash', fare='500.00')
    _settle(trip)

    s = TripSettlement.objects.get(trip=trip)
    recorded = {
        'percent': s.commission_percent,
        'amount': s.commission_amount,
        'net': s.driver_net,
    }

    # Supersede every live card with a materially different commission.
    live = list(RateCard.objects.filter(is_active=True, effective_to__isnull=True))
    for card in live:
        card.new_version(commission_percent=Decimal('40.00'))

    s.refresh_from_db()
    assert s.commission_percent == recorded['percent'], (
        'the settled commission rate followed a later rate-card change')
    assert s.commission_amount == recorded['amount']
    assert s.driver_net == recorded['net']
    assert s.driver_net == s.gross_fare - s.commission_amount


# ---------------------------------------------------------------------------
# The database is the backstop
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_postgres_refuses_a_second_original_settlement():
    """Independent of the function that writes settlements.

    A data migration or a management command does not go through
    `credit_driver_wallet`, and "this ride was settled once" has to survive that.
    """
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    trip = _make_settled_trip(5, payment_method='cash', fare='200.00')
    _settle(trip)

    with pytest.raises(IntegrityError), transaction.atomic():
        TripSettlement.objects.create(
            trip=trip, driver=trip.driver_id,
            gross_fare=Decimal('200.00'), commission_percent=Decimal('18.00'),
            commission_amount=Decimal('36.00'), driver_net=Decimal('164.00'),
            payment_method='cash', settled_at=timezone.now(),
        )

    assert TripSettlement.objects.filter(trip=trip, version=1).count() == 1


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_a_correction_is_a_new_version_not_an_edit():
    """Corrections stay possible; the original stays intact.

    This is why `trip` is a ForeignKey with a partial unique index at version 1
    rather than the OneToOne the design named -- a OneToOne permits exactly one row,
    so no correction could ever be written.
    """
    from django.db import transaction
    from django.utils import timezone

    trip = _make_settled_trip(6, payment_method='cash', fare='200.00')
    _settle(trip)
    original = TripSettlement.objects.get(trip=trip, version=1)

    with transaction.atomic():
        TripSettlement.objects.create(
            trip=trip, driver=trip.driver_id,
            wallet_transaction=original.wallet_transaction,
            gross_fare=original.gross_fare,
            commission_percent=Decimal('15.00'),
            commission_amount=Decimal('30.00'),
            driver_net=original.gross_fare - Decimal('30.00'),
            payment_method='cash', settled_at=timezone.now(),
            version=2,
            provenance_note='Commission rate misapplied at settlement time.',
        )

    rows = list(TripSettlement.objects.filter(trip=trip).order_by('version'))
    assert len(rows) == 2
    original.refresh_from_db()
    assert original.commission_percent != rows[1].commission_percent, (
        'the correction overwrote the original instead of superseding it')
    # The current settlement is the highest version.
    assert rows[-1].version == 2
    assert rows[-1].provenance_note


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_negative_commission_and_gross_are_refused_by_the_database():
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    trip = _make_settled_trip(7, payment_method='cash', fare='200.00')

    for field, value in (('commission_amount', Decimal('-1.00')),
                         ('gross_fare', Decimal('-1.00'))):
        kwargs = dict(
            trip=trip, driver=trip.driver_id,
            gross_fare=Decimal('200.00'), commission_percent=Decimal('18.00'),
            commission_amount=Decimal('36.00'), driver_net=Decimal('164.00'),
            payment_method='cash', settled_at=timezone.now(),
        )
        kwargs[field] = value
        with pytest.raises(IntegrityError), transaction.atomic():
            TripSettlement.objects.create(**kwargs)


# ---------------------------------------------------------------------------
# Concurrency, with real threads against real PostgreSQL
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_settlements_of_one_trip_produce_one_settlement():
    """A webhook and a completion racing is not hypothetical.

    Both threads read a zero balance, both try to insert the ledger row, one loses
    on the idempotency key and returns. Exactly one settlement, exactly one credit,
    and a balance that moved once.
    """
    from django.db import connections

    trip = _make_settled_trip(8, payment_method='online', fare='600.00')
    errors = []
    barrier = threading.Barrier(4)

    def worker():
        try:
            barrier.wait(timeout=20)
            _settle(trip)
        except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
            errors.append(repr(exc))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    settlements = TripSettlement.objects.filter(trip=trip)
    ledger = WalletTransaction.objects.filter(
        idempotency_key=f'TRIP_{trip.id}_EARNING')
    balance = _driver_balance(trip)

    print(f'concurrent settle: settlements={settlements.count()} '
          f'ledger={ledger.count()} balance={balance} errors={errors}')

    assert settlements.count() == 1, (
        f'{settlements.count()} settlements for one trip under concurrency')
    assert ledger.count() == 1, (
        f'{ledger.count()} ledger rows -- the driver was credited more than once')
    s = settlements.first()
    assert balance == s.driver_net, (
        f'balance {balance} does not match the single settlement net {s.driver_net}')


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_settlements_of_different_trips_all_succeed():
    """The guard must be per trip, not a global lock.

    A constraint that serialised every settlement on the platform would be a
    different bug.
    """
    from django.db import connections

    trips = [_make_settled_trip(20 + i, payment_method='online', fare='100.00')
             for i in range(4)]
    errors = []
    barrier = threading.Barrier(len(trips))

    def worker(trip):
        try:
            barrier.wait(timeout=20)
            _settle(trip)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(t,)) for t in trips]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    print(f'independent settles: {TripSettlement.objects.count()} '
          f'settlements, errors={errors}')
    assert not errors, errors
    for trip in trips:
        assert TripSettlement.objects.filter(trip=trip).count() == 1
