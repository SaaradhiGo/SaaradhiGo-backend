"""MVAG 2025 (owner brief, 2026-11-09): the fare band per country and state, the commission cap.

Every pricing change is shown BEFORE and AFTER on a fixed example trip: 10 km, 20 min on a card
of 30 + 12/km + 1.50/min (180.00 metered), so each assertion reads as a fare a rider would see.
"Before" is the world with no regulation rows (what the code did until today).
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

import pytest

from finance_world import make_driver, make_rider, make_trip, settle
from servers.approvals.service import InvalidRequest
from servers.command_centre.models import COMMISSION_ABOVE_CAP, OperatorAlert
from servers.pricing import regulation
from servers.pricing import regulation_workflow as reg_flow
from servers.pricing.commission_models import CommissionCountryConfig, CommissionStateCap
from servers.payments.models import TripSettlement
from servers.pricing.models import RateCard
from servers.pricing.regulation_models import MarketRegulation
from servers.pricing.services import quote_fare
from test_finance_actions import approve_request, body_of, checker, error_code, get, swept
from test_pricing_module import BASE, LONG_AGO, activate, draft, geo, make_rule, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]

IST = dt_timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 11, 9, 12, 0, tzinfo=IST)
ONE_AM = datetime(2026, 11, 9, 1, 0, tzinfo=IST)


def card(zone, vt, cap, night='1.00'):
    return RateCard.objects.create(
        zone=zone, vehicle_type=vt, effective_from=LONG_AGO, base_fare=Decimal('30.00'),
        per_km_fare=Decimal('12.00'), per_min_fare=Decimal('1.50'), min_fare=Decimal('50.00'),
        night_surge_multiplier=Decimal(night), surge_cap_multiplier=Decimal(cap))


def quote(zone, at, *, drivers, riders):
    """The example trip in `zone`, with `drivers` nearby and `riders` asking."""
    with mock.patch('servers.pricing.services.find_zone_for_point', return_value=zone), \
            mock.patch('servers.redis_client.nearby_drivers', return_value=[object()] * drivers), \
            mock.patch('servers.redis_client.count_nearby_active_riders', return_value=riders):
        return quote_fare(10, 20, 'sedan', pickup_lat=17.4, pickup_lon=78.4, at=at)


def no_regulation():
    MarketRegulation.objects.all().delete()


def put(client, url, body, expect):
    r = swept(client.put(url, body, format='json'))
    assert r.status_code == expect, (url, r.status_code, r.content[:300])
    return r


@pytest.fixture
def tg(world):
    world.hyd.state_code = 'TG'
    world.hyd.save(update_fields=['state_code'])
    return world


class TestTheSeed:
    def test_india_band_telangana_unset_and_the_20_percent_cap(self, db):
        india = MarketRegulation.objects.get(country='IN', state_code='')
        assert (india.max_dynamic_multiplier, india.min_dynamic_multiplier) == (
            Decimal('2.00'), Decimal('0.50'))
        assert india.base_fare_source == 'state_notified'
        telangana = MarketRegulation.objects.get(country='IN', state_code='TG')
        assert telangana.max_dynamic_multiplier is None and telangana.min_dynamic_multiplier is None
        assert telangana.note == 'state notification not confirmed'
        cfg = CommissionCountryConfig.objects.get(country='IN')
        assert cfg.max_percent == Decimal('20.00')
        assert 'at least 80%' in cfg.reason


class TestTheBand:
    def test_lowest_cap_and_highest_floor_win(self, db):
        no_regulation()
        assert regulation.fare_limits('IN', 'TG', Decimal('2.50')).cap == Decimal('2.50')
        MarketRegulation.objects.create(country='IN', max_dynamic_multiplier=Decimal('2.00'),
                                      min_dynamic_multiplier=Decimal('0.50'))
        country = regulation.fare_limits('IN', 'TG', Decimal('2.50'))
        assert (country.cap, country.floor, country.cap_source) == (
            Decimal('2.00'), Decimal('0.50'), 'country')
        MarketRegulation.objects.create(country='IN', state_code='TG',
                                      max_dynamic_multiplier=Decimal('1.75'),
                                      min_dynamic_multiplier=Decimal('0.80'))
        state = regulation.fare_limits('IN', 'TG', Decimal('2.50'))
        assert (state.cap, state.floor, state.cap_source) == (
            Decimal('1.75'), Decimal('0.80'), 'state')
        card_wins = regulation.fare_limits('IN', 'TG', Decimal('1.50'))
        assert (card_wins.cap, card_wins.cap_source) == (Decimal('1.50'), 'rate_card')
        # Another state does not inherit Telangana's row.
        assert regulation.fare_limits('IN', 'AP', Decimal('2.50')).cap == Decimal('2.00')

    def test_an_unset_state_row_falls_back_to_the_country(self, db):
        limits = regulation.fare_limits('IN', 'TG', Decimal('2.50'))
        assert (limits.cap, limits.floor) == (Decimal('2.00'), Decimal('0.50'))


class TestTheQuoteBeforeAndAfter:
    def test_a_loose_card_is_held_to_the_india_cap_at_night(self, tg):
        card(tg.hyd, tg.sedan, cap='2.50', night='1.50')
        no_regulation()
        before = quote(tg.hyd, ONE_AM, drivers=1, riders=6)       # night 1.5 x demand 1.5
        assert before['surge_multiplier'] == Decimal('2.25')
        assert before['total_fare'] == Decimal('405')
        MarketRegulation.objects.create(country='IN', max_dynamic_multiplier=Decimal('2.00'),
                                      min_dynamic_multiplier=Decimal('0.50'))
        after = quote(tg.hyd, ONE_AM, drivers=1, riders=6)
        assert after['surge_multiplier'] == Decimal('2.00')
        assert after['total_fare'] == Decimal('360')
        assert after['fare_limits']['cap_source'] == 'country'

    def test_hyderabad_never_goes_over_1_5(self, tg):
        card(tg.hyd, tg.sedan, cap='1.50', night='1.50')
        no_regulation()
        before = quote(tg.hyd, ONE_AM, drivers=1, riders=6)
        MarketRegulation.objects.create(country='IN', max_dynamic_multiplier=Decimal('2.00'),
                                      min_dynamic_multiplier=Decimal('0.50'))
        MarketRegulation.objects.create(country='IN', state_code='TG',
                                      note='state notification not confirmed')
        for at, drivers, riders in ((ONE_AM, 1, 6), (NOON, 1, 6), (NOON, 1, 4), (NOON, 20, 0)):
            q = quote(tg.hyd, at, drivers=drivers, riders=riders)
            assert q['surge_multiplier'] <= Decimal('1.50'), (at, drivers, riders)
        after = quote(tg.hyd, ONE_AM, drivers=1, riders=6)
        assert before['total_fare'] == after['total_fare'] == Decimal('270')
        assert after['fare_limits']['cap_source'] == 'rate_card'

    def test_the_over_supply_discount_respects_a_floor(self, tg):
        card(tg.hyd, tg.sedan, cap='1.50')
        no_regulation()
        before = quote(tg.hyd, NOON, drivers=12, riders=0)        # 0.90 over-supply discount
        assert before['surge_multiplier'] == Decimal('0.90') and before['total_fare'] == Decimal('162')
        MarketRegulation.objects.create(country='IN', min_dynamic_multiplier=Decimal('0.50'))
        country_floor = quote(tg.hyd, NOON, drivers=12, riders=0)
        assert country_floor['total_fare'] == Decimal('162')        # 0.90 is above 0.50
        MarketRegulation.objects.create(country='IN', state_code='TG',
                                      min_dynamic_multiplier=Decimal('0.95'))
        after = quote(tg.hyd, NOON, drivers=12, riders=0)
        assert after['surge_multiplier'] == Decimal('0.95') and after['total_fare'] == Decimal('171')

    def test_the_rider_reads_higher_or_lower_demand_never_surge(self, tg):
        card(tg.hyd, tg.sedan, cap='1.50')
        high = quote(tg.hyd, NOON, drivers=1, riders=6)
        low = quote(tg.hyd, NOON, drivers=12, riders=0)
        flat = quote(tg.hyd, NOON, drivers=3, riders=2)
        assert high['demand_label'] == 'Higher demand'
        assert low['demand_label'] == 'Lower demand'
        assert flat['demand_label'] is None
        for q in (high, low, flat):
            assert q['base_fare'] == Decimal('30.00') and 'tax_amount' in q
            for line in q['fare_adjustments']:
                assert 'surge' not in line['label'].lower()

    def test_the_receipt_line_names_the_demand(self, db):
        from servers.ride.receipts import _demand_line

        assert _demand_line(Decimal('1.30')) == 'Higher demand'
        assert _demand_line(Decimal('0.90')) == 'Lower demand'
        assert _demand_line(Decimal('1.00')) == 'Demand multiplier'


class TestMarketRegulationMakerChecker:
    def test_a_state_value_needs_a_second_admin_and_is_audited(self, tg):
        maker, _ = tg.super
        r = put(maker, f'{BASE}/regulation/IN/', {
            'state_code': 'TG', 'max_dynamic_multiplier': '1.80', 'min_dynamic_multiplier': '0.70',
            'base_fare_source': 'state_notified', 'note': 'G.O. 12 of 2026',
            'reason': 'Telangana notification'}, expect=202)
        assert MarketRegulation.objects.get(country='IN', state_code='TG').max_dynamic_multiplier is None
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        row = MarketRegulation.objects.get(country='IN', state_code='TG')
        assert (row.max_dynamic_multiplier, row.min_dynamic_multiplier, row.version) == (
            Decimal('1.80'), Decimal('0.70'), 2)
        listed = get(maker, f'{BASE}/regulation/')
        assert {(x['country'], x['state_code']) for x in listed['results']} >= {('IN', ''), ('IN', 'TG')}

    def test_a_cap_below_one_is_refused(self, tg):
        maker, _ = tg.super
        r = put(maker, f'{BASE}/regulation/IN/', {
            'max_dynamic_multiplier': '0.90', 'reason': 'typo'}, expect=400)
        assert error_code(r) == 'INVALID_VALUE'


class TestCommissionCap:
    def test_a_rule_above_20_is_refused_in_india(self, world):
        maker, _ = world.super
        r = draft(maker, percent='21')
        assert error_code(r) == 'ABOVE_CAP'

    def test_a_state_cap_can_only_be_lower(self, world):
        maker, user = world.super
        with pytest.raises(InvalidRequest) as exc:
            reg_flow.propose_state_cap(user, country='IN', state_code='TG', max_percent='22',
                                       reason='typo')
        assert exc.value.code == 'STATE_CAP_ABOVE_COUNTRY'
        r = put(maker, f'{BASE}/commission/config/IN/states/TG/', {
            'max_percent': '18', 'reason': 'Telangana driver share'}, expect=202)
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        assert CommissionStateCap.objects.get(country='IN', state_code='TG').max_percent == Decimal('18.00')
        # A Hyderabad rule is now held to 18%; the country default to the strictest state.
        r = draft(maker, city=world.geo['IN-TG-HYD'].code, percent='19')
        assert error_code(r) == 'ABOVE_CAP'
        assert regulation.cap_for('IN', world.geo['IN']) == Decimal('18.00')

    def test_an_existing_rule_above_the_cap_is_flagged_not_changed(self, world):
        CommissionCountryConfig.objects.filter(country='IN').update(max_percent=None)
        rule = make_rule('IN', '25.00', node=world.geo['IN'])
        assert regulation.rules_above_cap() == []
        CommissionCountryConfig.objects.filter(country='IN').update(max_percent=Decimal('20.00'))
        assert [(r.id, c) for r, c in regulation.rules_above_cap()] == [(rule.id, Decimal('20.00'))]
        regulation.alert_rules_above_cap()
        regulation.alert_rules_above_cap()
        alerts = OperatorAlert.objects.filter(kind=COMMISSION_ABOVE_CAP)
        assert alerts.count() == 1 and alerts.get().severity == 'critical'
        rule.refresh_from_db()
        assert rule.percent == Decimal('25.00') and rule.status == 'active'
        listed = get(world.super[0], f'{BASE}/commission/config/')
        india = next(x for x in listed['results'] if x['country'] == 'IN')
        assert india['rules_above_cap'][0]['rule_id'] == rule.id


class TestTheDriverKeepsEightyPercent:
    @pytest.mark.parametrize('percent', ['15.00', '20.00'])
    @pytest.mark.parametrize('method', ['online', 'cash'])
    def test_settlement_on_a_sample_trip(self, world, percent, method):
        make_rule('IN', percent, node=world.geo['IN'])
        driver = make_driver(world.geo, 'IN-TG-HYD', 'm', currency='INR', calling='91')
        trip = make_trip(world.hyd, make_rider('m', '91'), driver, fare='437', method=method)
        settle(trip)
        s = TripSettlement.objects.get(trip=trip)
        assert s.driver_net_minor * 100 >= s.gross_fare_minor * 80
        assert s.driver_net_minor + s.commission_amount_minor == s.gross_fare_minor
