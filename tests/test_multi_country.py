"""Multi-country support: the market is a property of the zone, not of the deployment.

WHAT THIS FILE IS FOR
---------------------
Before this work "India" was spread across the codebase as a dozen unattributed
constants: a global `Asia/Kolkata`, `+91` in three phone helpers, a log-redaction
regex that only matched Indian numbers, inclusive GST arithmetic on the receipt,
`per_km_fare` on every rate card, and no currency on any money row.

None of those were wrong. They were just not *attributed*, so none of them failed
loudly when a second market appeared — they produced a US rider charged in rupee
magnitudes, a receipt headed "GST" and "Rs.", and a driver whose "today" opened at
14:30 the previous afternoon.

So the tests here are mostly about the second market behaving differently, and about
**India behaving exactly as before**. The second half is the one that matters: the
whole risk of this change is a refactor that silently reprices a live market.

THERE IS A DRILL TOO
--------------------
`qa/multi_country_drill.py` prices and receipts a real ride in each market and prints
the numbers. It found a defect these tests would not have: the quote charged US sales
tax at 8.625% while the receipt recomputed it at 5% — India's rate — because the
receipt's rate-card lookup fell through to a hardcoded `Decimal('5.00')`. Every
individual assertion passed; nothing compared the two numbers a rider would compare.
That comparison is now `test_the_receipt_taxes_at_the_rate_the_quote_charged` below.
"""

import logging
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from base.countries import (
    COUNTRIES, INDIA, UNITED_STATES, country_of_phone, get_country, is_supported,
    normalize_phone,
)
from base.market_time import local_day_start, zone_timezone
from base.money import (
    DEFAULT_CURRENCY, format_money, format_totals, normalize_currency, quantize,
    single_currency_or_none, sum_by_currency,
)
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.pricing.models import RateCard, ServiceZone
from servers.pricing.services import quote_fare
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider, Wallet, WalletTransaction

User = get_user_model()

pytestmark = pytest.mark.django_db

_n = iter(range(1, 9999))

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
HYD = (Decimal('17.4450'), Decimal('78.3800'))
SF = (Decimal('37.7749'), Decimal('-122.4194'))


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def vehicle_type():
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return vt


@pytest.fixture
def india_zone():
    return ServiceZone.objects.create(
        code='T-IN-HYD', name='Hyderabad', country='IN', state_code='TG',
        city='Hyderabad', zone_type='city', polygon_geojson=HYD_POLYGON,
        priority=50, is_active=True,
    )


@pytest.fixture
def us_zone():
    return ServiceZone.objects.create(
        code='T-US-SFO', name='San Francisco', country='US', state_code='CA',
        city='San Francisco', zone_type='city', polygon_geojson=SF_POLYGON,
        priority=50, is_active=True, timezone_name='America/Los_Angeles',
    )


def _card(zone, vt, base, per_dist, per_min, min_fare, tax):
    return RateCard.objects.create(
        zone=zone, vehicle_type=vt, base_fare=Decimal(base),
        per_km_fare=Decimal(per_dist), per_min_fare=Decimal(per_min),
        min_fare=Decimal(min_fare), commission_percent=Decimal('20.00'),
        gst_percent=Decimal(tax),
    )


def _rider(country='IN'):
    i = next(_n)
    cc = COUNTRIES[country].calling_code
    u = User.objects.create_user(
        phone_number=f'+{cc}9{i:09d}'[:16], username=f'r{country}{i}',
        role='rider', email=f'r{i}@example.test')
    Rider.objects.create(user_id=u)
    return u


def _driver(vt, country='IN'):
    i = next(_n)
    cc = COUNTRIES[country].calling_code
    u = User.objects.create_user(
        phone_number=f'+{cc}8{i:09d}'[:16], username=f'd{country}{i}',
        role='driver')
    d = Driver.objects.create(user_id=u, approved=True, status='online')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number=f'TC{i:05d}')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


def _trip(zone, rider, driver, fare, **kw):
    now = timezone.now()
    # The pickup must be INSIDE the zone it is attributed to. An earlier version
    # of this helper used Hyderabad coordinates for every trip including the US
    # ones, which made the data self-contradictory and masked a real defect.
    point = SF if zone.country == 'US' else HYD
    return Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=_status('completed'),
        zone=zone, pickup_lat=point[0], pickup_long=point[1],
        destination_lat=point[0] + Decimal('0.02'), destination_long=point[1],
        estimated_fare=Decimal(fare), payment_method='cash',
        accepted_at=now, started_at=now, completed_at=now, **kw,
    )


# ===========================================================================
# The country registry
# ===========================================================================

def test_the_registry_describes_both_markets():
    assert is_supported('IN') and is_supported('US')
    assert get_country('in').code == 'IN'          # case-insensitive
    assert get_country('us ').code == 'US'         # whitespace-tolerant


def test_an_unknown_country_falls_back_rather_than_raising():
    """On the request path a 500 is worse than a documented fallback."""
    assert get_country('ZZ').code == 'IN'
    assert get_country(None).code == 'IN'
    assert not is_supported('ZZ')


