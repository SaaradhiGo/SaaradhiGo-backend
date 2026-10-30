"""Before/after proof for the move to integer minor units.

WHAT THIS PROVES
----------------
Money moved from `Decimal(10,2)` columns to `BigInteger` minor units (paise,
cents). The owner's condition for that change was that behaviour stays
identical: the same quoted fares, the same totals, the same payouts, to the
paisa.

`tests/fixtures/money_parity_golden.json` was written by THIS file, running
against the code as it stood BEFORE the migration (capture mode, below). It is
the "before". Every run of this test is the "after": it re-prices the same grid
of quotes and pushes the same seeded trips through the real money path --
booking snapshot, promo, settlement and commission, driver and rider ledgers,
receipt tax, rider credits, withdrawal -- and compares every amount it reads
back against the golden file.

The golden file must never be regenerated to make this test pass. Regenerating
it is how you would hide a pricing change.

THE ONE KNOWN DIFFERENCE
------------------------
`quote_fare`, the receipt tax and rider credits rounded with `round(x, 2)` or
a bare `.quantize()` -- banker's rounding, ROUND_HALF_EVEN -- while the
commission and promos rounded HALF_UP. The owner's rule is a single
ROUND_HALF_UP, in the money helper. The two disagree only when an amount lands
exactly on a half paisa AND the paisa digit before it is even: 256.125 was
256.12 and is now 256.13.

Those cases are not hidden by choosing inputs that avoid them. The grid
contains them, the test proves every moved value moved ONLY because of the
rounding mode, and `EXPECTED_MOVED_PAID_AMOUNTS` freezes the list of charged
or paid amounts that moved. See docs/admin/AUDIT.md section 22.4.

    MONEY_PARITY_REPORT=moved.json pytest tests/test_money_minor_units_parity.py

writes every moved value, line items included, for review.

CAPTURE MODE
------------
    MONEY_PARITY_CAPTURE=1 pytest tests/test_money_minor_units_parity.py

writes the golden file instead of comparing. Run once, before the migration.
"""

import json
import os
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

GOLDEN = Path(__file__).parent / 'fixtures' / 'money_parity_golden.json'
CAPTURE = os.environ.get('MONEY_PARITY_CAPTURE') == '1'

User = get_user_model()

# ---------------------------------------------------------------------------
# The fixed inputs. Changing any of these invalidates the golden file.
# ---------------------------------------------------------------------------

HYD = (Decimal('17.385'), Decimal('78.486'))
NYC = (Decimal('40.7580'), Decimal('-73.9855'))

DISTANCES = ['0', '0.5', '1.0125', '2.5', '3.333', '7.25', '12.35', '23.987']
DURATIONS = ['0', '0.0625', '4', '9.5', '17.25', '41']
SURGES = ['1.00', '1.25', '1.37', '1.80']
NIGHTS = [False, True]

# (label, zone, distance, duration, surge, night, payment, promo)
SEEDED_TRIPS = [
    ('in-cash-plain',       'IN', '7.25',   '17.25', '1.00', False, 'cash',   None),
    ('in-online-plain',     'IN', '12.35',  '41',    '1.00', False, 'online', None),
    ('in-online-surge',     'IN', '3.333',  '9.5',   '1.37', False, 'online', None),
    ('in-cash-night',       'IN', '23.987', '41',    '1.25', True,  'cash',   None),
    ('in-online-minfare',   'IN', '0.5',    '4',     '1.00', False, 'online', None),
    ('in-online-capped',    'IN', '2.5',    '9.5',   '1.80', True,  'online', None),
    ('in-wallet-pct-promo', 'IN', '12.35',  '17.25', '1.00', False, 'wallet', 'PARITYPCT'),
    ('in-cash-flat-promo',  'IN', '7.25',   '9.5',   '1.00', False, 'cash',   'PARITYFLAT'),
    ('us-online-plain',     'US', '7.25',   '17.25', '1.00', False, 'online', None),
    ('us-cash-surge',       'US', '12.35',  '41',    '1.37', False, 'cash',   None),
    ('us-online-minfare',   'US', '0.5',    '0',     '1.00', False, 'online', None),
    ('us-online-night',     'US', '23.987', '41',    '1.25', True,  'online', None),
]

