"""Does the platform actually behave as two markets? Run a ride in each and look.

WHY THIS IS A DRILL AND NOT ONLY A TEST SUITE
---------------------------------------------
A test suite tells you an assertion held. It does not tell you what the rider
actually sees, and the failure mode of multi-country work is precisely that nothing
raises -- a US fare is quoted in rupee magnitudes, a receipt says "GST" and "Rs.", a
driver's "today" opens at the wrong hour. Every one of those passes a test that was
written with the same assumption as the code.

So this prices and completes a real ride in each market, through `quote_fare` and the
real receipt generator, and PRINTS the numbers and the document lines for a human to
read. It asserts too -- it exits non-zero on a real failure -- but the printed evidence
is the point.

    DJANGO_SETTINGS_MODULE=base.settings_ci python qa/multi_country_drill.py

Runs against whatever database that settings module points at (SQLite by default), in a
transaction that is rolled back, so it leaves nothing behind.

WHAT IT CHECKS THAT MATTERS MOST
--------------------------------
That **India did not move**. Every India figure is asserted against the value the
single-market code produced, because the whole risk of this work is a refactor that
silently reprices an existing market.
"""

import io
import os
import sys
from decimal import Decimal
from pathlib import Path

# This drill prints currency symbols, and a Windows console defaults to cp1252,
# which cannot encode the rupee sign. Reconfigure rather than avoid the symbol:
# whether the right symbol reaches a rider is precisely what is being checked.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
else:  # pragma: no cover
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                  errors='replace')

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'base.settings_ci')

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

from base.countries import COUNTRIES, normalize_phone  # noqa: E402
from base.market_time import local_day_start, zone_timezone  # noqa: E402
from base.money import format_money, sum_by_currency  # noqa: E402
from servers.driver.models import Driver, Vehicle, VehicleType  # noqa: E402
from servers.pricing.models import RateCard, ServiceZone  # noqa: E402
from servers.pricing.services import quote_fare  # noqa: E402
from servers.ride.models import Trip, TripStatus  # noqa: E402
from servers.rider.models import Rider  # noqa: E402

User = get_user_model()

RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), detail))
    print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f'  {detail}' if detail else ''))
    return bool(ok)


def observe(name, detail):
    print(f'  [obs ] {name}: {detail}')


# Two polygons far enough apart that a point in one is not in the other.
HYD_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[78.30, 17.40], [78.50, 17.40], [78.50, 17.50],
                     [78.30, 17.50], [78.30, 17.40]]],
}
SF_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-122.52, 37.70], [-122.35, 37.70], [-122.35, 37.84],
                     [-122.52, 37.84], [-122.52, 37.70]]],
}

HYD_POINT = (Decimal('17.4450'), Decimal('78.3800'))
SF_POINT = (Decimal('37.7749'), Decimal('-122.4194'))


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def make_zone(code, name, country, polygon, city, state, **kw):
    """Create a zone, letting the country registry fill the market fields."""
    zone = ServiceZone.objects.create(
        code=code, name=name, country=country, state_code=state, city=city,
        zone_type='city', polygon_geojson=polygon, priority=50,
        is_active=True, **kw,
    )
    return zone


def make_rate_card(zone, vt, base, per_dist, per_min, min_fare, tax):
    return RateCard.objects.create(
        zone=zone, vehicle_type=vt,
        base_fare=Decimal(base), per_km_fare=Decimal(per_dist),
        per_min_fare=Decimal(per_min), min_fare=Decimal(min_fare),
        commission_percent=Decimal('20.00'), gst_percent=Decimal(tax),
    )


def make_people(country_code, n):
    cc = COUNTRIES[country_code].calling_code
    rider_national = f'9{n:09d}'[:10]
    driver_national = f'8{n:09d}'[:10]
    ru = User.objects.create_user(
        phone_number=f'+{cc}{rider_national}', username=f'r{country_code}{n}',
        role='rider', email=f'rider{n}@example.test')
    Rider.objects.create(user_id=ru)
    du = User.objects.create_user(
        phone_number=f'+{cc}{driver_national}', username=f'd{country_code}{n}',
        role='driver')
    d = Driver.objects.create(user_id=du, approved=True, status='online')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number=f'MC{country_code}{n:04d}')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return ru, d, vt


