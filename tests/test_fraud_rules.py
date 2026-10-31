"""Module 13: the detection rules. Each rule has a positive and a negative case, the flags they
raise dedupe and are idempotent, the beat task converges, and the thresholds change only through
a checker (and bump the version).

Rules only RAISE flags: nothing here (or in the code under test) changes a fare, a payout, a wallet
or a notification.
"""

import dataclasses
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from finance_world import make_payment, status_obj
from fraud_support import (  # noqa: F401
    ADM, block_the_network, body_of, client_for, completed_trip, error_code, flag_url, geo, get,
    hyd_driver, make_flag, make_rider, operator, post, seed_rules, world,
)
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.command_centre.models import OperatorAlert
from servers.fraud import config as rule_config
from servers.fraud import raising, runner
from servers.fraud.models import FlagEvent, RiskFlag, RiskRuleConfig
from servers.fraud.rules import RULES, SCHEDULED
from servers.ride.models import Trip, TripLocationPoint, TripMoneyAction

pytestmark = [pytest.mark.django_db]


@pytest.fixture
def seeded(world):
    seed_rules()
    return world


def thresholds(key, country='IN', **over):
    values = RULES[key].defaults(country)
    values.update(over)
    return values


def run(key, country='IN', **over):
    return RULES[key].evaluate(thresholds(key, country, **over), country, timezone.now())


def trip_where(world, rider, driver, **fields):
    trip = completed_trip(world, rider, driver)
    if fields:
        Trip.objects.filter(pk=trip.pk).update(**fields)
        trip.refresh_from_db()
    return trip


def tokens(*users, token='device-aaaa'):
    for user in users:
        user.fcm_token = token
        user.save(update_fields=['fcm_token'])


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------

class TestTheSeed:
    def test_in_and_us_get_the_standard_defaults_and_a_second_run_writes_nothing(self, world):
        seed_rules()
        rows = RiskRuleConfig.objects.filter(is_active=True)
        assert rows.count() == 2 * len(SCHEDULED)
        assert {r.country for r in rows} == {'IN', 'US'}
        assert all(r.version == 1 and r.enabled for r in rows)
        audits = AdminAuditLog.objects.filter(action='fraud_rules_seeded').count()
        seed_rules()
        assert RiskRuleConfig.objects.count() == 2 * len(SCHEDULED)
        assert AdminAuditLog.objects.filter(action='fraud_rules_seeded').count() == audits

    def test_every_default_sits_inside_its_own_bounds(self):
        for rule in RULES.values():
            for p in rule.params:
                for default in (p.default_in, p.default_us):
                    assert p.low <= default <= p.high, (rule.key, p.key)


# ---------------------------------------------------------------------------
# multi_accounting
# ---------------------------------------------------------------------------

class TestMultiAccounting:
    def test_three_accounts_on_one_handset_is_a_hit_on_the_oldest(self, world):
        riders = [make_rider(f'MA{i}', '91') for i in range(3)]
        tokens(*riders)
        hits = run('multi_accounting')
        assert len(hits) == 1
        hit = hits[0]
        assert hit.subject.pk == riders[0].pk
        assert hit.refs['counterpart_user_ids'] == [riders[1].pk, riders[2].pk]
        assert 'same_device' in hit.signals and hit.measures['accounts'] == 3

    def test_two_accounts_or_different_handsets_are_not(self, world):
        a, b, c = (make_rider(f'MN{i}', '91') for i in range(3))
        tokens(a, b)                                    # only two share
        c.fcm_token = 'device-cccc'
        c.save(update_fields=['fcm_token'])
        assert run('multi_accounting') == []

    def test_a_phone_number_written_two_ways_links_accounts(self, world):
        a, b, c = (make_rider(f'MP{i}', '91') for i in range(3))
        b.emergency_contact = a.phone_number
        b.save(update_fields=['emergency_contact'])
        c.emergency_contact = '0' + a.phone_number[-10:]
        c.save(update_fields=['emergency_contact'])
        hits = run('multi_accounting')
        assert len(hits) == 1 and 'same_emergency_contact' in hits[0].signals
        assert hits[0].severity != 'high'                 # families share a contact

    def test_a_first_ride_promo_redemption_on_a_shared_device_is_high(self, world):
        from servers.ride.models import PromoCode, PromoRedemption

        riders = [make_rider(f'MR{i}', '91') for i in range(3)]
        tokens(*riders)
        promo = PromoCode.objects.create(
            code='FIRST1', discount_type='flat', discount_flat_minor=5000, currency='INR',
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=9))
        trip = completed_trip(world, riders[1], hyd_driver(world, 'MRD'))
        PromoRedemption.objects.create(promo=promo, user=riders[1], trip=trip, currency='INR',
                                       discount_amount_minor=5000)
        assert run('multi_accounting')[0].severity == 'high'

    def test_old_accounts_outside_the_window_are_not_looked_at(self, world):
        riders = [make_rider(f'MO{i}', '91') for i in range(3)]
        tokens(*riders)
        from django.contrib.auth import get_user_model

        get_user_model().objects.filter(pk__in=[r.pk for r in riders]).update(
            created_at=timezone.now() - timedelta(days=90))
        assert run('multi_accounting') == []

    def test_a_device_shared_by_drivers_is_not_a_rider_group(self, world):
        drivers = [hyd_driver(world, f'MD{i}') for i in range(3)]
        tokens(*[d.user_id for d in drivers], token='device-dddd')
        assert run('multi_accounting') == []