def test_the_two_markets_differ_in_every_way_that_matters():
    """If any of these matched, the field would not need to exist."""
    assert INDIA.currency != UNITED_STATES.currency
    assert INDIA.tax_mode != UNITED_STATES.tax_mode
    assert INDIA.distance_unit != UNITED_STATES.distance_unit
    assert INDIA.calling_code != UNITED_STATES.calling_code
    assert INDIA.tax_label != UNITED_STATES.tax_label
    assert INDIA.vehicle_documents != UNITED_STATES.vehicle_documents


def test_the_us_has_no_default_tax_rate_and_india_does():
    """There is no national US sales-tax rate, so guessing one is wrong.

    This is the fix for a real defect: the receipt used a literal 5.00 -- India's
    GST rate -- for any trip whose rate card could not be found.
    """
    assert Decimal(INDIA.default_tax_percent) == Decimal('5.00')
    assert Decimal(UNITED_STATES.default_tax_percent) == Decimal('0.00')


def test_the_us_vehicle_documents_do_not_ask_for_indian_certificates():
    """A US driver must never be asked for a Pollution Under Control certificate."""
    assert 'puc' not in UNITED_STATES.vehicle_documents
    assert 'permit' not in UNITED_STATES.vehicle_documents
    assert 'fitness' not in UNITED_STATES.vehicle_documents
    assert 'registration' in UNITED_STATES.vehicle_documents
    # India keeps all four.
    for doc in ('rc', 'puc', 'permit', 'fitness'):
        assert doc in INDIA.vehicle_documents


# ===========================================================================
# Phone normalisation — the defect that locked a US operator out
# ===========================================================================

def test_a_bare_us_number_does_not_become_an_indian_one():
    """The defect. `f"+91{phone}"` turned 5551234567 into +915551234567."""
    assert normalize_phone('5551234567', default_country='US') == '+15551234567'


def test_a_bare_indian_number_still_works():
    assert normalize_phone('9876543210', default_country='IN') == '+919876543210'


