"""The Decimal -> minor-units migrations, run for real over seeded rows.

Migrates the five apps back to before the change, writes money rows through
the HISTORICAL models (the Decimal columns as they were), migrates forwards,
and checks every value converted exactly. Then proves the verification step
refuses a value that does not match, and that the migrations reverse to the
original Decimals.

Runs on SQLite by default. `pytest -m postgres` runs the same class against
PostgreSQL, where each migration is its own transaction and DDL after an
UPDATE on the same table is refused ("pending trigger events"). That is why
each app has three migrations -- add (schema), convert (data), drop (schema)
-- and this class running there is what proves the split holds in both
directions; SQLite would never have noticed.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from base import money_migration as mm

BEFORE = [
    ('ride', '0017_multi_country_market_fields'),
    ('payments', '0012_multi_country_market_fields'),
    ('driver', '0027_backfill_driver_home_city'),
    ('rider', '0012_multi_country_market_fields'),
    ('pricing', '0010_link_zones_to_geo_nodes'),
]
# Conversion done, Decimal columns still present: where verification can be
# shown refusing a mismatch.
MIDWAY = [
    ('ride', '0019_money_minor_units_convert'),
    ('payments', '0014_money_minor_units_convert'),
    ('driver', '0029_money_minor_units_convert'),
    ('rider', '0014_money_minor_units_convert'),
    ('pricing', '0012_money_minor_units_convert'),
]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


def _latest():
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    return [node for node in executor.loader.graph.leaf_nodes()]


SEEDED_MODELS = [
    ('ride', 'Trip'), ('ride', 'FarePricing'), ('ride', 'Receipt'),
    ('ride', 'PromoCode'), ('ride', 'PromoRedemption'), ('payments', 'Payment'),
    ('payments', 'TransactionHistory'), ('payments', 'TripSettlement'),
    ('driver', 'WithdrawalRequest'), ('driver', 'IncentiveQuest'),
    ('rider', 'Wallet'), ('rider', 'WalletTransaction'), ('pricing', 'TripFareShadow'),
]


def _require_a_clean_database(apps):
    """Fail loudly, by name, if another test's rows are still here.

    The assertions below look rows up with `.get()` and the seed uses fixed
    unique values, both of which assume nothing else is in these tables. Under
    a teardown that failed to flush -- as every transactional PostgreSQL test
    did while the audit log refused TRUNCATE -- leftovers made this test fail
    with a MultipleObjectsReturned or an IntegrityError that pointed at the
    wrong thing. This names the real cause instead.
    """
    leftovers = {f'{app}.{model}': n for app, model in SEEDED_MODELS
                 if (n := apps.get_model(app, model).objects.count())}
    if leftovers:
        pytest.fail(f'database not clean before seeding (a previous teardown did '
                    f'not flush?): {leftovers}')


def _seed(apps):
    """One row of every converted model, through the pre-migration schema."""
    _require_a_clean_database(apps)
    User = apps.get_model('auth_user', 'CustomUser')
    Driver = apps.get_model('driver', 'Driver')
    Trip = apps.get_model('ride', 'Trip')
    FarePricing = apps.get_model('ride', 'FarePricing')
    Receipt = apps.get_model('ride', 'Receipt')
    PromoCode = apps.get_model('ride', 'PromoCode')
    PromoRedemption = apps.get_model('ride', 'PromoRedemption')
    Payment = apps.get_model('payments', 'Payment')
    TransactionHistory = apps.get_model('payments', 'TransactionHistory')
    TripSettlement = apps.get_model('payments', 'TripSettlement')
    WithdrawalRequest = apps.get_model('driver', 'WithdrawalRequest')
    IncentiveQuest = apps.get_model('driver', 'IncentiveQuest')
    Wallet = apps.get_model('rider', 'Wallet')
    WalletTransaction = apps.get_model('rider', 'WalletTransaction')
    TripStatus = apps.get_model('ride', 'TripStatus')
    TripFareShadow = apps.get_model('pricing', 'TripFareShadow')

    now = timezone.now()
    # People through the CURRENT models -- their tables are not part of this
    # migration, and the historical user model lacks the app's defaults --
    # then re-read as historical instances so the FKs below accept them.
    from django.contrib.auth import get_user_model
    from servers.driver.models import Driver as LiveDriver
    live = get_user_model()
    r = live.objects.create_user(phone_number='+919700088001', role='rider')
    d = live.objects.create_user(phone_number='+919800088001', role='driver')
    rider = User.objects.get(pk=r.pk)
    du = User.objects.get(pk=d.pk)
    driver = Driver.objects.get(pk=LiveDriver.objects.create(user_id=d, approved=True).pk)
    status, _ = TripStatus.objects.get_or_create(status_code='completed')

    usd_trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=status, currency='USD',
        pickup_lat=Decimal('40.7'), pickup_long=Decimal('-73.9'),
        destination_lat=Decimal('40.7'), destination_long=Decimal('-73.9'),
        estimated_fare=Decimal('25.92'), final_fare=None,
        cancellation_fee=Decimal('0.00'))
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=status, currency='INR',
        pickup_lat=Decimal('17.38'), pickup_long=Decimal('78.48'),
        destination_lat=Decimal('17.39'), destination_long=Decimal('78.49'),
        estimated_fare=Decimal('135.68'), final_fare=Decimal('110.18'),
        cancellation_fee=Decimal('49.99'))
    FarePricing.objects.create(
        trip_id=trip, currency='INR', base_fare=Decimal('30.00'),
        distance_fare=Decimal('90.63'), time_fare=Decimal('15.05'),
        total_fare=Decimal('135.68'), gross_fare=Decimal('135.68'),
        discount_amount=Decimal('25.50'), rider_payable=Decimal('110.18'))
    Receipt.objects.create(
        trip_id=trip, user_id=rider, receipt_number='MIG-1', currency='INR',
        total_fare=Decimal('110.18'), gst_amount=Decimal('5.25'))
    flat = PromoCode.objects.create(
        code='MIGFLAT', discount_type='flat', discount_value=Decimal('25.50'),
        min_fare=Decimal('100.00'), valid_from=now, valid_to=now)
    PromoCode.objects.create(
        code='MIGPCT', discount_type='percent', discount_value=Decimal('15.00'),
        max_discount_amount=Decimal('40.00'), valid_from=now, valid_to=now)
    PromoRedemption.objects.create(
        promo=flat, user=rider, trip=trip, discount_amount=Decimal('25.50'))
    Payment.objects.create(trip_id=trip, user_id=rider, amount=Decimal('110.18'),
                           method='online', currency='INR')
    TransactionHistory.objects.create(
        trip_id=trip, user_id=rider, driver_id=driver, amount=Decimal('90.35'),
        method='online', currency='INR')
    wt = WalletTransaction.objects.create(
        user_id=du, amount=Decimal('90.35'), txn_type='credit', currency='INR',
        idempotency_key='MIG-WT-1')
    TripSettlement.objects.create(
        trip=trip, driver=driver, wallet_transaction=wt, currency='INR',
        gross_fare=Decimal('110.18'), commission_percent=Decimal('18.00'),
        commission_amount=Decimal('19.83'), driver_net=Decimal('90.35'),
        settled_at=now)
    Wallet.objects.create(user_id=du, scope='driver', balance=Decimal('528.28'), currency='INR')
    Wallet.objects.create(user_id=rider, scope='rider', balance=Decimal('0.00'), currency='INR')
    WithdrawalRequest.objects.create(driver=driver, amount=Decimal('500.00'), currency='INR')
    IncentiveQuest.objects.create(
        title='Ten trips', target_trips=10, reward_amount=Decimal('500.00'),
        start_date=now, end_date=now)
    TripFareShadow.objects.create(
        trip=usd_trip, quoted_fare=Decimal('25.92'),
        shadow_estimate=Decimal('25.91'), shadow_actual=None)


# (app, model, lookup, field, expected minor) -- every converted column.
EXPECTED = [
    ('ride', 'Trip', {'currency': 'INR'}, 'estimated_fare', 13568),
    ('ride', 'Trip', {'currency': 'INR'}, 'final_fare', 11018),
    ('ride', 'Trip', {'currency': 'INR'}, 'cancellation_fee', 4999),
    ('ride', 'Trip', {'currency': 'USD'}, 'estimated_fare', 2592),
    ('ride', 'Trip', {'currency': 'USD'}, 'final_fare', None),
    ('ride', 'FarePricing', {}, 'base_fare', 3000),
    ('ride', 'FarePricing', {}, 'distance_fare', 9063),
    ('ride', 'FarePricing', {}, 'time_fare', 1505),
    ('ride', 'FarePricing', {}, 'total_fare', 13568),
    ('ride', 'FarePricing', {}, 'gross_fare', 13568),
    ('ride', 'FarePricing', {}, 'discount_amount', 2550),
    ('ride', 'FarePricing', {}, 'rider_payable', 11018),
    ('ride', 'Receipt', {}, 'total_fare', 11018),
    ('ride', 'Receipt', {}, 'gst_amount', 525),
    ('ride', 'PromoCode', {'code': 'MIGFLAT'}, 'discount_flat', 2550),
    ('ride', 'PromoCode', {'code': 'MIGFLAT'}, 'min_fare', 10000),
    ('ride', 'PromoCode', {'code': 'MIGPCT'}, 'max_discount_amount', 4000),
    ('ride', 'PromoRedemption', {}, 'discount_amount', 2550),
    ('payments', 'Payment', {}, 'amount', 11018),
    ('payments', 'TransactionHistory', {}, 'amount', 9035),
    ('payments', 'TripSettlement', {}, 'gross_fare', 11018),
    ('payments', 'TripSettlement', {}, 'commission_amount', 1983),
    ('payments', 'TripSettlement', {}, 'driver_net', 9035),
    ('driver', 'WithdrawalRequest', {}, 'amount', 50000),
    ('driver', 'IncentiveQuest', {}, 'reward_amount', 50000),
    ('rider', 'Wallet', {'scope': 'driver'}, 'balance', 52828),
    ('rider', 'Wallet', {'scope': 'rider'}, 'balance', 0),
    ('rider', 'WalletTransaction', {}, 'amount', 9035),
    ('pricing', 'TripFareShadow', {}, 'quoted_fare', 2592),
    ('pricing', 'TripFareShadow', {}, 'shadow_estimate', 2591),
    ('pricing', 'TripFareShadow', {}, 'shadow_actual', None),
]


@pytest.mark.django_db(transaction=True)
class TestMoneyMinorUnitsMigration:

    def test_forwards_exact_verified_and_reversible(self):
        latest = _latest()
        try:
            _seed(_migrate(BEFORE))

            apps = _migrate(latest)
            for app, model, lookup, field, minor in EXPECTED:
                row = apps.get_model(app, model).objects.get(**lookup)
                assert getattr(row, f'{field}_minor') == minor, (model, field)

            # The two rows that took a currency they did not have: a promo
            # from its (absent) zone, a shadow from its USD trip.
            PromoCode = apps.get_model('ride', 'PromoCode')
            assert PromoCode.objects.get(code='MIGPCT').discount_percent == Decimal('15.00')
            assert PromoCode.objects.get(code='MIGPCT').discount_flat_minor is None
            assert PromoCode.objects.get(code='MIGFLAT').currency == 'INR'
            assert apps.get_model('pricing', 'TripFareShadow').objects.get().currency == 'USD'

            # And back: every original Decimal restored.
            apps = _migrate(BEFORE)
            Trip = apps.get_model('ride', 'Trip')
            assert Trip.objects.get(currency='INR').estimated_fare == Decimal('135.68')
            assert Trip.objects.get(currency='INR').cancellation_fee == Decimal('49.99')
            assert apps.get_model('ride', 'PromoCode').objects.get(
                code='MIGFLAT').discount_value == Decimal('25.50')
            assert apps.get_model('ride', 'PromoCode').objects.get(
                code='MIGPCT').discount_value == Decimal('15.00')
            assert apps.get_model('rider', 'Wallet').objects.get(
                scope='driver').balance == Decimal('528.28')
            assert apps.get_model('payments', 'TripSettlement').objects.get(
                ).driver_net == Decimal('90.35')
        finally:
            _migrate(latest)

    def test_verification_refuses_a_value_that_does_not_match(self):
        latest = _latest()
        try:
            _seed(_migrate(BEFORE))
            apps = _migrate(MIDWAY)
            Trip = apps.get_model('ride', 'Trip')

            # Both columns exist here. Corrupt one converted value, the way a
            # write that did not land -- or a 100x bug -- would.
            Trip.objects.filter(currency='INR').update(estimated_fare_minor=135)
            with pytest.raises(mm.MoneyMigrationError, match='differ from original'):
                mm._verify(Trip, ['estimated_fare'], 'currency')
        finally:
            _migrate(latest)


@pytest.mark.postgres
class TestMoneyMinorUnitsMigrationOnPostgres(TestMoneyMinorUnitsMigration):
    """The same, against PostgreSQL: `pytest -m postgres`."""


def test_a_value_finer_than_the_minor_unit_is_refused_not_rounded():
    assert mm._exact_minor(Decimal('135.68'), 2, 'x') == 13568
    assert mm._exact_minor(Decimal('500'), 0, 'x') == 500
    assert mm._exact_minor(None, 2, 'x') is None
    with pytest.raises(mm.MoneyMigrationError, match='refusing to round'):
        mm._exact_minor(Decimal('135.685'), 2, 'Trip#1.estimated_fare')
    with pytest.raises(mm.MoneyMigrationError):
        mm._exact_minor(Decimal('0.50'), 0, 'a 0-digit currency')