# ---------------------------------------------------------------------------
# collusion
# ---------------------------------------------------------------------------

class TestCollusion:
    def test_the_same_pair_ten_times_in_a_week_is_a_hit_on_the_driver(self, world):
        rider, driver = make_rider('C1', '91'), hyd_driver(world, 'C1')
        trips = [completed_trip(world, rider, driver) for _ in range(10)]
        hits = run('collusion')
        assert len(hits) == 1
        assert hits[0].subject.pk == driver.user_id_id
        assert hits[0].refs['counterpart_user_ids'] == [rider.pk]
        assert hits[0].refs['trip_ids'] == sorted(t.pk for t in trips)
        assert 'repeat_pair' in hits[0].signals

    def test_nine_trips_or_trips_spread_over_many_riders_are_not(self, world):
        driver = hyd_driver(world, 'C2')
        one = make_rider('C2', '91')
        for _ in range(9):
            completed_trip(world, one, driver)
        for i in range(10):
            completed_trip(world, make_rider(f'C2x{i}', '91'), driver)
        assert run('collusion') == []

    def test_old_trips_outside_the_window_are_not_counted(self, world):
        rider, driver = make_rider('C3', '91'), hyd_driver(world, 'C3')
        for _ in range(10):
            trip_where(world, rider, driver, completed_at=timezone.now() - timedelta(days=30))
        assert run('collusion') == []

    def test_a_driver_whose_trips_are_mostly_with_a_few_riders(self, world):
        driver = hyd_driver(world, 'C4')
        regulars = [make_rider(f'C4r{i}', '91') for i in range(2)]
        for i in range(18):
            completed_trip(world, regulars[i % 2], driver)
        for i in range(2):
            completed_trip(world, make_rider(f'C4o{i}', '91'), driver)
        hits = run('collusion', min_pair_trips=50)
        assert len(hits) == 1 and 'rider_concentration' in hits[0].signals
        assert hits[0].measures['rider_share_pct'] == 95

    def test_an_incentive_in_progress_makes_it_high(self, world):
        from servers.promotions.models import Campaign, IncentiveProgress

        rider, driver = make_rider('C5', '91'), hyd_driver(world, 'C5')
        for _ in range(10):
            completed_trip(world, rider, driver)
        campaign = Campaign.objects.create(
            kind='driver_incentive', name='Weekend', state='live', country='IN',
            geo_node=world.geo['IN-TG-HYD'], currency='INR', budget_minor=100000,
            config={'target_trips': 30, 'reward_minor': 30000})
        IncentiveProgress.objects.create(campaign=campaign, driver=driver, trips_counted=10,
                                         currency='INR')
        hit = run('collusion', concentration_pct=50, min_driver_trips=5)[0]
        assert 'incentive_in_progress' in hit.signals and hit.severity == 'high'


# ---------------------------------------------------------------------------
# incentive_farming
# ---------------------------------------------------------------------------

def counted_trips(world, driver, campaign, n, **fields):
    from servers.promotions.models import IncentiveTrip

    rider = make_rider(f'IF{driver.pk}{campaign.pk}{n}', '91')
    out = []
    for _ in range(n):
        trip = trip_where(world, rider, driver, **fields)
        IncentiveTrip.objects.create(campaign=campaign, driver=driver, trip=trip)
        out.append(trip)
    return out


@pytest.fixture
def incentive(world):
    from servers.promotions.models import Campaign

    return Campaign.objects.create(
        kind='driver_incentive', name='Weekend', state='live', country='IN',
        geo_node=world.geo['IN-TG-HYD'], currency='INR', budget_minor=100000,
        config={'target_trips': 30, 'reward_minor': 30000})