@pytest.mark.parametrize('raw,expected', [
    ('+1 (555) 123-4567', '+15551234567'),
    ('+1-555-123-4567', '+15551234567'),
    ('+91 98765 43210', '+919876543210'),
    ('+919876543210', '+919876543210'),
    ('15551234567', '+15551234567'),
    ('919876543210', '+919876543210'),
])
def test_every_shape_a_human_types_normalises(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize('raw', ['', None, '12', 'abc', '+', '1', '0'])
def test_nonsense_is_refused_rather_than_guessed(raw):
    """A guess here is an account nobody can find again."""
    assert normalize_phone(raw) is None


def test_an_unsupported_country_code_is_refused():
    """+44 is a real number and not a market we serve. Storing it would create
    an account that no normalisation path can reproduce."""
    assert normalize_phone('+447911123456') is None


def test_a_number_identifies_its_country():
    assert country_of_phone('+919876543210').code == 'IN'
    assert country_of_phone('+15551234567').code == 'US'
    assert country_of_phone('+447911123456') is None


# ===========================================================================
# Log redaction — the live privacy defect
# ===========================================================================

def test_a_us_phone_number_is_redacted_from_logs(caplog):
    """It was not. The filter matched `+91` only, so a US number went out clear.

    Asserted through the real filter on a real record, not by testing the regex:
    the regex was never the thing that was wrong.
    """
    from base.logging_filters import PIIRedactionFilter

    record = logging.LogRecord(
        name='t', level=logging.INFO, pathname='t', lineno=1,
        msg='rider +15551234567 requested a ride', args=(), exc_info=None,
    )
    PIIRedactionFilter().filter(record)

    assert '5551234567' not in record.msg, (
        f'a US phone number survived redaction: {record.msg!r}'
    )
    assert '+1555' in record.msg, (
        'the country code and prefix should survive so a log line is still '
        f'useful for debugging: {record.msg!r}'
    )


def test_an_indian_number_is_still_redacted():
    """The behaviour that already existed must not regress."""
    from base.logging_filters import PIIRedactionFilter

    record = logging.LogRecord(
        name='t', level=logging.INFO, pathname='t', lineno=1,
        msg='driver +919876543210 online', args=(), exc_info=None,
    )
    PIIRedactionFilter().filter(record)

    assert '9876543210' not in record.msg
    assert '+9198' in record.msg


def test_redaction_does_not_mangle_short_numerics():
    """A fare, a trip id or a version must survive untouched."""
    from base.logging_filters import PIIRedactionFilter

    record = logging.LogRecord(
        name='t', level=logging.INFO, pathname='t', lineno=1,
        msg='trip 1234 fare +190 version +7', args=(), exc_info=None,
    )
    PIIRedactionFilter().filter(record)

    assert '1234' in record.msg and '190' in record.msg


# ===========================================================================
# Zones inherit their market from the country
# ===========================================================================

def test_an_india_zone_inherits_indias_conventions(india_zone):
    assert india_zone.currency == 'INR'
    assert india_zone.tax_mode == 'inclusive'
    assert india_zone.distance_unit == 'km'
    assert india_zone.timezone_name == 'Asia/Kolkata'


def test_a_us_zone_inherits_americas_conventions(us_zone):
    assert us_zone.currency == 'USD'
    assert us_zone.tax_mode == 'additive'
    assert us_zone.distance_unit == 'mi'


def test_an_explicit_value_always_beats_the_country_default():
    """A zone must be able to say "no, this one is different"."""
    zone = ServiceZone.objects.create(
        code='T-US-ALT', name='Alt', country='US', zone_type='city',
        polygon_geojson=SF_POLYGON, currency='USD', tax_mode='inclusive',
        distance_unit='km', timezone_name='America/Chicago',
    )
    assert zone.tax_mode == 'inclusive'
    assert zone.distance_unit == 'km'
    assert zone.timezone_name == 'America/Chicago'


def test_inheritance_is_idempotent(india_zone):
    """Re-saving must not flip a value back and forth."""
    india_zone.save()
    india_zone.refresh_from_db()
    assert india_zone.currency == 'INR'
    assert india_zone.tax_mode == 'inclusive'


# ===========================================================================
# Currency is stamped on every money row
# ===========================================================================

def test_a_trip_is_stamped_from_its_zone(us_zone, vehicle_type):
    trip = _trip(us_zone, _rider('US'), _driver(vehicle_type, 'US'), '30.83')
    assert trip.currency == 'USD'


def test_an_india_trip_is_still_inr(india_zone, vehicle_type):
    trip = _trip(india_zone, _rider(), _driver(vehicle_type), '190.00')
    assert trip.currency == 'INR'


def test_a_receipt_inherits_the_trips_currency(us_zone, vehicle_type):
    from unittest import mock

    from servers.ride.receipts import issue_receipt

    _card(us_zone, vehicle_type, '2.50', '1.75', '0.35', '8.00', '8.625')
    trip = _trip(us_zone, _rider('US'), _driver(vehicle_type, 'US'), '30.83')
    with mock.patch('servers.ride.receipts._send_receipt_email',
                    return_value=(True, '')), \
            mock.patch('servers.ride.receipts._attach_pdf_to_receipt'):
        receipt = issue_receipt(trip)
    assert receipt.currency == 'USD'


def test_an_explicit_currency_is_not_overwritten(india_zone, vehicle_type):
    """A correction or a data migration must be able to set it."""
    trip = _trip(india_zone, _rider(), _driver(vehicle_type), '100.00',
                 currency='USD')
    assert trip.currency == 'USD'


def test_a_narrow_save_is_not_widened_when_the_stamp_does_not_change(
    india_zone, vehicle_type,
):
    """The reason the mixin does not just add 'currency' to every update_fields.

    `auto_cancel_trip` passes `update_fields` precisely so a stale in-memory
    instance cannot overwrite columns it did not intend to touch. If the stamp
    widened every narrow write, currency would become the one column that always
    gets overwritten from whatever the stale instance held.
    """
    trip = _trip(india_zone, _rider(), _driver(vehicle_type), '100.00')
    captured = {}
    original = type(trip).__mro__[1].save

    def spy(self, *args, **kwargs):
        captured['update_fields'] = kwargs.get('update_fields')
        return original(self, *args, **kwargs)

    from unittest import mock
    with mock.patch.object(type(trip).__mro__[1], 'save', spy):
        trip.save(update_fields=['estimated_fare_minor'])

    assert captured['update_fields'] == ['estimated_fare_minor'], (
        f'a narrow save was widened to {captured["update_fields"]}'
    )


def test_a_wallet_transaction_follows_the_trip_it_references(
    us_zone, vehicle_type,
):
    """`WalletTransaction` has no trip FK -- only `reference_id = 'TRIP_<id>'`."""
    rider = _rider('US')
    trip = _trip(us_zone, rider, _driver(vehicle_type, 'US'), '30.83')
    txn = WalletTransaction.objects.create(
        user_id=rider, amount=Decimal('6.17'), txn_type='debit',
        status='completed', purpose='trip_commission',
        reference_id=f'TRIP_{trip.id}', idempotency_key=f'k{trip.id}',
    )
    assert txn.currency == 'USD', (
        'a settlement row for a US trip joined the Indian totals'
    )


def test_a_wallet_transaction_with_no_trip_uses_the_wallet(us_zone):
    """A top-up or a promo credit has no trip to ask."""
    rider = _rider('US')
    Wallet.objects.create(user_id=rider, balance=Decimal('0.00'),
                          currency='USD')
    txn = WalletTransaction.objects.create(
        user_id=rider, amount=Decimal('25.00'), txn_type='credit',
        status='completed', purpose='topup', idempotency_key='t-us-1',
    )
    assert txn.currency == 'USD'


# ===========================================================================
# Tax mode — inclusive vs additive
# ===========================================================================

def test_india_tax_is_inside_the_quoted_fare(india_zone, vehicle_type):
    """India's GST is levied on the fare the rider is quoted, so the quote adds
    nothing on top."""
    _card(india_zone, vehicle_type, '30', '12', '2', '50', '5.00')
    q = quote_fare(pickup_lat=HYD[0], pickup_lon=HYD[1],
                   distance_km=Decimal('10'), duration_min=Decimal('20'),
                   vehicle_type='sedan', record_demand=False)
    assert q['tax_mode'] == 'inclusive'
    # Reported, not added. The quote states the GST sitting inside the fare --
    # 190.00 * 5 / 105 -- so a rider is not shown "GST 0.00" beside a 5% rate,
    # and the total is byte-for-byte the pre-multi-country number.
    assert q['tax_amount'] == Decimal('9.05')
    assert q['total_fare'] == Decimal('190.00')


def test_reporting_indias_inclusive_tax_does_not_add_it(india_zone, vehicle_type):
    """The number the rider pays must not move because we started stating the
    tax inside it. Inclusive extraction is display arithmetic only."""
    _card(india_zone, vehicle_type, '30', '12', '2', '50', '5.00')
    q = quote_fare(pickup_lat=HYD[0], pickup_lon=HYD[1],
                   distance_km=Decimal('10'), duration_min=Decimal('20'),
                   vehicle_type='sedan', record_demand=False)
    metered = q['base_fare'] + q['distance_fare'] + q['time_fare']
    adjustments = sum((a['amount'] for a in q['fare_adjustments']), Decimal('0'))
    assert q['total_fare'] == metered + adjustments
    # Specifically: the tax is NOT one of those adjustment lines in India.
    assert 'tax' not in {a['code'] for a in q['fare_adjustments']}


def test_us_tax_is_added_on_top(us_zone, vehicle_type):
    _card(us_zone, vehicle_type, '2.50', '1.75', '0.35', '8.00', '8.625')
    q = quote_fare(pickup_lat=SF[0], pickup_lon=SF[1],
                   distance_km=Decimal('10'), duration_min=Decimal('20'),
                   vehicle_type='sedan', record_demand=False)
    assert q['tax_mode'] == 'additive'
    assert q['tax_amount'] > Decimal('0.00')
    # 2.50 + 17.50 + 7.00 = 27.00 metered, + 8.625% tax
    assert q['total_fare'] == Decimal('27.00') + q['tax_amount']


def test_a_flat_per_trip_fee_is_charged_and_named(us_zone, vehicle_type):
    """Several US jurisdictions levy a fixed amount per trip, not a percentage."""
    us_zone.per_trip_fee = Decimal('1.50')
    us_zone.per_trip_fee_label = 'City access fee'
    us_zone.save()
    _card(us_zone, vehicle_type, '2.50', '1.75', '0.35', '8.00', '8.625')

    q = quote_fare(pickup_lat=SF[0], pickup_lon=SF[1],
                   distance_km=Decimal('10'), duration_min=Decimal('20'),
                   vehicle_type='sedan', record_demand=False)

    fee = next(a for a in q['fare_adjustments'] if a['code'] == 'per_trip_fee')
    assert fee['amount'] == Decimal('1.50')
    assert fee['label'] == 'City access fee'
    assert q['total_fare'] == Decimal('27.00') + q['tax_amount'] + Decimal('1.50')


@pytest.mark.parametrize('zone_fixture,point,card', [
    ('india_zone', HYD, ('30', '12', '2', '50', '5.00')),
    ('us_zone', SF, ('2.50', '1.75', '0.35', '8.00', '8.625')),
])
def test_the_decomposition_closes_exactly_in_both_markets(
    request, zone_fixture, point, card, vehicle_type,
):
    """The invariant a rider can check by adding up their receipt."""
    zone = request.getfixturevalue(zone_fixture)
    _card(zone, vehicle_type, *card)
    q = quote_fare(pickup_lat=point[0], pickup_lon=point[1],
                   distance_km=Decimal('7.3'), duration_min=Decimal('17'),
                   vehicle_type='sedan', record_demand=False)

    components = q['base_fare'] + q['distance_fare'] + q['time_fare']
    adjustments = sum((a['amount'] for a in q['fare_adjustments']),
                      Decimal('0.00'))
    assert components + adjustments == q['total_fare'], (
        f'{components} + {adjustments} != {q["total_fare"]}'
    )


def test_the_receipt_taxes_at_the_rate_the_quote_charged(us_zone, vehicle_type):
    """The check that found the real defect in this work.

    The quote charged 8.625% and the receipt recomputed at 5% -- India's rate --
    because the receipt's rate-card lookup returned None and fell through to a
    hardcoded `Decimal('5.00')`. Every other assertion passed. Nothing compared
    the two numbers a rider would compare.
    """
    from unittest import mock

    from servers.ride.receipts import issue_receipt

    _card(us_zone, vehicle_type, '2.50', '1.75', '0.35', '8.00', '8.625')
    q = quote_fare(pickup_lat=SF[0], pickup_lon=SF[1],
                   distance_km=Decimal('10'), duration_min=Decimal('20'),
                   vehicle_type='sedan', record_demand=False)
    trip = _trip(us_zone, _rider('US'), _driver(vehicle_type, 'US'),
                 str(q['total_fare']))

    with mock.patch('servers.ride.receipts._send_receipt_email',
                    return_value=(True, '')), \
            mock.patch('servers.ride.receipts._attach_pdf_to_receipt'):
        receipt = issue_receipt(trip)

    assert abs(receipt.gst_amount - q['tax_amount']) <= Decimal('0.01'), (
        f'quote charged {q["tax_amount"]} of tax, receipt says '
        f'{receipt.gst_amount} -- the receipt is using a different rate'
    )


def test_a_missing_rate_card_does_not_tax_a_us_trip_at_indias_rate(
    us_zone, vehicle_type,
):
    """The fallback itself. No card anywhere in the zone."""
    from unittest import mock

    from servers.ride.receipts import issue_receipt

    trip = _trip(us_zone, _rider('US'), _driver(vehicle_type, 'US'), '30.00')
    with mock.patch('servers.ride.receipts._send_receipt_email',
                    return_value=(True, '')), \
            mock.patch('servers.ride.receipts._attach_pdf_to_receipt'):
        receipt = issue_receipt(trip)

    # 5% of 30.00 would be ~1.43. The US default is 0.00, because there is no
    # national rate to guess.
    assert receipt.gst_amount == Decimal('0.00'), (
        f'a US trip with no rate card was taxed {receipt.gst_amount}, which '
        f'means a rate was invented for a jurisdiction nobody declared'
    )


# ===========================================================================
# Receipts say the right things
# ===========================================================================

@pytest.mark.parametrize('zone_fixture,country,symbol,wrong,label,unit', [
    ('india_zone', 'IN', '₹', '$', 'GST', 'km'),
    ('us_zone', 'US', '$', '₹', 'Sales tax', 'mi'),
])
def test_the_receipt_speaks_the_markets_language(
    request, zone_fixture, country, symbol, wrong, label, unit, vehicle_type,
):
    from unittest import mock

    from servers.ride.receipts import issue_receipt

    zone = request.getfixturevalue(zone_fixture)
    _card(zone, vehicle_type, '30', '12', '2', '50', '5.00')
    trip = _trip(zone, _rider(country), _driver(vehicle_type, country),
                 '190.00', actual_distance_km=Decimal('10'))

    with mock.patch('servers.ride.receipts._send_receipt_email',
                    return_value=(True, '')), \
            mock.patch('servers.ride.receipts._attach_pdf_to_receipt'):
        receipt = issue_receipt(trip)

    html = receipt.html_body
    assert symbol in html
    assert wrong not in html, f'the other market\'s symbol leaked in: {wrong}'
    assert label in html
    assert f' {unit}' in html
    assert 'Rs.' not in html, 'the hardcoded rupee abbreviation is still there'


def test_distance_converts_for_display_but_is_stored_metric():
    """Storage stays metric so distances remain comparable across markets."""
    from servers.ride.receipts import format_distance

    assert format_distance(Decimal('10'), 'km') == '10.00 km'
    # 10 km is 6.21 miles.
    assert format_distance(Decimal('10'), 'mi') == '6.21 mi'
    assert format_distance(None, 'mi') == '—'


# ===========================================================================
# Aggregates refuse to lie
# ===========================================================================

def test_a_cross_market_total_comes_back_as_two_numbers(
    india_zone, us_zone, vehicle_type,
):
    """The reason the currency column exists.

    A single `Sum()` here would add dollars to rupees and present the result as
    money, and nothing would say so.
    """
    _trip(india_zone, _rider(), _driver(vehicle_type), '190.00')
    _trip(us_zone, _rider('US'), _driver(vehicle_type, 'US'), '30.83')

    totals = sum_by_currency(Trip.objects.all(), 'estimated_fare_minor')

    assert set(totals) == {'INR', 'USD'}
    assert totals['INR'] == Decimal('190.00')
    assert totals['USD'] == Decimal('30.83')


def test_a_single_market_total_is_still_one_number(india_zone, vehicle_type):
    """A single-market deployment must not be made harder to read."""
    _trip(india_zone, _rider(), _driver(vehicle_type), '100.00')
    _trip(india_zone, _rider(), _driver(vehicle_type), '150.00')

    totals = sum_by_currency(Trip.objects.all(), 'estimated_fare_minor')

    assert totals == {'INR': Decimal('250.00')}
    assert single_currency_or_none(Trip.objects.all()) == 'INR'


def test_a_mixed_queryset_has_no_single_currency(india_zone, us_zone,
                                                 vehicle_type):
    """So a view that shows one number knows it is not entitled to."""
    _trip(india_zone, _rider(), _driver(vehicle_type), '100.00')
    _trip(us_zone, _rider('US'), _driver(vehicle_type, 'US'), '20.00')

    assert single_currency_or_none(Trip.objects.all()) is None


def test_totals_render_as_separate_amounts_for_an_operator():
    out = format_totals({'INR': Decimal('190.00'), 'USD': Decimal('30.83')})
    assert '₹190.00' in out and '$30.83' in out
    assert '220' not in out, 'the two currencies were added together'


# ===========================================================================
# Money formatting and rounding
# ===========================================================================

def test_money_formats_in_its_own_currency():
    assert format_money(Decimal('190'), 'INR') == '₹190.00'
    assert format_money(Decimal('30.5'), 'USD') == '$30.50'
    assert format_money(Decimal('1234567.891'), 'USD') == '$1,234,567.89'


def test_an_unsupported_currency_falls_back_loudly(caplog):
    with caplog.at_level(logging.ERROR):
        assert normalize_currency('XYZ') == DEFAULT_CURRENCY
    assert 'unsupported currency' in caplog.text


def test_rounding_is_half_up_as_every_fare_table_assumes():
    """Banker's rounding here would move prices."""
    assert quantize(Decimal('1.005'), 'USD') == Decimal('1.01')
    assert quantize(Decimal('2.675'), 'INR') == Decimal('2.68')


# ===========================================================================
# "Today" belongs to the market, not the deployment
# ===========================================================================

def test_each_market_measures_its_own_day(india_zone, us_zone):
    """A driver in New York asking what they earned today used to get a window
    that opened at 14:30 the previous afternoon."""
    in_start = local_day_start(zone_timezone(india_zone))
    us_start = local_day_start(zone_timezone(us_zone))

    assert in_start.utcoffset() != us_start.utcoffset()
    assert in_start.hour == 0 and us_start.hour == 0
    assert in_start != us_start


def test_a_zone_with_a_broken_timezone_does_not_crash_an_earnings_screen(
    india_zone, caplog,
):
    """A typo'd IANA name must degrade, not 500."""
    ServiceZone.objects.filter(pk=india_zone.pk).update(
        timezone_name='Not/AReal_Zone')
    india_zone.refresh_from_db()

    with caplog.at_level(logging.ERROR):
        start = local_day_start(zone_timezone(india_zone))

    assert start is not None
    assert 'invalid timezone_name' in caplog.text


def test_a_us_zone_keeps_the_timezone_it_was_given(us_zone):
    """The US has six, so the country default must never silently win."""
    assert us_zone.timezone_name == 'America/Los_Angeles'
    assert str(zone_timezone(us_zone)) == 'America/Los_Angeles'


# ===========================================================================
# India must not have moved. This is the half that matters.
# ===========================================================================

def test_india_prices_exactly_what_it_priced_before(india_zone, vehicle_type):
    """Pinned to the arithmetic, not to a fixture, so a repricing shows up here.

    30 base + (12 x 10 km) + (2 x 20 min) = 190.00, GST inside.
    """
    _card(india_zone, vehicle_type, '30', '12', '2', '50', '5.00')
    q = quote_fare(pickup_lat=HYD[0], pickup_lon=HYD[1],
                   distance_km=Decimal('10'), duration_min=Decimal('20'),
                   vehicle_type='sedan', record_demand=False)

    assert q['total_fare'] == Decimal('190.00')
    assert q['base_fare'] == Decimal('30.00')
    assert q['distance_fare'] == Decimal('120.00')
    assert q['time_fare'] == Decimal('40.00')
    # The guard against India gaining an ADDITIVE tax is the absence of the
    # line, not a zero amount: `tax_amount` now reports the GST already inside
    # the 190.00. An additive tax would appear here AND move the total above.
    assert 'tax' not in {a['code'] for a in q['fare_adjustments']}, (
        'India gained an additive tax line, which would double-charge GST'
    )
    assert 'per_trip_fee' not in {a['code'] for a in q['fare_adjustments']}


def test_the_india_minimum_fare_still_applies(india_zone, vehicle_type):
    _card(india_zone, vehicle_type, '30', '12', '2', '50', '5.00')
    q = quote_fare(pickup_lat=HYD[0], pickup_lon=HYD[1],
                   distance_km=Decimal('0.1'), duration_min=Decimal('1'),
                   vehicle_type='sedan', record_demand=False)

    assert q['min_fare_applied'] is True
    assert q['total_fare'] == Decimal('50.00')


def test_final_fare_is_still_null_in_both_markets(india_zone, us_zone,
                                                  vehicle_type):
    """The financial boundary, unchanged by any of this."""
    t1 = _trip(india_zone, _rider(), _driver(vehicle_type), '190.00')
    t2 = _trip(us_zone, _rider('US'), _driver(vehicle_type, 'US'), '30.83')

    assert t1.final_fare is None and t2.final_fare is None


# ===========================================================================
# Driver compliance — the blocker that made US approval impossible
# ===========================================================================

def test_a_us_driver_is_not_asked_for_indian_certificates(vehicle_type):
    """The blocker. The console required all five Indian documents of everyone.

    `permit`, `fitness` and `puc` have no US equivalent -- a US driver cannot
    obtain a Pollution Under Control certificate -- so `all()` could never be
    true and a US driver could never be approved. Not a confusing error: no
    possible path to approval.
    """
    from servers.driver.compliance import required_vehicle_documents

    us_driver = _driver(vehicle_type, 'US')
    required = required_vehicle_documents(us_driver)

    for indian_only in ('puc', 'permit', 'fitness'):
        assert indian_only not in required, (
            f'a US driver is still being asked for {indian_only}'
        )
    assert 'registration' in required and 'insurance' in required


def test_an_indian_driver_still_needs_all_five(vehicle_type):
    """India's requirements must not have been relaxed by this."""
    from servers.driver.compliance import required_vehicle_documents

    required = required_vehicle_documents(_driver(vehicle_type, 'IN'))

    for doc in ('rc', 'insurance', 'permit', 'fitness', 'puc'):
        assert doc in required, f'India stopped requiring {doc}'


def test_a_drivers_country_is_read_from_their_phone_number(vehicle_type):
    """The only signal available before they have taken a trip."""
    from servers.driver.compliance import driver_country_code

    assert driver_country_code(_driver(vehicle_type, 'US')) == 'US'
    assert driver_country_code(_driver(vehicle_type, 'IN')) == 'IN'


def test_a_us_vehicle_without_its_inspection_is_not_compliant(vehicle_type):
    """Registration and insurance are not enough. The inspection is required,
    it now HAS a column (`inspection_doc`), and so missing it reads as missing
    rather than as a platform gap."""
    from servers.driver.compliance import (
        evaluate_vehicle, unrecordable_documents, vehicle_is_compliant,
    )

    us_driver = _driver(vehicle_type, 'US')
    vehicle = us_driver.active_vehicle
    vehicle.rc_doc = 'docs/reg.pdf'
    vehicle.insurance_doc = 'docs/ins.pdf'
    vehicle.save(update_fields=['rc_doc', 'insurance_doc'])

    checks = evaluate_vehicle(vehicle, us_driver)
    assert checks['registration']['present'] is True
    assert checks['insurance']['present'] is True
    assert checks['inspection']['applicable'] is True
    assert checks['inspection']['present'] is False
    assert vehicle_is_compliant(vehicle, us_driver) is False
    # Nothing is unrecordable any more. This assertion is the one that will
    # fail first if a future country adds a document type with no column --
    # which is the point: it must block visibly, not pass silently.
    assert unrecordable_documents(us_driver) == ()


def test_a_fully_documented_us_vehicle_is_compliant(vehicle_type):
    """The end of the blocker: there is now a reachable True for the US."""
    from servers.driver.compliance import vehicle_is_compliant

    us_driver = _driver(vehicle_type, 'US')
    vehicle = us_driver.active_vehicle
    vehicle.rc_doc = 'docs/reg.pdf'
    vehicle.insurance_doc = 'docs/ins.pdf'
    vehicle.inspection_doc = 'docs/inspection.pdf'
    vehicle.save(update_fields=['rc_doc', 'insurance_doc', 'inspection_doc'])

    assert vehicle_is_compliant(vehicle, us_driver) is True


def test_an_indian_vehicle_with_all_five_is_compliant(vehicle_type):
    """The control: India's approval path must still reach True."""
    from servers.driver.compliance import vehicle_is_compliant

    d = _driver(vehicle_type, 'IN')
    v = d.active_vehicle
    v.rc_doc = 'd/rc.pdf'
    v.insurance_doc = 'd/ins.pdf'
    v.permit_doc = 'd/permit.pdf'
    v.fitness_doc = 'd/fit.pdf'
    v.puc_doc = 'd/puc.pdf'
    v.save()

    assert vehicle_is_compliant(v, d) is True


def test_a_missing_document_is_named_for_the_operator(vehicle_type):
    """An operator needs to know WHICH document, not just that one is missing."""
    from servers.driver.compliance import missing_documents

    d = _driver(vehicle_type, 'IN')
    v = d.active_vehicle
    v.rc_doc = 'd/rc.pdf'
    v.save(update_fields=['rc_doc'])

    missing = missing_documents(v, d)
    assert 'rc' not in missing
    assert 'puc' in missing and 'insurance' in missing


# ===========================================================================
# Payout minimums and messages speak the driver's currency
# ===========================================================================


def _configure_payouts(*countries):
    """Payout rules are per-country configuration; a country with none cannot
    withdraw. India is what `seed_payout_config` writes. The US row is a
    HYPOTHETICAL owner decision ($10 minimum), here only so these tests keep
    exercising the market-aware message and minimum logic they were written for.
    """
    from finance_world import seed_in_payout_config
    from servers.payments.models import PayoutCountryConfig

    if 'IN' in countries:
        seed_in_payout_config()
    if 'US' in countries:
        PayoutCountryConfig.objects.create(
            country='US', fee_percent=Decimal('2.00'), minimum_withdrawal_minor=1000)


def test_a_us_driver_is_not_told_their_minimum_is_500_rupees(vehicle_type):
    """The message said "Minimum withdrawal amount is ₹500" to everyone.

    A US driver was shown a rupee sign beside a dollar balance and blocked from
    withdrawing anything under $500 -- which is not a minimum, it is a barrier.
    """
    _configure_payouts('US')
    from servers.driver.services import (
        minimum_withdrawal_for, validate_withdrawal_amount,
    )

    d = _driver(vehicle_type, 'US')
    Wallet.objects.create(user_id=d.user_id, balance=Decimal('100.00'),
                          currency='USD')

    assert minimum_withdrawal_for(d) == Decimal('10')

    ok, msg = validate_withdrawal_amount(d, Decimal('5'))
    assert ok is False
    assert '$10.00' in msg, f'message was {msg!r}'
    assert '₹' not in msg, f'a rupee sign reached a US driver: {msg!r}'


def test_the_minimum_is_no_longer_what_blocks_a_us_driver(vehicle_type):
    """The control, stated precisely.

    `calculate_available_balance` derives from the settlement ledger rather than
    from the wallet row, so a $25 request on a seeded wallet is still refused --
    for lack of balance. What matters is that it is refused for THAT reason and
    not because $25 is under a 500-rupee minimum, which is what used to happen.
    """
    _configure_payouts('US')
    from servers.driver.services import validate_withdrawal_amount

    d = _driver(vehicle_type, 'US')
    Wallet.objects.create(user_id=d.user_id, balance=Decimal('100.00'),
                          currency='USD')

    ok, msg = validate_withdrawal_amount(d, Decimal('25'))

    assert 'Minimum withdrawal' not in msg, (
        f'$25 was blocked by a minimum threshold: {msg!r}'
    )
    assert 'Insufficient balance' in msg
    assert '$' in msg and '₹' not in msg


def test_an_indian_driver_still_has_a_500_rupee_minimum(vehicle_type):
    """India's threshold must not have moved."""
    _configure_payouts('IN')
    from servers.driver.services import (
        minimum_withdrawal_for, validate_withdrawal_amount,
    )

    d = _driver(vehicle_type, 'IN')
    Wallet.objects.create(user_id=d.user_id, balance=Decimal('1000.00'),
                          currency='INR')

    assert minimum_withdrawal_for(d) == Decimal('500')
    ok, msg = validate_withdrawal_amount(d, Decimal('100'))
    assert ok is False
    assert '₹500.00' in msg, f'message was {msg!r}'


def test_an_insufficient_balance_message_uses_the_right_symbol(vehicle_type):
    _configure_payouts('US')
    from servers.driver.services import validate_withdrawal_amount

    d = _driver(vehicle_type, 'US')
    Wallet.objects.create(user_id=d.user_id, balance=Decimal('15.00'),
                          currency='USD')

    ok, msg = validate_withdrawal_amount(d, Decimal('50'))
    assert ok is False
    assert '$' in msg and '₹' not in msg, f'message was {msg!r}'


# ===========================================================================
# The operator console renders each row in its own currency
# ===========================================================================

def test_the_money_filter_renders_each_row_in_its_own_currency():
    """The console had ~40 hardcoded rupee signs across 11 templates.

    Replacing them with dollars where a US page needs it would be the same
    mistake twice: the symbol belongs to the ROW, because a trip list can show
    an Indian trip and a US trip on the same screen.
    """
    from django.template import Context, Template

    t = Template('{% load money_tags %}{{ a|money:c1 }} and {{ b|money:c2 }}')
    out = t.render(Context({'a': Decimal('190'), 'c1': 'INR',
                            'b': Decimal('30.83'), 'c2': 'USD'}))

    assert out == '₹190.00 and $30.83', out


def test_the_money_filter_shows_an_em_dash_rather_than_a_zero_fare():
    """A blank fare is not a free ride, and printing 0.00 says it was."""
    from django.template import Context, Template

    t = Template('{% load money_tags %}{{ a|money:"USD" }}')
    assert t.render(Context({'a': None})) == '—'


def test_the_money_filter_never_raises_into_a_page():
    """A filter that throws blanks the whole page an operator is reading."""
    from django.template import Context, Template

    t = Template('{% load money_tags %}{{ a|money:"USD" }}')
    out = t.render(Context({'a': object()}))
    assert out, 'the filter produced nothing instead of degrading'


def test_totals_render_as_two_amounts_never_a_sum():
    """The defect the currency column exists to prevent, at the last moment."""
    from django.template import Context, Template

    t = Template('{% load money_tags %}{{ totals|money_totals }}')
    out = t.render(Context({'totals': {'INR': Decimal('190.00'),
                                       'USD': Decimal('30.83')}}))

    assert '₹190.00' in out and '$30.83' in out
    assert '220' not in out, 'rupees were added to dollars'


def test_no_console_template_hardcodes_a_currency_symbol():
    """A regression lock. Re-introducing one is easy and invisible in review."""
    import pathlib

    offenders = []
    for path in pathlib.Path('templates/admin_pages').glob('*.html'):
        text = path.read_text(encoding='utf-8')
        if '₹' in text or 'Rs.' in text:
            offenders.append(path.name)

    assert not offenders, (
        f'these console templates hardcode a currency: {offenders}. '
        f'Use {{{{ amount|money:row.currency }}}} so the symbol follows the row.'
    )


def test_every_console_template_still_compiles():
    """The money filter is registered via TEMPLATES OPTIONS libraries, because
    `servers.admin_dashboard` is deliberately not an installed app. If that
    registration is lost, every page that formats money raises at render time
    and blanks itself."""
    import pathlib

    from django.template.loader import get_template

    broken = []
    for path in sorted(pathlib.Path('templates/admin_pages').glob('*.html')):
        try:
            get_template(f'admin_pages/{path.name}')
        except Exception as exc:  # noqa: BLE001
            broken.append(f'{path.name}: {exc}')

    assert not broken, f'templates failed to compile: {broken}'
