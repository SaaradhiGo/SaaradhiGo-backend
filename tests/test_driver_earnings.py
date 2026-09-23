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


# ---------------------------------------------------------------------------
# Merge review: the definitions are identities, and the endpoints are reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('method', ['cash', 'online'])
@pytest.mark.django_db
def test_net_is_always_gross_minus_commission(rider, driver, method):
    """One definition, both payment methods.

    The ledger represents cash and online settlement differently -- a commission
    debit versus a net credit -- but the reported semantics must not differ, or a
    driver who takes both cannot read their own earnings. GROSS is the rider fare
    for the completed trip, COMMISSION is what settlement actually recorded, and
    NET is the difference. Asserted as an identity rather than against fixed
    numbers, so it holds at whatever commission rate applies.
    """
    credit_driver_wallet(_completed_trip(rider, driver, 400, method))

    rows = settlement_rows(driver)
    assert len(rows) == 1
    row = rows[0]

    assert row.gross == Decimal('400.00')
    assert row.commission > 0
    assert row.net == row.gross - row.commission

    totals = summarise(rows)
    assert totals['total_gross'] == row.gross
    assert totals['total_commission'] == row.commission
    assert totals['total_net'] == totals['total_gross'] - totals['total_commission']

    # And the cash-specific requirement: the driver holds the fare, the debit is
    # the commission, and the net earning is still visible rather than zero.
    if method == 'cash':
        assert row.driver_holds_cash is True
        assert totals['cash_collected'] == Decimal('400.00')
        assert totals['total_net'] > 0, 'a cash driver must see earnings, not zero'
    else:
        assert row.driver_holds_cash is False
        assert totals['cash_collected'] == Decimal('0.00')


@pytest.mark.django_db
def test_reported_commission_does_not_follow_a_later_rate_card_change(rider, driver,
                                                                     vehicle_type):
    """Historical commission must come from the ledger, not from configuration.

    A rate card edited after a trip settled must not rewrite what the driver was
    told they earned. This seeds a deliberately absurd rate AFTER settlement and
    asserts the reported commission does not move -- the test that fails if anyone
    reintroduces `commission_percent_for_trip` into the reporting path.
    """
    from servers.pricing.models import RateCard, ServiceZone

    trip = _completed_trip(rider, driver, 1000, 'online')
    credit_driver_wallet(trip)

    before = summarise(settlement_rows(driver))
    settled_commission = before['total_commission']
    assert settled_commission > 0

    zone, _ = ServiceZone.objects.get_or_create(
        code='EARN-TEST',
        defaults={'name': 'Earnings test', 'zone_type': 'city',
                  'state_code': 'TS', 'city': 'Hyderabad',
                  'polygon_geojson': {
                      'type': 'Polygon',
                      'coordinates': [[[78.0, 17.0], [79.0, 17.0],
                                       [79.0, 18.0], [78.0, 18.0],
                                       [78.0, 17.0]]],
                  }},
    )
    RateCard.objects.create(
        zone=zone, vehicle_type=vehicle_type,
        base_fare=Decimal('1.00'), per_km_fare=Decimal('1.00'),
        per_min_fare=Decimal('1.00'), min_fare=Decimal('1.00'),
        commission_percent=Decimal('91.00'), version=99, is_active=True,
    )

    after = summarise(settlement_rows(driver))
    assert after['total_commission'] == settled_commission, \
        'a later rate card must not rewrite settled commission'
    assert after['effective_commission_percent'] == before['effective_commission_percent']


@pytest.mark.django_db
def test_both_endpoints_leave_every_financial_row_byte_identical(rider, driver):
    """Proven at the API boundary, not only in the service layer.

    Snapshots the driver's wallet balance, every WalletTransaction, every
    TransactionHistory row and every trip's fare columns across real requests to
    both endpoints. This is the assertion that makes the fix safe to merge without
    reviewing settlement: if the presentation is still wrong it can be corrected
    again with no data migration and no adjustment entries.
    """
    from rest_framework.test import APIClient

    from servers.payments.models import TransactionHistory

    credit_driver_wallet(_completed_trip(rider, driver, 250, 'cash'))
    credit_driver_wallet(_completed_trip(rider, driver, 375, 'online'))

    def snapshot():
        return {
            'wallet': Wallet.objects.order_by('id').values_list(
                'id', 'user_id', 'scope', 'balance'),
            'wallet_txns': list(WalletTransaction.objects.order_by('id').values()),
            'history': list(TransactionHistory.objects.order_by('id').values()),
            'trips': list(Trip.objects.order_by('id').values(
                'id', 'estimated_fare', 'final_fare', 'actual_distance_km',
                'actual_duration_min', 'payment_status', 'payment_method',
                'surge_multiplier')),
        }

    before = snapshot()
    before['wallet'] = list(before['wallet'])

    api = APIClient()
    api.force_authenticate(user=driver.user_id)
    assert api.get('/api/v1/driver/earnings/').status_code == 200
    assert api.get('/api/v1/driver/earnings/summary/').status_code == 200

    after = snapshot()
    after['wallet'] = list(after['wallet'])

    assert after['wallet'] == before['wallet'], 'wallet balances must not move'
    assert after['wallet_txns'] == before['wallet_txns'], 'no ledger entry may appear'
    assert after['history'] == before['history'], 'settlement history is untouched'
    assert after['trips'] == before['trips'], 'no fare value may change'


@pytest.mark.django_db
def test_summary_and_list_agree_with_each_other(rider, driver):
    """Two endpoints, one definition. A drift between them is a support ticket."""
    from rest_framework.test import APIClient

    for fare, method in ((120, 'cash'), (240, 'online'), (60, 'cash')):
        credit_driver_wallet(_completed_trip(rider, driver, fare, method))

    api = APIClient()
    api.force_authenticate(user=driver.user_id)
    rows = api.get('/api/v1/driver/earnings/').json()
    rows = rows.get('data', rows)['results']
    summary = api.get('/api/v1/driver/earnings/summary/').json()
    summary = summary.get('data', summary)

    assert len(rows) == int(summary['total_trips'])
    assert sum(Decimal(r['amount']) for r in rows) == Decimal(summary['total_earned'])
    assert sum(Decimal(r['commission']) for r in rows) == Decimal(summary['total_commission'])
    assert sum(Decimal(r['net_amount']) for r in rows) == Decimal(summary['total_net'])