class TestIncentiveFarming:
    def test_six_very_short_counted_trips_is_a_hit(self, world, incentive):
        driver = hyd_driver(world, 'IF1')
        trips = counted_trips(world, driver, incentive, 6, actual_distance_km=Decimal('0.40'),
                              actual_duration_min=Decimal('2.00'))
        hits = run('incentive_farming')
        assert len(hits) == 1 and hits[0].subject.pk == driver.user_id_id
        assert hits[0].refs['trip_ids'] == sorted(t.pk for t in trips)
        assert hits[0].refs['campaign_ids'] == [incentive.pk]
        assert hits[0].measures['short_share_pct'] == 100 and hits[0].severity == 'medium'

    def test_twelve_of_twelve_short_trips_is_high(self, world, incentive):
        driver = hyd_driver(world, 'IF2')
        counted_trips(world, driver, incentive, 12, actual_distance_km=Decimal('0.40'),
                      actual_duration_min=Decimal('2.00'))
        assert run('incentive_farming')[0].severity == 'high'

    def test_ordinary_trips_and_too_few_short_ones_are_not(self, world, incentive):
        driver = hyd_driver(world, 'IF3')
        counted_trips(world, driver, incentive, 8, actual_distance_km=Decimal('6.00'),
                      actual_duration_min=Decimal('18.00'))
        counted_trips(world, driver, incentive, 5, actual_distance_km=Decimal('0.40'),
                      actual_duration_min=Decimal('2.00'))
        assert run('incentive_farming') == []

    def test_short_in_distance_but_long_in_time_is_not_short(self, world, incentive):
        driver = hyd_driver(world, 'IF4')
        counted_trips(world, driver, incentive, 7, actual_distance_km=Decimal('0.50'),
                      actual_duration_min=Decimal('25.00'))
        assert run('incentive_farming') == []


# ---------------------------------------------------------------------------
# gps_anomaly
# ---------------------------------------------------------------------------

def trail(trip, driver, coords, start=None, step_s=30):
    """Points `step_s` apart from the trip's start, one per `(lat, lng)`."""
    start = start or trip.started_at
    for i, (lat, lng) in enumerate(coords):
        TripLocationPoint.objects.create(
            trip=trip, driver=driver, latitude=Decimal(str(lat)), longitude=Decimal(str(lng)),
            recorded_at=start + timedelta(seconds=step_s * i), sequence=i)


def started_trip(world, rider, driver, **fields):
    now = timezone.now()
    return trip_where(world, rider, driver, started_at=now - timedelta(minutes=20),
                      completed_at=now, **fields)


NORMAL = [(17.4000 + i * 0.001, 78.4000) for i in range(8)]                  # ~110 m apart
JUMPY = [(17.4000, 78.4000), (17.4001, 78.4000), (18.4000, 78.4000), (18.4001, 78.4000),
         (17.4000, 78.4000), (17.4001, 78.4000)]                              # two 110 km jumps


class TestGpsAnomaly:
    def test_two_trips_with_impossible_jumps_is_a_hit(self, world):
        driver = hyd_driver(world, 'G1')
        rider = make_rider('G1', '91')
        trips = []
        for _ in range(2):
            trip = started_trip(world, rider, driver, actual_distance_km=Decimal('5.00'))
            trail(trip, driver, JUMPY)
            trips.append(trip)
        hits = run('gps_anomaly')
        assert len(hits) == 1 and hits[0].subject.pk == driver.user_id_id
        assert 'impossible_jump' in hits[0].signals
        assert hits[0].refs['trip_ids'] == sorted(t.pk for t in trips)

    def test_a_trail_far_shorter_than_the_billed_distance_counts(self, world):
        driver, rider = hyd_driver(world, 'G2'), make_rider('G2', '91')
        for _ in range(2):
            trip = started_trip(world, rider, driver, actual_distance_km=Decimal('20.00'))
            trail(trip, driver, NORMAL)                       # about 0.8 km driven, 20 km billed
        hits = run('gps_anomaly')
        assert len(hits) == 1 and 'trail_short_of_billed' in hits[0].signals

    def test_a_clean_trail_that_matches_the_bill_is_not(self, world):
        driver, rider = hyd_driver(world, 'G3'), make_rider('G3', '91')
        for _ in range(3):
            trip = started_trip(world, rider, driver, actual_distance_km=Decimal('0.80'))
            trail(trip, driver, NORMAL)
        assert run('gps_anomaly') == []

    def test_one_odd_trip_is_not_enough(self, world):
        driver, rider = hyd_driver(world, 'G4'), make_rider('G4', '91')
        trip = started_trip(world, rider, driver, actual_distance_km=Decimal('5.00'))
        trail(trip, driver, JUMPY)
        assert run('gps_anomaly') == []

    def test_a_single_jump_is_noise_and_old_trips_are_ignored(self, world):
        driver, rider = hyd_driver(world, 'G5'), make_rider('G5', '91')
        for _ in range(3):
            trip = started_trip(world, rider, driver, actual_distance_km=Decimal('0.80'))
            trail(trip, driver, NORMAL[:3] + [(18.4, 78.4)] + NORMAL[:3])   # one jump each way
        old = started_trip(world, rider, driver, actual_distance_km=Decimal('5.00'))
        Trip.objects.filter(pk=old.pk).update(completed_at=timezone.now() - timedelta(days=5))
        trail(old, driver, JUMPY)
        # each trip has two jump segments here (out and back), so allow 3 before it counts
        assert run('gps_anomaly', min_jumps=3) == []