# Amounts a rider pays or a driver is paid -- totals, settlements, ledgers,
# receipts -- that moved from the old HALF_EVEN rounding to HALF_UP. Every one
# is an exact half-paisa tie, proven by the test below.
EXPECTED_MOVED_PAID_AMOUNTS = {
    'ledger.driver_available',  # 528.28 -> 528.29
    'ledger.driver_wallet_balance',  # 528.28 -> 528.29
    'quotes.IN|0|41|1.25|night.total_fare',  # 152.62 -> 152.63
    'quotes.IN|0|41|1.37|night.total_fare',  # 152.62 -> 152.63
    'quotes.IN|0|41|1.80|day.total_fare',  # 152.62 -> 152.63
    'quotes.IN|0|41|1.80|night.total_fare',  # 152.62 -> 152.63
    'quotes.IN|12.35|41|1.00|day.total_fare',  # 256.12 -> 256.13
    'quotes.IN|23.987|17.25|1.00|day.total_fare',  # 360.02 -> 360.03
    'quotes.IN|7.25|0|1.00|day.total_fare',  # 120.62 -> 120.63
    'quotes.IN|7.25|4|1.00|day.total_fare',  # 127.62 -> 127.63
    'quotes.US|0.5|4|1.80|day.total_fare',  # 12.20 -> 12.21
    'quotes.US|0.5|9.5|1.00|night.total_fare',  # 10.83 -> 10.84
    'quotes.US|0.5|9.5|1.80|night.total_fare',  # 17.30 -> 17.31
    'quotes.US|0|41|1.25|day.tax_amount',  # 1.87 -> 1.88
    'quotes.US|0|41|1.25|day.total_fare',  # 25.74 -> 25.76
    'quotes.US|12.35|17.25|1.25|night.tax_amount',  # 3.68 -> 3.69
    'quotes.US|12.35|17.25|1.25|night.total_fare',  # 47.95 -> 47.97
    'quotes.US|2.5|0|1.80|day.total_fare',  # 16.32 -> 16.33
    'quotes.US|2.5|4|1.00|day.total_fare',  # 11.81 -> 11.82
    'quotes.US|2.5|4|1.80|day.total_fare',  # 19.06 -> 19.07
    'trips.in-online-plain.driver_ledger.amount',  # 210.02 -> 210.03
    'trips.in-online-plain.fare_pricing.total_fare',  # 256.12 -> 256.13
    'trips.in-online-plain.quote.total_fare',  # 256.12 -> 256.13
    'trips.in-online-plain.receipt.total_fare',  # 256.12 -> 256.13
    'trips.in-online-plain.settlement.driver_net',  # 210.02 -> 210.03
    'trips.in-online-plain.settlement.gross_fare',  # 256.12 -> 256.13
    'trips.in-online-plain.transaction_history.amount',  # 210.02 -> 210.03
    'trips.in-online-plain.trip.estimated_fare',  # 256.12 -> 256.13
}


def _s(value):
    """A money value as the string the API would show, or None."""
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(Decimal('0.01')))


# ---------------------------------------------------------------------------
# Market setup: Hyderabad (INR, GST inclusive) and New York (USD, sales tax
# added on top, plus a flat per-trip fee).
# ---------------------------------------------------------------------------

def _square(lat, lng, half=Decimal('0.05')):
    lat, lng = float(lat), float(lng)
    h = float(half)
    return {'type': 'Polygon', 'coordinates': [[
        [lng - h, lat - h], [lng - h, lat + h], [lng + h, lat + h],
        [lng + h, lat - h], [lng - h, lat - h],
    ]]}