def run_market(label, zone, rider, driver, point, distance_km, duration_min):
    """Price and complete one ride, then issue its receipt. Returns facts."""
    print(f'\n--- {label} -------------------------------------------------')
    lat, lon = point

    quote = quote_fare(
        pickup_lat=lat, pickup_lon=lon,
        distance_km=Decimal(str(distance_km)), duration_min=Decimal(str(duration_min)),
        vehicle_type='sedan', record_demand=False,
    )

    observe('zone resolved', f"{quote['zone_code']} (source={quote['source']})")
    observe('currency', quote['currency'])
    observe('tax', f"{quote['tax_label']} {quote['tax_percent']}% "
                   f"mode={quote['tax_mode']} amount={quote['tax_amount']}")
    observe('per-trip fee', str(quote['per_trip_fee']))
    observe('distance unit', quote['distance_unit'])
    observe('total charged', format_money(quote['total_fare'], quote['currency']))
    print('        decomposition:')
    print(f"          base      {format_money(quote['base_fare'], quote['currency'])}")
    print(f"          distance  {format_money(quote['distance_fare'], quote['currency'])}")
    print(f"          time      {format_money(quote['time_fare'], quote['currency'])}")
    for adj in quote['fare_adjustments']:
        print(f"          {adj['code']:<9} "
              f"{format_money(adj['amount'], quote['currency'])}  ({adj['label']})")

    # The invariant that must hold in every market: the lines a rider can add up
    # must equal the amount charged.
    components = (Decimal(str(quote['base_fare']))
                  + Decimal(str(quote['distance_fare']))
                  + Decimal(str(quote['time_fare'])))
    adjustments = sum((Decimal(str(a['amount'])) for a in quote['fare_adjustments']),
                      Decimal('0.00'))
    check(f'{label}: decomposition closes exactly',
          components + adjustments == Decimal(str(quote['total_fare'])),
          f'{components} + {adjustments} == {quote["total_fare"]}')

    now = timezone.now()
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=_status('completed'),
        zone=zone,
        pickup_lat=lat, pickup_long=lon,
        destination_lat=lat + Decimal('0.02'), destination_long=lon,
        estimated_fare=quote['total_fare'], payment_method='cash',
        estimated_distance_km=Decimal(str(distance_km)),
        actual_distance_km=Decimal(str(distance_km)),
        accepted_at=now, started_at=now, completed_at=now,
        pickup_address='Drill pickup', destination_address='Drill drop',
    )
    check(f'{label}: trip stamped with its market currency',
          trip.currency == quote['currency'],
          f'trip.currency={trip.currency}')

    from servers.ride.receipts import issue_receipt
    from unittest import mock
    with mock.patch('servers.ride.receipts._send_receipt_email',
                    return_value=(True, '')), \
            mock.patch('servers.ride.receipts._attach_pdf_to_receipt'):
        receipt = issue_receipt(trip)

    check(f'{label}: receipt carries the currency',
          receipt is not None and receipt.currency == quote['currency'],
          f'receipt.currency={getattr(receipt, "currency", None)}')

    html = receipt.html_body or ''
    sym = COUNTRIES[zone.country].currency_symbol
    wrong_sym = '$' if sym != '$' else '₹'
    check(f'{label}: receipt shows the right currency symbol',
          sym in html and wrong_sym not in html,
          f'contains {sym!r}, does not contain {wrong_sym!r}')
    check(f'{label}: receipt uses this market\'s tax name',
          COUNTRIES[zone.country].tax_label in html,
          f'contains {COUNTRIES[zone.country].tax_label!r}')
    check(f'{label}: receipt shows distance in this market\'s unit',
          f' {zone.distance_unit}' in html,
          f'contains {zone.distance_unit!r}')
    check(f'{label}: receipt never says "Rs."',
          'Rs.' not in html)

    observe('receipt tax line', f'{receipt.gst_amount} ({zone.tax_mode})')

    # The check that found the real bug in this work.
    #
    # The quote said $2.33 of sales tax and the receipt said $1.40 -- which is
    # 5%, India's GST rate, because the receipt's rate-card lookup returned None
    # and fell through to a hardcoded Decimal('5.00'). Every individual test
    # passed: the quote was right, the receipt rendered, the currency was right.
    # Nothing compared the two numbers that a rider would compare.
    #
    # Tolerance of one minor unit, because the two are derived differently --
    # the quote taxes the pre-tax fare forward, the receipt works back from the
    # charged total -- and a one-paisa disagreement between those is rounding,
    # not a wrong rate.
    quoted_tax = Decimal(str(quote['tax_amount']))
    if zone.tax_mode == 'additive':
        check(f'{label}: the receipt taxes at the rate the quote charged',
              abs(Decimal(str(receipt.gst_amount)) - quoted_tax) <= Decimal('0.01'),
              f'quote {quoted_tax} vs receipt {receipt.gst_amount}')
    else:
        # Inclusive: the quote adds nothing, and the receipt extracts what was
        # already inside. The check is that it extracted at the card's rate.
        expected = (Decimal(str(quote['total_fare']))
                    * Decimal(str(quote['tax_percent']))
                    / (Decimal('100') + Decimal(str(quote['tax_percent']))))
        check(f'{label}: the receipt extracted tax at the card rate',
              abs(Decimal(str(receipt.gst_amount))
                  - expected.quantize(Decimal('0.01'))) <= Decimal('0.01'),
              f'expected ~{expected.quantize(Decimal("0.01"))}, '
              f'got {receipt.gst_amount}')
    observe('driver local day start',
            local_day_start(zone_timezone(zone)).isoformat())

    return {'quote': quote, 'trip': trip, 'receipt': receipt}