# ---------------------------------------------------------------------------
# cancellation_abuse
# ---------------------------------------------------------------------------

def cancelled_trip(world, rider, driver, by):
    trip = completed_trip(world, rider, driver)
    Trip.objects.filter(pk=trip.pk).update(
        status_id=status_obj('cancelled'), cancelled_by=by, accepted_at=timezone.now(),
        cancelled_at=timezone.now(), completed_at=None)
    trip.refresh_from_db()
    return trip


class TestCancellationAbuse:
    def test_a_driver_who_cancels_half_of_what_they_accept(self, world):
        driver, rider = hyd_driver(world, 'X1'), make_rider('X1', '91')
        for _ in range(5):
            cancelled_trip(world, rider, driver, 'driver')
        for _ in range(5):
            Trip.objects.filter(pk=completed_trip(world, rider, driver).pk).update(
                accepted_at=timezone.now())
        hits = run('cancellation_abuse')
        assert len(hits) == 1 and hits[0].subject.pk == driver.user_id_id
        assert hits[0].measures == {'accepted': 10, 'cancelled': 5, 'cancel_pct': 50}

    def test_a_driver_who_cancels_rarely_is_not(self, world):
        driver, rider = hyd_driver(world, 'X2'), make_rider('X2', '91')
        cancelled_trip(world, rider, driver, 'driver')
        for _ in range(9):
            Trip.objects.filter(pk=completed_trip(world, rider, driver).pk).update(
                accepted_at=timezone.now())
        assert run('cancellation_abuse') == []

    def test_a_rider_who_cancels_after_accept_and_is_refunded_each_time(self, world):
        driver, rider = hyd_driver(world, 'X3'), make_rider('X3', '91')
        trips = [cancelled_trip(world, rider, driver, 'rider') for _ in range(5)]
        for trip in trips[:3]:
            TripMoneyAction.objects.create(trip=trip, kind='refund', amount_minor=2000,
                                           currency='INR', method='ledger_only',
                                           status='recorded', reason='Cancellation fee waived')
        hits = run('cancellation_abuse')
        assert len(hits) == 1 and hits[0].subject.pk == rider.pk
        assert hits[0].measures == {'cancelled_after_accept': 5, 'refunded': 3}

    def test_many_rider_cancels_without_refunds_are_not(self, world):
        driver, rider = hyd_driver(world, 'X4'), make_rider('X4', '91')
        for _ in range(8):
            cancelled_trip(world, rider, driver, 'rider')
        assert run('cancellation_abuse') == []


# ---------------------------------------------------------------------------
# payment_abuse
# ---------------------------------------------------------------------------

class TestPaymentAbuse:
    def test_five_failed_online_payments_is_a_hit(self, world):
        rider, driver = make_rider('P1', '91'), hyd_driver(world, 'P1')
        for i in range(5):
            make_payment(completed_trip(world, rider, driver), f'order_P1_{i}', amount=20000,
                         status='failed')
        hits = run('payment_abuse')
        assert len(hits) == 1 and hits[0].subject.pk == rider.pk
        assert hits[0].measures == {'failed_online_payments': 5}
        assert len(hits[0].refs['trip_ids']) == 5

    def test_two_failures_are_a_bad_day_not_a_pattern(self, world):
        rider, driver = make_rider('P2', '91'), hyd_driver(world, 'P2')
        for i in range(2):
            make_payment(completed_trip(world, rider, driver), f'order_P2_{i}', amount=20000,
                         status='failed')
        assert run('payment_abuse') == []

    def test_unpaid_cash_trips_and_refunds_are_counted_and_two_signals_make_it_high(self, world):
        rider, driver = make_rider('P3', '91'), hyd_driver(world, 'P3')
        for _ in range(3):
            trip_where(world, rider, driver, payment_method='cash', payment_status='pending')
        for i in range(4):
            make_payment(completed_trip(world, rider, driver), f'order_P3_{i}', amount=20000,
                         status='refunded')
        hit = run('payment_abuse')[0]
        assert set(hit.signals) == {'refunds', 'unpaid_cash_trips'} and hit.severity == 'high'

    def test_cash_trips_that_were_paid_are_not_unpaid(self, world):
        rider, driver = make_rider('P4', '91'), hyd_driver(world, 'P4')
        for _ in range(5):
            trip_where(world, rider, driver, payment_method='cash', payment_status='completed')
        assert run('payment_abuse') == []


