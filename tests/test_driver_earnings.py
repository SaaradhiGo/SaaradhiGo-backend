"""Driver earnings: the cash driver who saw ₹0.

A driver who only takes cash rides had completed trips, had commission deducted
from their settlement balance, and was shown zero earnings and zero trips. That
is the kind of defect that ends a pilot, and it was a read bug: cash settlement is
recorded as a commission DEBIT, and both earnings endpoints filtered
`txn_type='credit'`.

These tests pin the corrected reading against the ledger `credit_driver_wallet`
actually writes, and they pin the thing that makes the fix safe: it writes
nothing. Settlement history, wallet balances and commission are untouched, so a
wrong report can be corrected again later without a data migration.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.earnings import (
    EarningRow, settlement_rows, summarise,
)
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.driver.utils import credit_driver_wallet
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Wallet, WalletTransaction, get_wallet

User = get_user_model()

LAT = Decimal('17.4450000')
LNG = Decimal('78.3800000')


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def vehicle_type(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return vt


@pytest.fixture
def rider(db):
    return User.objects.create_user(phone_number='+919700000301', role='rider',
                                    full_name='Test Rider')


@pytest.fixture
def driver(db, vehicle_type):
    u = User.objects.create_user(phone_number='+919800000301', role='driver')
    d = Driver.objects.create(user_id=u, approved=True)
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vehicle_type,
                           vehicle_number='TS09GP0301')
    return d


def _completed_trip(rider, driver, fare, payment_method):
    t = Trip.objects.create(
        user_id=rider, status_id=_status('completed'),
        pickup_lat=LAT, pickup_long=LNG,
        destination_lat=LAT, destination_long=LNG,
        estimated_fare=Decimal(str(fare)),
        payment_method=payment_method,
    )
    t.driver_id = driver
    t.completed_at = timezone.now()
    t.save(update_fields=['driver_id', 'completed_at'])
    return t


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_cash_only_driver_sees_their_trips_and_earnings(rider, driver):
    """The reported bug, as a test. Three cash rides, none of them visible.

    `credit_driver_wallet` writes a commission DEBIT for a cash trip, because the
    driver already holds the fare. Reading only credits therefore returned
    nothing at all -- not a wrong total, an empty history.
    """
    for fare in (100, 200, 300):
        credit_driver_wallet(_completed_trip(rider, driver, fare, 'cash'))

    # The old read: credits only. It finds nothing, which is the bug.
    assert WalletTransaction.objects.filter(
        user_id=driver.user_id, txn_type='credit',
    ).count() == 0

    rows = settlement_rows(driver)
    assert len(rows) == 3, 'every cash trip must appear'

    totals = summarise(rows)
    assert totals['total_trips'] == 3
    assert totals['total_gross'] == Decimal('600.00')
    assert totals['total_commission'] > 0
    assert totals['total_net'] == Decimal('600.00') - totals['total_commission']
    assert totals['cash_collected'] == Decimal('600.00')


@pytest.mark.django_db
def test_an_online_drivers_commission_is_no_longer_deducted_twice(rider, driver):
    """The credit row holds the NET, so summing credits into a gross field and
    then subtracting a commission from it charges the driver twice.

    The driver app computes `netEarned = total_earned - total_commission`, so
    `total_earned` has to be the gross fare for that subtraction to be right.
    """
    trip = _completed_trip(rider, driver, 500, 'online')
    credit_driver_wallet(trip)

    credit = WalletTransaction.objects.get(user_id=driver.user_id,
                                           purpose='trip_earnings')
    assert credit.amount < Decimal('500.00'), 'the ledger row is the net, not the fare'

    totals = summarise(settlement_rows(driver))
    assert totals['total_gross'] == Decimal('500.00')
    assert totals['total_net'] == credit.amount
    assert totals['total_commission'] == Decimal('500.00') - credit.amount
    # net = gross - commission, once.
    assert totals['total_gross'] - totals['total_commission'] == totals['total_net']


@pytest.mark.django_db
def test_cash_and_online_trips_are_comparable_in_one_list(rider, driver):
    """Same definition of net for both, or a mixed-method driver cannot read it."""
    credit_driver_wallet(_completed_trip(rider, driver, 100, 'cash'))
    credit_driver_wallet(_completed_trip(rider, driver, 100, 'online'))

    rows = settlement_rows(driver)
    assert len(rows) == 2
    cash = next(r for r in rows if r.driver_holds_cash)
    online = next(r for r in rows if not r.driver_holds_cash)

    assert cash.gross == online.gross == Decimal('100.00')
    assert cash.commission == online.commission
    assert cash.net == online.net, 'identical fare, identical earning'
    assert cash.method == 'cash'
    assert online.method == 'online'


@pytest.mark.django_db
def test_the_commission_rate_comes_from_the_ledger_not_a_literal(rider, driver):
    """The old summary hardcoded 20%.

    The reported rate is now derived from what was actually charged, so it cannot
    drift from settlement -- and a trip settled under an old rate keeps reporting
    that old rate.
    """
    import servers.driver.views as driver_views

    source = open(driver_views.__file__, encoding='utf-8').read()
    assert 'commission_percent = 20' not in source

    trip = _completed_trip(rider, driver, 1000, 'online')
    credit_driver_wallet(trip)
    row = WalletTransaction.objects.get(user_id=driver.user_id, purpose='trip_earnings')

    totals = summarise(settlement_rows(driver))
    expected_percent = (
        (Decimal('1000.00') - row.amount) / Decimal('1000.00') * Decimal('100')
    ).quantize(Decimal('0.01'))
    assert totals['effective_commission_percent'] == expected_percent


# ---------------------------------------------------------------------------
# What must NOT be counted as a trip
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_withdrawals_are_not_counted_as_trips(rider, driver):
    """A driver's wallet also holds payouts and withdrawal refunds.

    Filtering on `txn_type='debit'` alone would have turned every withdrawal into
    a phantom cash ride, which is why the filter is on `purpose` instead.
    """
    credit_driver_wallet(_completed_trip(rider, driver, 100, 'cash'))

    for purpose in ('withdrawal', 'payout', 'refund_failed_withdrawal',
                    'refund_rejected_withdrawal'):
        WalletTransaction.objects.create(
            user_id=driver.user_id, amount=Decimal('50.00'), txn_type='debit',
            status='completed', purpose=purpose,
            idempotency_key=f'WD_{purpose}',
        )

    rows = settlement_rows(driver)
    assert len(rows) == 1, [r.method for r in rows]
    assert summarise(rows)['total_trips'] == 1


@pytest.mark.django_db
def test_a_pending_settlement_row_is_not_counted(rider, driver):
    """Only completed settlement is an earning."""
    WalletTransaction.objects.create(
        user_id=driver.user_id, amount=Decimal('80.00'), txn_type='credit',
        status='pending', purpose='trip_earnings', reference_id='TRIP_999',
        idempotency_key='TRIP_999_EARNING',
    )
    assert settlement_rows(driver) == []


@pytest.mark.django_db
def test_another_drivers_settlement_is_never_visible(rider, driver, vehicle_type):
    """Earnings are per driver; the query must be scoped to the caller."""
    other_user = User.objects.create_user(phone_number='+919800000399', role='driver')
    other = Driver.objects.create(user_id=other_user, approved=True)
    Vehicle.objects.create(driver_id=other, vehicle_type_id=vehicle_type,
                           vehicle_number='TS09GP0399')

    credit_driver_wallet(_completed_trip(rider, other, 400, 'cash'))
    assert settlement_rows(driver) == []
    assert len(settlement_rows(other)) == 1


@pytest.mark.django_db
def test_a_settlement_whose_trip_vanished_is_reported_as_unresolved(driver):
    """Counted, not dropped.

    A driver who can see that one row is unresolved trusts the total more than a
    driver whose trip silently disappeared from their history.
    """
    WalletTransaction.objects.create(
        user_id=driver.user_id, amount=Decimal('90.00'), txn_type='credit',
        status='completed', purpose='trip_earnings', reference_id='TRIP_424242',
        idempotency_key='TRIP_424242_EARNING',
    )
    rows = settlement_rows(driver)
    assert len(rows) == 1
    assert rows[0].gross is None

    totals = summarise(rows)
    assert totals['unresolved_trips'] == 1
    assert totals['total_trips'] == 1
    assert totals['total_net'] == Decimal('90.00'), 'what is known is still reported'


# ---------------------------------------------------------------------------
# It is a read
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_reading_earnings_changes_no_settlement_state(rider, driver):
    """The fix must not be able to alter money. That is what makes it safe.

    Snapshots the wallet balance, every ledger row and the trip's fare columns
    across a full read of both endpoints' logic.
    """
    credit_driver_wallet(_completed_trip(rider, driver, 250, 'cash'))
    credit_driver_wallet(_completed_trip(rider, driver, 250, 'online'))

    wallet_before = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER).balance
    ledger_before = list(WalletTransaction.objects.order_by('id').values())
    trips_before = list(Trip.objects.order_by('id').values(
        'id', 'estimated_fare', 'final_fare', 'payment_status',
    ))

    summarise(settlement_rows(driver))

    assert get_wallet(driver.user_id, Wallet.SCOPE_DRIVER).balance == wallet_before
    assert list(WalletTransaction.objects.order_by('id').values()) == ledger_before
    assert list(Trip.objects.order_by('id').values(
        'id', 'estimated_fare', 'final_fare', 'payment_status',
    )) == trips_before


# ---------------------------------------------------------------------------
# The API contract the driver app already parses
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_summary_endpoint_keeps_every_existing_key(rider, driver, client):
    """The shipped driver app parses these names; none may disappear."""
    from rest_framework.test import APIClient

    credit_driver_wallet(_completed_trip(rider, driver, 300, 'cash'))

    api = APIClient()
    api.force_authenticate(user=driver.user_id)
    resp = api.get('/api/v1/driver/earnings/summary/')
    assert resp.status_code == 200, resp.content

    payload = resp.json().get('data', resp.json())
    for key in ('total_earned', 'total_commission', 'total_trips', 'today_earned',
                'today_trips', 'commission_percent', 'wallet_balance'):
        assert key in payload, key

    # The bug, at the API boundary.
    assert Decimal(payload['total_earned']) == Decimal('300.00')
    assert payload['total_trips'] == 1


@pytest.mark.django_db
def test_list_endpoint_returns_a_row_per_cash_trip(rider, driver):
    from rest_framework.test import APIClient

    credit_driver_wallet(_completed_trip(rider, driver, 150, 'cash'))

    api = APIClient()
    api.force_authenticate(user=driver.user_id)
    resp = api.get('/api/v1/driver/earnings/')
    assert resp.status_code == 200, resp.content

    body = resp.json().get('data', resp.json())
    results = body['results']
    assert len(results) == 1
    row = results[0]
    for key in ('id', 'trip_id_val', 'amount', 'commission', 'net_amount',
                'created_at', 'method', 'user_name'):
        assert key in row, key
    assert row['method'] == 'cash'
    assert Decimal(row['amount']) == Decimal('150.00')
    assert Decimal(row['commission']) > 0, 'commission was hardcoded to 0.0 before'
    assert row['cash_in_hand'] is True


@pytest.mark.django_db
def test_earnings_require_a_driver_identity(rider):
    """A rider must not be able to read a driver earnings report."""
    from rest_framework.test import APIClient

    api = APIClient()
    api.force_authenticate(user=rider)
    assert api.get('/api/v1/driver/earnings/summary/').status_code in (401, 403)


# ---------------------------------------------------------------------------
# Row formatting
# ---------------------------------------------------------------------------

def test_a_row_with_no_gross_still_reports_an_amount():
    """The app reads `amount` first; sending null would render a blank row."""
    row = EarningRow(
        ledger_id=1, trip_id=None, gross=None, commission=None,
        net=Decimal('70.00'), method='online', created_at=None,
        rider_name='', driver_holds_cash=False,
    )
    assert row.to_api()['amount'] == '70.00'