@pytest.fixture
def market(db):
    from servers.driver.models import VehicleType
    from servers.pricing.models import RateCard, ServiceZone
    from servers.pricing.services import invalidate_polygon_cache

    vt = VehicleType.objects.create(type='parity-sedan')

    hyd = ServiceZone.objects.create(
        code='IN-TG-HYD-PARITY', name='Hyderabad parity', country='IN',
        state_code='TG', city='Hyderabad', zone_type='subzone', priority=500,
        polygon_geojson=_square(*HYD), is_active=True,
    )
    nyc = ServiceZone.objects.create(
        code='US-NY-NYC-PARITY', name='New York parity', country='US',
        state_code='NY', city='New York', zone_type='city', priority=500,
        polygon_geojson=_square(*NYC), is_active=True,
        currency='USD', timezone_name='America/New_York', tax_mode='additive',
        per_trip_fee=Decimal('2.75'), per_trip_fee_label='Congestion surcharge',
    )
    invalidate_polygon_cache()

    past = timezone.now() - timedelta(days=30)
    RateCard.objects.create(
        zone=hyd, vehicle_type=vt, version=1, effective_from=past,
        base_fare=Decimal('30.00'), per_km_fare=Decimal('12.50'),
        per_min_fare=Decimal('1.75'), min_fare=Decimal('60.00'),
        night_surge_multiplier=Decimal('1.25'), surge_cap_multiplier=Decimal('1.50'),
        commission_percent=Decimal('18.00'), gst_percent=Decimal('5.000'),
    )
    RateCard.objects.create(
        zone=nyc, vehicle_type=vt, version=1, effective_from=past,
        base_fare=Decimal('2.55'), per_km_fare=Decimal('1.75'),
        per_min_fare=Decimal('0.35'), min_fare=Decimal('7.00'),
        night_surge_multiplier=Decimal('1.10'), surge_cap_multiplier=Decimal('2.00'),
        commission_percent=Decimal('25.00'), gst_percent=Decimal('8.875'),
    )
    # The golden file was captured when the commission came from these rate cards
    # (18 and 25). Commission is a CommissionRule now (module 8); the same two rates
    # are set as country rules so the money path stays comparable with the golden.
    from servers.pricing.commission_models import ACTIVE, CommissionRule
    for country, pct in (('IN', '18.00'), ('US', '25.00')):
        CommissionRule.objects.update_or_create(
            country=country, geo_node=None, vehicle_type=None, version=1,
            defaults={'percent': Decimal(pct), 'status': ACTIVE, 'effective_from': past,
                      'reason': 'golden-file parity'})
    return {'vt': vt, 'IN': hyd, 'US': nyc}


def _at(zone_key, night):
    """Noon or 1am local, on a fixed date, in the zone's own time zone."""
    tz = ZoneInfo('Asia/Kolkata' if zone_key == 'IN' else 'America/New_York')
    return datetime(2026, 10, 14, 1 if night else 12, 0, tzinfo=tz)


def _quote(zone_key, distance, duration, surge, night):
    from servers.pricing.services import quote_fare

    lat, lng = HYD if zone_key == 'IN' else NYC
    with mock.patch('servers.pricing.services.compute_surge_multiplier',
                    return_value=Decimal(surge)):
        return quote_fare(
            distance_km=Decimal(distance), duration_min=Decimal(duration),
            vehicle_type='parity-sedan', pickup_lat=lat, pickup_lon=lng,
            at=_at(zone_key, night),
        )


def _quote_record(q):
    out = {k: _s(q[k]) for k in (
        'total_fare', 'base_fare', 'distance_fare', 'time_fare',
        'tax_amount', 'per_trip_fee')}
    out['currency'] = q['currency']
    out['surge_multiplier'] = str(q['surge_multiplier'])
    out['min_fare_applied'] = q['min_fare_applied']
    out['night_surge_applied'] = q['night_surge_applied']
    out['adjustments'] = [[a['code'], _s(a['amount'])] for a in q['fare_adjustments']]
    return out


def _grid():
    for zone_key in ('IN', 'US'):
        for d in DISTANCES:
            for t in DURATIONS:
                for s in SURGES:
                    for n in NIGHTS:
                        yield f'{zone_key}|{d}|{t}|{s}|{"night" if n else "day"}', (zone_key, d, t, s, n)


# ---------------------------------------------------------------------------
# The seeded trips, through the real money path
# ---------------------------------------------------------------------------