# ---------------------------------------------------------------------------
# Thresholds are per country
# ---------------------------------------------------------------------------

class TestPerCountry:
    def test_a_rule_runs_with_its_own_countrys_numbers_only(self, seeded):
        rider, driver = make_rider('PC', '91'), hyd_driver(seeded, 'PC')
        for _ in range(10):
            completed_trip(seeded, rider, driver)
        assert runner.run_all()['created'] == 1
        RiskRuleConfig.objects.filter(country='US').update(enabled=False)
        # an Indian config with an unreachable line raises nothing, whatever the US numbers say
        rule_config.write_version(country='IN', rule_key='collusion',
                                  thresholds=thresholds('collusion', min_pair_trips=200),
                                  enabled=True, reason='test')
        RiskFlag.objects.all().delete()
        assert runner.run_all()['created'] == 0

    def test_a_country_with_no_config_runs_no_rule(self, world):
        rider, driver = make_rider('NC', '91'), hyd_driver(world, 'NC')
        for _ in range(10):
            completed_trip(world, rider, driver)
        assert runner.run_all()['rules'] == {}
        assert not RiskFlag.objects.exists()


# ---------------------------------------------------------------------------
# Dedupe, idempotence and the beat task
# ---------------------------------------------------------------------------

class TestDedupeAndIdempotence:
    def test_repeated_hits_append_to_the_open_flag_instead_of_raising_another(self, seeded):
        rider, driver = make_rider('DD', '91'), hyd_driver(seeded, 'DD')
        for _ in range(10):
            completed_trip(seeded, rider, driver)
        runner.run_all()
        flag = RiskFlag.objects.get(kind='collusion')
        assert flag.hit_count == 1 and flag.evidence['hit_count'] == 1
        first_trips = list(flag.evidence['refs']['trip_ids'])

        runner.run_all()                                     # same data: nothing changes
        flag.refresh_from_db()
        assert flag.hit_count == 1 and flag.events.count() == 1

        newest = completed_trip(seeded, rider, driver)       # one more trip: appended
        runner.run_all()
        flag.refresh_from_db()
        assert RiskFlag.objects.filter(kind='collusion').count() == 1
        assert flag.hit_count == 2 and flag.evidence['hit_count'] == 2
        assert flag.evidence['refs']['trip_ids'] == sorted(first_trips + [newest.pk])
        assert flag.evidence['last_seen'] >= flag.evidence['first_seen']
        assert flag.events.filter(action='hit').count() == 1

    def test_the_beat_task_is_idempotent(self, seeded):
        from servers.fraud.tasks import run_rules

        rider, driver = make_rider('BT', '91'), hyd_driver(seeded, 'BT')
        for _ in range(10):
            completed_trip(seeded, rider, driver)
        first = run_rules()
        counts = (RiskFlag.objects.count(), FlagEvent.objects.count(),
                  OperatorAlert.objects.count())
        second = run_rules()
        assert first['created'] == 1 and second['created'] == 0
        assert (RiskFlag.objects.count(), FlagEvent.objects.count(),
                OperatorAlert.objects.count()) == counts
        assert RiskRuleConfig.objects.filter(country='IN', last_run_at__isnull=False).exists()

    def test_the_task_is_scheduled_every_fifteen_minutes(self):
        from django.conf import settings

        entry = settings.CELERY_BEAT_SCHEDULE['fraud-run-rules-every-15-min']
        assert entry['task'] == 'fraud.run_rules'
        assert 15 in entry['schedule'].minute

    def test_a_decided_flag_does_not_come_back_for_the_same_evidence(self, seeded):
        rider, driver = make_rider('DC', '91'), hyd_driver(seeded, 'DC')
        for _ in range(10):
            completed_trip(seeded, rider, driver)
        runner.run_all()
        flag = RiskFlag.objects.get(kind='collusion')
        post(operator('city_ops_manager', seeded.geo, 'IN-TG-HYD')[0], flag_url(flag, 'dismiss/'),
             {'reason_code': 'false_positive', 'note': 'A daily commute'}, expect=200)
        runner.run_all()
        assert RiskFlag.objects.filter(kind='collusion').count() == 1
        completed_trip(seeded, rider, driver)                # new evidence: a new flag
        runner.run_all()
        assert RiskFlag.objects.filter(kind='collusion').count() == 2
        assert RiskFlag.objects.filter(kind='collusion', status='open').count() == 1

    def test_one_failing_rule_does_not_stop_the_others(self, seeded):
        rider, driver = make_rider('FR', '91'), hyd_driver(seeded, 'FR')
        for _ in range(10):
            completed_trip(seeded, rider, driver)
        broken = dataclasses.replace(RULES['multi_accounting'],
                                     evaluate=mock.Mock(side_effect=RuntimeError('boom')))
        with mock.patch.dict(RULES, {'multi_accounting': broken}):
            result = runner.run_all()
        assert 'IN:multi_accounting' in result['failed'] and result['created'] == 1

    def test_the_flag_records_the_version_of_the_rule_that_raised_it(self, seeded):
        rider, driver = make_rider('RV', '91'), hyd_driver(seeded, 'RV')
        for _ in range(10):
            completed_trip(seeded, rider, driver)
        runner.run_all()
        flag = RiskFlag.objects.get(kind='collusion')
        assert (flag.rule_key, flag.rule_version) == ('collusion', 1)
        assert flag.anchor_node.code == 'IN-TG-HYD' and flag.dedupe_key == f'collusion:{flag.subject_id}'
        assert flag.events.first().action == 'raised'

    def test_no_evidence_carries_a_raw_value(self, seeded):
        riders = [make_rider(f'EV{i}', '91') for i in range(3)]
        tokens(*riders, token='push-token-secret-value')
        runner.run_all()
        text = str(RiskFlag.objects.get(kind='multi_accounting').evidence)
        for secret in ['push-token-secret-value'] + [r.phone_number for r in riders]:
            assert secret not in text

    def test_rules_never_notify_or_touch_money(self, seeded):
        from servers.promotions.models import LedgerEntry
        from servers.rider.models import WalletTransaction

        riders = [make_rider(f'NM{i}', '91') for i in range(3)]
        tokens(*riders)
        rider, driver = make_rider('NM', '91'), hyd_driver(seeded, 'NM')
        for _ in range(10):
            completed_trip(seeded, rider, driver)
        wallet = WalletTransaction.objects.count(), LedgerEntry.objects.count()
        with mock.patch('servers.notifications.dispatcher.notify') as notify:
            result = runner.run_all()
        assert result['created'] == 2 and not notify.called
        assert (WalletTransaction.objects.count(), LedgerEntry.objects.count()) == wallet