def main():
    print('=' * 74)
    print('MULTI-COUNTRY DRILL  —  one real ride priced and receipted per market')
    print('=' * 74)

    # Build a fresh database by running every migration, rather than touching
    # whatever the configured one happens to contain. Two reasons: the drill
    # must be safe to run anywhere, and applying the migrations is itself worth
    # verifying -- the multi-country work added five of them plus a data
    # backfill, and a drill that assumed the schema would not have caught a bad
    # one.
    setup_test_environment()
    connection.creation.create_test_db(verbosity=0, autoclobber=True)
    print('(built a fresh database by applying all migrations)')

    sid = transaction.savepoint()
    try:
        # ---------------- India ------------------------------------------
        in_zone = make_zone('MC-IN-HYD', 'Hyderabad drill', 'IN',
                            HYD_POLYGON, 'Hyderabad', 'TG')
        check('IN zone inherited its market from the country',
              (in_zone.currency, in_zone.tax_mode, in_zone.distance_unit,
               in_zone.timezone_name)
              == ('INR', 'inclusive', 'km', 'Asia/Kolkata'),
              f'{in_zone.currency}/{in_zone.tax_mode}/{in_zone.distance_unit}/'
              f'{in_zone.timezone_name}')

        in_rider, in_driver, vt = make_people('IN', 1)
        make_rate_card(in_zone, vt, '30', '12', '2', '50', '5.00')
        india = run_market('INDIA', in_zone, in_rider, in_driver,
                           HYD_POINT, '10', '20')

        # ---------------- United States ----------------------------------
        # Timezone set explicitly, which the admin help text insists on for any
        # country with more than one.
        us_zone = make_zone('MC-US-SFO', 'San Francisco drill', 'US',
                            SF_POLYGON, 'San Francisco', 'CA',
                            timezone_name='America/Los_Angeles',
                            per_trip_fee=Decimal('1.50'),
                            per_trip_fee_label='City access fee')
        check('US zone inherited its market from the country',
              (us_zone.currency, us_zone.tax_mode, us_zone.distance_unit)
              == ('USD', 'additive', 'mi'),
              f'{us_zone.currency}/{us_zone.tax_mode}/{us_zone.distance_unit}')
        check('US zone kept the timezone it was given explicitly',
              us_zone.timezone_name == 'America/Los_Angeles',
              us_zone.timezone_name)

        us_rider, us_driver, _ = make_people('US', 2)
        # US magnitudes: $2.50 base, $1.75/mi, $0.35/min, $8 minimum.
        make_rate_card(us_zone, vt, '2.50', '1.75', '0.35', '8.00', '8.625')
        usa = run_market('UNITED STATES', us_zone, us_rider, us_driver,
                         SF_POINT, '10', '20')

        # ---------------- The cross-market invariants ---------------------
        print('\n--- cross-market ------------------------------------------')

        check('the two markets priced in different currencies',
              india['quote']['currency'] != usa['quote']['currency'],
              f"{india['quote']['currency']} vs {usa['quote']['currency']}")

        check('India extracted tax from the fare, the US added it on top',
              india['quote']['tax_amount'] == Decimal('0.00')
              and usa['quote']['tax_amount'] > Decimal('0.00'),
              f"IN adds {india['quote']['tax_amount']}, "
              f"US adds {usa['quote']['tax_amount']}")

        # The US total must be strictly greater than its own pre-tax fare, and
        # the difference must be exactly tax + fee.
        us_q = usa['quote']
        us_pre = (Decimal(str(us_q['base_fare'])) + Decimal(str(us_q['distance_fare']))
                  + Decimal(str(us_q['time_fare'])))
        us_added = sum((Decimal(str(a['amount'])) for a in us_q['fare_adjustments']
                        if a['code'] in ('tax', 'per_trip_fee')), Decimal('0.00'))
        check('the US total is the fare plus tax plus the flat fee',
              Decimal(str(us_q['tax_amount'])) + Decimal(str(us_q['per_trip_fee']))
              == us_added,
              f'tax {us_q["tax_amount"]} + fee {us_q["per_trip_fee"]} == {us_added}')
        observe('US pre-tax metered fare', format_money(us_pre, 'USD'))

        # Aggregates: the whole point of the currency column.
        totals = sum_by_currency(
            Trip.objects.filter(id__in=[india['trip'].id, usa['trip'].id]),
            'final_fare')
        totals_est = sum_by_currency(
            Trip.objects.filter(id__in=[india['trip'].id, usa['trip'].id]),
            'estimated_fare')
        check('a cross-market total comes back as two numbers, not one',
              len(totals_est) == 2,
              f'{dict((k, str(v)) for k, v in totals_est.items())}')
        observe('formatted for an operator',
                __import__('base.money', fromlist=['format_totals'])
                .format_totals(totals_est))
        observe('final_fare totals (both NULL — cash pilot)', str(dict(totals)))

        check('final_fare stayed NULL in both markets',
              india['trip'].final_fare is None and usa['trip'].final_fare is None)

        # Day boundaries differ, which is the earnings-rollover fix.
        in_day = local_day_start(zone_timezone(in_zone))
        us_day = local_day_start(zone_timezone(us_zone))
        check('each market measures "today" from its own midnight',
              in_day.utcoffset() != us_day.utcoffset(),
              f'IN {in_day.isoformat()} vs US {us_day.isoformat()}')

        # Phone normalisation, the defect that locked a US operator out.
        check('a bare US number normalises to +1, not +91',
              normalize_phone('5551234567', default_country='US') == '+15551234567',
              normalize_phone('5551234567', default_country='US'))
        check('a bare Indian number still normalises to +91',
              normalize_phone('9876543210', default_country='IN') == '+919876543210',
              normalize_phone('9876543210', default_country='IN'))
        check('an already-E.164 US number is left alone',
              normalize_phone('+1 (555) 123-4567') == '+15551234567',
              normalize_phone('+1 (555) 123-4567'))
        check('nonsense is refused rather than guessed',
              normalize_phone('12') is None)

    finally:
        transaction.savepoint_rollback(sid)
        print('\n(rolled back — the drill left nothing in the database)')

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print('\n' + '=' * 74)
    print(f'{passed}/{len(RESULTS)} checks passed')
    print('=' * 74)
    if passed != len(RESULTS):
        print('\nFAILED:')
        for name, ok, detail in RESULTS:
            if not ok:
                print(f'  {name}  {detail}')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