def _promos(market):
    from servers.ride.models import PromoCode

    now = timezone.now()
    PromoCode.objects.create(
        code='PARITYPCT', discount_type='percent', discount_value=Decimal('15.00'),
        max_discount_amount=Decimal('40.00'), min_fare=Decimal('100.00'),
        valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=30),
        max_per_user_redemptions=50,
    )
    PromoCode.objects.create(
        code='PARITYFLAT', discount_type='flat', discount_value=Decimal('25.00'),
        min_fare=Decimal('0.00'),
        valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=30),
        max_per_user_redemptions=50,
    )


def _people():
    from servers.driver.models import Driver

    rider = User.objects.create_user(phone_number='+919700077001', role='rider',
                                     full_name='Parity Rider')
    du = User.objects.create_user(phone_number='+919800077001', role='driver',
                                  full_name='Parity Driver')
    driver = Driver.objects.create(user_id=du, approved=True)
    return rider, driver


def _run_trip(market, rider, driver, spec):
    from servers.driver.utils import credit_driver_wallet
    from servers.payments.models import TransactionHistory, TripSettlement
    from servers.ride.models import FarePricing, Receipt, Trip, TripStatus
    from servers.ride.promos import apply_promo
    from servers.ride.receipts import issue_receipt
    from servers.rider.models import WalletTransaction

    label, zone_key, d, t, s, night, payment, promo = spec
    q = _quote(zone_key, d, t, s, night)
    lat, lng = HYD if zone_key == 'IN' else NYC
    zone = market[zone_key]
    completed, _ = TripStatus.objects.get_or_create(status_code='completed')

    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, zone=zone,
        pickup_lat=lat, pickup_long=lng, destination_lat=lat, destination_long=lng,
        estimated_fare=q['total_fare'],
        estimated_distance_km=Decimal(d), estimated_duration_min=Decimal(t),
        surge_multiplier=q['surge_multiplier'],
        requested_vehicle_type=market['vt'], payment_method=payment,
    )
    FarePricing.objects.create(
        trip_id=trip, base_fare=q['base_fare'], distance_fare=q['distance_fare'],
        time_fare=q['time_fare'], surge_multiplier=q['surge_multiplier'],
        total_fare=q['total_fare'],
    )

    promo_out = None
    if promo:
        res = apply_promo(promo, rider, q['total_fare'], zone=None)
        promo_out = {'ok': res.ok, 'discount': _s(res.discount_amount),
                     'final': _s(res.final_fare)}
        if res.ok:
            trip.final_fare = res.final_fare
            trip.save()

    trip.status_id = completed
    trip.completed_at = timezone.now()
    trip.save()

    credit_driver_wallet(trip)
    receipt = issue_receipt(trip)

    trip.refresh_from_db()
    fp = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
    st = TripSettlement.objects.get(trip=trip)
    wt = WalletTransaction.objects.get(idempotency_key=f'TRIP_{trip.id}_EARNING')
    th = TransactionHistory.objects.filter(trip_id=trip).order_by('-id').first()
    receipt = Receipt.objects.get(pk=receipt.pk)

    return {
        'quote': _quote_record(q),
        'promo': promo_out,
        'trip': {'estimated_fare': _s(trip.estimated_fare),
                 'final_fare': _s(trip.final_fare),
                 'cancellation_fee': _s(trip.cancellation_fee),
                 'currency': trip.currency},
        'fare_pricing': {k: _s(getattr(fp, k)) for k in (
            'base_fare', 'distance_fare', 'time_fare', 'total_fare')},
        'settlement': {'gross_fare': _s(st.gross_fare),
                       'commission_percent': str(st.commission_percent),
                       'commission_amount': _s(st.commission_amount),
                       'driver_net': _s(st.driver_net),
                       'currency': st.currency},
        'driver_ledger': {'amount': _s(wt.amount), 'txn_type': wt.txn_type,
                          'currency': wt.currency},
        'transaction_history': {'amount': _s(th.amount), 'txn_type': th.txn_type},
        'receipt': {'total_fare': _s(receipt.total_fare),
                    'gst_amount': _s(receipt.gst_amount),
                    'currency': receipt.currency},
    }