# ---------------------------------------------------------------------------
# Changing a threshold: always a checker, bumps the version
# ---------------------------------------------------------------------------

def put_rule(client, country='IN', rule='collusion', **body):
    body.setdefault('reason', 'Commutes are tripping the line')
    return client.put(f'{ADM}/rules/{country}/{rule}/', body, format='json')


def approve(request_id):
    from test_finance_actions import approve_request, checker

    chk, _ = checker()
    return approve_request(chk, request_id)


class TestRuleChanges:
    def test_only_a_super_admin_can_propose_and_the_others_are_refused(self, seeded):
        body = {'thresholds': {'min_pair_trips': 14}}
        for who in (seeded.ops, seeded.mkt, seeded.finance, seeded.analyst, seeded.support):
            assert put_rule(who[0], **body).status_code == 403
        assert rule_config.config_for('IN', 'collusion').version == 1

    def test_a_proposal_is_a_pending_request_and_changes_nothing(self, seeded):
        response = put_rule(seeded.super[0], thresholds={'min_pair_trips': 14})
        assert response.status_code == 202
        data = body_of(response)
        assert data['state'] == 'pending_approval'
        assert data['approval_request']['action_key'] == 'fraud.rule_change'
        config = rule_config.config_for('IN', 'collusion')
        assert config.version == 1 and config.thresholds['min_pair_trips'] == 10

    def test_the_pending_request_shows_on_the_rules_tab(self, seeded):
        put_rule(seeded.super[0], thresholds={'min_pair_trips': 14})
        india = next(c for c in get(seeded.mkt[0], f'{ADM}/rules/')['results']
                     if c['country'] == 'IN')
        collusion = next(r for r in india['rules'] if r['rule_key'] == 'collusion')
        assert collusion['pending'][0]['thresholds']['min_pair_trips'] == 14
        assert collusion['pending'][0]['reason']
        assert india['may_edit'] is False

    def test_approval_by_a_second_person_writes_version_two_and_audits_it(self, seeded):
        data = body_of(put_rule(seeded.super[0], thresholds={'min_pair_trips': 14}))
        response = approve(data['approval_request']['id'])
        assert response.status_code == 200, response.content
        config = rule_config.config_for('IN', 'collusion')
        assert config.version == 2 and config.thresholds['min_pair_trips'] == 14
        assert config.changed_via_id == data['approval_request']['id']
        old = RiskRuleConfig.objects.get(country='IN', rule_key='collusion', version=1)
        assert old.is_active is False and old.valid_to is not None
        row = AdminAuditLog.objects.get(action='fraud_rule_changed')
        assert row.before['thresholds']['min_pair_trips'] == 10
        assert row.after['thresholds']['min_pair_trips'] == 14
        assert RiskRuleConfig.objects.filter(country='US', rule_key='collusion',
                                             version=1, is_active=True).exists()

    def test_the_proposer_cannot_approve_their_own_change(self, seeded):
        data = body_of(put_rule(seeded.super[0], thresholds={'min_pair_trips': 14}))
        own = seeded.super[0].post(
            f'/api/v1/approvals/admin/requests/{data["approval_request"]["id"]}/approve/',
            {'reason': 'me'}, format='json')
        assert own.status_code in (403, 409)
        assert rule_config.config_for('IN', 'collusion').version == 1

    def test_a_flag_raised_after_the_change_records_the_new_version(self, seeded):
        data = body_of(put_rule(seeded.super[0], thresholds={'min_pair_trips': 8}))
        approve(data['approval_request']['id'])
        rider, driver = make_rider('V2', '91'), hyd_driver(seeded, 'V2')
        for _ in range(8):
            completed_trip(seeded, rider, driver)
        runner.run_all()
        assert RiskFlag.objects.get(kind='collusion').rule_version == 2

    def test_bad_values_are_refused_before_a_request_exists(self, seeded):
        before = ApprovalRequest.objects.count()
        for body in ({'thresholds': {'min_pair_trips': 0}}, {'thresholds': {'min_pair_trips': 'x'}},
                     {'thresholds': {'min_pair_trips': 5000}}, {'thresholds': {'bogus': 3}},
                     {'thresholds': 'nope'}, {'thresholds': {'min_pair_trips': 14}, 'reason': ''}):
            response = put_rule(seeded.super[0], **body)
            assert response.status_code == 400, body
        assert put_rule(seeded.super[0], country='IN', rule='nope',
                        thresholds={'a': 1}).status_code == 404
        assert put_rule(seeded.super[0], rule='referral_abuse',
                        thresholds={'a': 1}).status_code in (400, 404)
        assert ApprovalRequest.objects.count() == before

    def test_an_unchanged_value_writes_no_request(self, seeded):
        before = ApprovalRequest.objects.count()
        response = put_rule(seeded.super[0], thresholds={'min_pair_trips': 10})
        assert response.status_code == 200 and body_of(response) == {'changed': False}
        assert ApprovalRequest.objects.count() == before

    def test_a_rule_can_be_switched_off_through_the_same_checker(self, seeded):
        data = body_of(put_rule(seeded.super[0], enabled=False))
        assert rule_config.config_for('IN', 'collusion').enabled is True
        approve(data['approval_request']['id'])
        assert rule_config.config_for('IN', 'collusion').enabled is False

    def test_the_rules_tab_lists_versions_runs_and_counts(self, seeded):
        rider, driver = make_rider('RT', '91'), hyd_driver(seeded, 'RT')
        for _ in range(10):
            completed_trip(seeded, rider, driver)
        runner.run_all()
        listing = get(seeded.mkt[0], f'{ADM}/rules/')['results']
        india = next(c for c in listing if c['country'] == 'IN')
        collusion = next(r for r in india['rules'] if r['rule_key'] == 'collusion')
        assert collusion['version'] == 1 and collusion['flags_7d'] == 1
        assert collusion['last_run_at'] and collusion['data_used'] and collusion['data_missing']
        pair = next(t for t in collusion['thresholds'] if t['key'] == 'min_pair_trips')
        assert (pair['value'], pair['default'], pair['low']) == (10, 10, 2)
        referral = next(r for r in india['rules'] if r['rule_key'] == 'referral_abuse')
        assert referral['mode'] == 'event' and referral['thresholds'] == []
        assert get(seeded.super[0], f'{ADM}/rules/')['results'][0]['may_edit'] is True
        assert seeded.finance[0].get(f'{ADM}/rules/').status_code == 403
        # a City Ops Manager in Hyderabad sees India's rules but not the United States'
        assert [c['country'] for c in get(seeded.ops[0], f'{ADM}/rules/')['results']] == ['IN']


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def alerts_for(client, kind=None):
    rows = client.get('/api/v1/command-centre/admin/alerts/').json()['data']['results']
    return [a for a in rows if kind is None or a['kind'] == kind]


class TestAlerts:
    def test_a_new_high_flag_raises_a_warning_for_fraud_readers_in_scope(self, world):
        node = world.geo['IN-TG-HYD']
        with_tx = lambda: None  # noqa: E731
        flag = make_flag(make_rider('A1', '91'), node, severity='high', score=90)
        alert = raising.alert_high(flag.pk)
        assert alert.kind == 'fraud.flag_high' and alert.severity == 'warning'
        assert alert.city_id == node.pk and 'A1' not in str(alert.payload)
        for who in (world.ops, world.mkt, world.super):
            assert len(alerts_for(who[0], 'fraud.flag_high')) == 1, who[1].full_name
        for who in (world.finance, world.analyst, world.ops_us, world.mkt_us):
            assert alerts_for(who[0], 'fraud.flag_high') == [], who[1].full_name
        del with_tx

    def test_a_rule_storm_makes_one_alert_not_twenty(self, world):
        node = world.geo['IN-TG-HYD']
        for i in range(6):
            flag = make_flag(make_rider(f'A2{i}', '91'), node, severity='high', score=90)
            raising.alert_high(flag.pk)
        rows = OperatorAlert.objects.filter(kind='fraud.flag_high')
        assert rows.count() == 1 and rows.first().payload['new_high_flags'] == 6
        # even after someone takes it, the half-hour window holds
        alert = rows.first()
        assert world.ops[0].post(f'/api/v1/command-centre/admin/alerts/{alert.pk}/acknowledge/',
                                 {'note': 'on it'}, format='json').status_code == 200
        raising.alert_high(make_flag(make_rider('A29', '91'), node, severity='high').pk)
        assert OperatorAlert.objects.filter(kind='fraud.flag_high').count() == 1

    def test_a_later_storm_raises_a_new_alert(self, world):
        node = world.geo['IN-TG-HYD']
        first = raising.alert_high(make_flag(make_rider('A3', '91'), node, severity='high').pk)
        OperatorAlert.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(minutes=raising.ALERT_WINDOW_MINUTES + 1))
        raising.alert_high(make_flag(make_rider('A4', '91'), node, severity='high').pk)
        assert OperatorAlert.objects.filter(kind='fraud.flag_high').count() == 2

    def test_a_medium_flag_raises_no_alert_and_a_raised_hit_does(self, seeded):
        rider, driver = make_rider('A5', '91'), hyd_driver(seeded, 'A5')
        for _ in range(10):
            completed_trip(seeded, rider, driver)
        with mock.patch('django.db.transaction.on_commit', side_effect=lambda f: f()):
            runner.run_all()
        assert OperatorAlert.objects.filter(kind='fraud.flag_high').count() == 0   # medium

        riders = [make_rider(f'A6{i}', '91') for i in range(3)]
        tokens(*riders)
        from servers.ride.models import PromoCode, PromoRedemption

        promo = PromoCode.objects.create(
            code='FIRST2', discount_type='flat', discount_flat_minor=5000, currency='INR',
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=9))
        PromoRedemption.objects.create(promo=promo, user=riders[1], trip=completed_trip(
            seeded, riders[1], driver), currency='INR', discount_amount_minor=5000)
        with mock.patch('django.db.transaction.on_commit', side_effect=lambda f: f()):
            runner.run_all()
        assert OperatorAlert.objects.filter(kind='fraud.flag_high').count() == 1

    def test_only_fraud_reviewers_can_take_the_alert(self, world):
        flag = make_flag(make_rider('A7', '91'), world.geo['IN-TG-HYD'], severity='high')
        alert = raising.alert_high(flag.pk)
        url = f'/api/v1/command-centre/admin/alerts/{alert.pk}/acknowledge/'
        assert world.mkt[0].post(url, {}, format='json').status_code == 403   # read-only
        assert world.finance[0].post(url, {}, format='json').status_code == 404
        assert world.ops[0].post(url, {}, format='json').status_code == 200

    def test_aged_open_flags_raise_one_info_alert_and_it_clears(self, world):
        node = world.geo['IN-TG-HYD']
        flags = [make_flag(make_rider(f'Q{i}', '91'), node, age_hours=60) for i in range(5)]
        make_flag(make_rider('Qfresh', '91'), node, age_hours=1)
        assert raising.check_queue_aging() == 1
        assert raising.check_queue_aging() == 0                 # one while it stays true
        alert = OperatorAlert.objects.get(kind='fraud.queue_aging')
        assert alert.severity == 'info' and alert.payload['aged_flags'] == 5
        assert len(alerts_for(world.ops[0], 'fraud.queue_aging')) == 1
        assert alerts_for(world.analyst[0], 'fraud.queue_aging') == []
        post(world.ops[0], flag_url(flags[0], 'dismiss/'),
             {'reason_code': 'duplicate', 'note': 'A duplicate flag'}, expect=200)
        raising.check_queue_aging()
        alert.refresh_from_db()
        assert alert.acknowledged_at is not None            # four left: under the line

    def test_fewer_than_five_aged_flags_raise_nothing(self, world):
        for i in range(4):
            make_flag(make_rider(f'QL{i}', '91'), world.geo['IN-TG-HYD'], age_hours=60)
        assert raising.check_queue_aging() == 0
        assert not OperatorAlert.objects.filter(kind='fraud.queue_aging').exists()