def _run_ledger_tail(rider, driver):
    """Rider credits and a driver withdrawal after all the trips settled."""
    from servers.driver.models import WithdrawalRequest
    from servers.driver.services import calculate_available_balance
    from servers.rider.credits import issue_refund_credit, issue_support_credit
    from servers.rider.models import Wallet

    r1 = issue_refund_credit(rider, '45.50', trip_id=1, idempotency_key='PARITY-R1')
    r2 = issue_support_credit(rider, Decimal('120'), ticket_ref='T-1',
                              idempotency_key='PARITY-S1')
    r3 = issue_refund_credit(rider, '45.50', trip_id=1, idempotency_key='PARITY-R1')

    available = calculate_available_balance(driver)
    wr = WithdrawalRequest.objects.create(driver=driver, amount=Decimal('500.00'))
    wr = WithdrawalRequest.objects.get(pk=wr.pk)

    rider_wallet = Wallet.objects.get(user_id=rider, scope=Wallet.SCOPE_RIDER)
    driver_wallet = Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER)
    return {
        'credits': [[c.ok, c.reason, _s(c.new_balance)] for c in (r1, r2, r3)],
        'rider_wallet_balance': _s(rider_wallet.balance),
        'driver_wallet_balance': _s(driver_wallet.balance),
        'driver_available': _s(available),
        'withdrawal_amount': _s(wr.amount),
    }


def _observe(market):
    _promos(market)
    rider, driver = _people()
    trips = {spec[0]: _run_trip(market, rider, driver, spec) for spec in SEEDED_TRIPS}
    return {
        'quotes': {key: _quote_record(_quote(*args)) for key, args in _grid()},
        'trips': trips,
        'ledger': _run_ledger_tail(rider, driver),
    }


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------

def _flatten(node, prefix=''):
    """{'a': {'b': [1, 2]}} -> {'a.b.0': 1, 'a.b.1': 2}: one entry per value."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            out.update(_flatten(v, f'{prefix}{k}.'))
        return out
    if isinstance(node, list):
        out = {}
        for i, v in enumerate(node):
            out.update(_flatten(v, f'{prefix}{i}.'))
        return out
    return {prefix[:-1]: node}


def _normalised(observed):
    """Adjustments as {code: amount}, with an absent 'rounding' line as 0.00.

    A tie that rounds the other way can make the reconciling 'rounding' line
    appear (0.00 -> 0.01) or disappear; that is a value change, not a change
    of shape, and is compared as one.
    """
    def fix(record):
        record = dict(record)
        adj = {code: amount for code, amount in record['adjustments']}
        adj.setdefault('rounding', '0.00')
        record['adjustments'] = adj
        return record

    out = json.loads(json.dumps(observed))
    out['quotes'] = {k: fix(v) for k, v in out['quotes'].items()}
    for trip in out['trips'].values():
        trip['quote'] = fix(trip['quote'])
    return out


def _is_itemised_line(key):
    """A display line of a fare breakdown, as opposed to an amount charged or
    paid: the base/distance/time components and the named adjustments that
    reconcile them to the total."""
    parts = key.split('.')
    return parts[-1] in ('base_fare', 'distance_fare', 'time_fare') or 'adjustments' in parts


def _observe_in_a_fresh_transaction(market, rounding):
    """One full run with the money helper's single rounding rule set to
    `rounding`, rolled back afterwards so the next run starts identical."""
    from django.db import transaction

    class _Rollback(Exception):
        pass

    box = {}
    try:
        with transaction.atomic(), mock.patch('base.money.ROUNDING', rounding):
            box['observed'] = _observe(market)
            raise _Rollback
    except _Rollback:
        pass
    return box['observed']


@pytest.mark.django_db
def test_money_path_matches_the_pre_migration_golden_file(market, settings):
    """The after equals the before, except where only the rounding mode moved
    a half-paisa tie -- and every such value is listed and checked.

    HOW "ONLY THE ROUNDING MODE" IS PROVEN
    The old code rounded some amounts HALF_EVEN (`round()`, a bare
    `.quantize()`) and others HALF_UP (commission, promos). The new code has
    one rule, `base.money.ROUNDING`, and nothing else rounds. So the same
    seeded money path is run twice through the NEW code: once as configured
    (HALF_UP) and once with that one constant switched to HALF_EVEN.

    Every pre-migration value must equal the HALF_UP run or the HALF_EVEN run.
    A value that equals neither changed for some reason OTHER than the
    rounding mode -- a lost conversion, a 100x error, a dropped field -- and
    fails the test. A value that equals only the HALF_EVEN run is a tie the new
    rule rounds the other way, and must differ by exactly one minor unit.
    """
    settings.PRICING_ALLOW_DEFAULT_FARE = False

    if CAPTURE:
        observed = _observe(market)
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(observed, indent=1, sort_keys=True) + '\n',
                          encoding='utf-8')
        pytest.skip(f'captured {GOLDEN.name}; not a comparison run')

    golden = _flatten(_normalised(json.loads(GOLDEN.read_text(encoding='utf-8'))))
    up = _flatten(_normalised(_observe_in_a_fresh_transaction(market, ROUND_HALF_UP)))
    even = _flatten(_normalised(_observe_in_a_fresh_transaction(market, ROUND_HALF_EVEN)))

    # Same shape: no value appeared or disappeared.
    assert set(up) ^ set(golden) == set()
    assert set(even) ^ set(golden) == set()

    moved = {k: (golden[k], up[k]) for k in golden if golden[k] != up[k]}

    # The driver's running balance spans every trip, and the OLD code rounded
    # the fare HALF_EVEN but the commission HALF_UP -- so no single mode
    # reproduces it. It is explained instead by its parts: it must move by
    # exactly the sum of the per-trip ledger rows that moved.
    def _delta(key):
        before, after = moved.get(key, (golden[key], golden[key]))
        return Decimal(after) - Decimal(before)

    ledger_rows = [k for k in golden if k.startswith('trips.') and k.endswith('driver_ledger.amount')]
    expected_balance_delta = sum(
        (_delta(k) if golden[k.replace('.amount', '.txn_type')] == 'credit' else -_delta(k))
        for k in ledger_rows)
    for key in ('ledger.driver_wallet_balance', 'ledger.driver_available'):
        assert _delta(key) == expected_balance_delta, key
        even[key] = golden[key]  # explained above

    unexplained = {k: (golden[k], up[k]) for k in golden
                   if golden[k] != up[k] and golden[k] != even[k]}
    assert unexplained == {}, (
        f'changed for a reason other than the rounding mode; all moved: '
        f'{ {k: v for k, v in moved.items() if not k.startswith("quotes.")} }')
    # Size of each move. An amount rounded once moves by one minor unit. A US
    # total is two roundings -- the metered fare, then the sales tax computed
    # on it -- and a driver's net is a total less a rounded commission, so
    # those can move by two. Nothing moves by more. The 'rounding' line is
    # not itself rounded -- it is the remainder that reconciles the itemised
    # lines to the total -- so what must hold for it is that the breakdown
    # still adds up, checked below.
    for key, (before, after) in moved.items():
        if key.endswith('.adjustments.rounding'):
            continue
        assert Decimal('0.01') <= abs(Decimal(after) - Decimal(before)) <= Decimal('0.02'), key

    observed_up = _normalised(_observe_in_a_fresh_transaction(market, ROUND_HALF_UP))
    for key, q in observed_up['quotes'].items():
        lines = (Decimal(q['base_fare']) + Decimal(q['distance_fare'])
                 + Decimal(q['time_fare'])
                 + sum(Decimal(a) for a in q['adjustments'].values()))
        assert lines == Decimal(q['total_fare']), f'{key} breakdown does not add up'

    report = os.environ.get('MONEY_PARITY_REPORT')
    if report:
        Path(report).write_text(json.dumps(
            {k: list(v) for k, v in sorted(moved.items())}, indent=1), encoding='utf-8')

    # The amounts a rider pays or a driver is paid that moved -- as opposed to
    # itemised display lines, where the 'rounding' line absorbs the move.
    # Frozen, so a new one cannot appear without this list being edited.
    paid = {k for k in moved if not _is_itemised_line(k)}
    assert paid == EXPECTED_MOVED_PAID_AMOUNTS
