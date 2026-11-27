"""Trip extra charges, step 1 (owner brief, 26 Nov): the catalogue, what a route meets, the locked total,
passed through without commission, Google's toll price only as a cached cross-check, and every change
through a second person."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.core.cache import cache

from servers.approvals.models import ApprovalRequest
from servers.pricing import fee_plans, toll_check, trip_charges
from servers.pricing.models import RateCard
from servers.pricing.trip_charge_models import TripChargeItem
from test_finance_actions import approve_request, body_of, checker, error_code, post
from test_pricing_module import BASE, LONG_AGO, geo, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]

PICKUP, AIRPORT_DROP = (17.43, 78.40), (17.24, 78.43)
#: A tolled road on the straight line between them, and an airport box around the drop.
ROAD = [[78.405, 17.40], [78.42, 17.30]]
AIRPORT_BOX = [[[78.41, 17.22], [78.45, 17.22], [78.45, 17.26], [78.41, 17.26], [78.41, 17.22]]]


@pytest.fixture(autouse=True)
def _clean():
    TripChargeItem.objects.all().delete()
    cache.clear()


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(trip_charges, '_enabled', lambda zone: True)


def item(code, kind, amounts, *, who='rider', start=date(2026, 1, 1), **extra):
    v = TripChargeItem.objects.filter(country='IN', code=code).count() + 1
    return TripChargeItem.objects.create(country='IN', code=code, version=v, kind=kind, name=extra.pop('name', code),
                                         amounts=amounts, who_pays=who, currency='INR', effective_from=start,
                                         **extra)


def road(amounts=None):
    return item('orr', 'tolled_road', amounts or {'sedan': 250}, name='Outer Ring Road',
                geometry={'type': 'LineString', 'coordinates': ROAD}, corridor_m=60)


def airport(**extra):
    return item('rgia', 'airport', {'sedan': 10000, 'auto': 5000}, name='Airport pickup fee',
                geometry={'type': 'Polygon', 'coordinates': AIRPORT_BOX}, **extra)


def lines(vt='sedan', pickup=PICKUP, drop=AIRPORT_DROP):
    items = trip_charges.items_in_force('IN', fee_plans.local_day('IN')).values()
    return trip_charges.match(items, trip_charges.route_points('', pickup, drop), pickup, drop, vt, 'INR')


class TestMatching:
    def test_a_tolled_road_is_priced_per_km_inside_its_corridor(self, world):
        road()
        (line,) = lines()
        assert line['kind'] == 'tolled_road' and line['label'] == 'Toll (estimated): Outer Ring Road'
        km = Decimal(line['km'])
        assert Decimal('9') < km < Decimal('12')                     # about 10.9 km of the line is on it
        assert line['amount_minor'] == int((Decimal('2.50') * km).quantize(Decimal('1'))) * 100
        assert line['estimated'] is True

    def test_a_toll_plaza_counts_once_when_the_route_passes_it(self, world):
        item('plaza', 'toll_plaza', {'sedan': 4500}, geometry={'type': 'Point', 'coordinates': [78.415, 17.335]},
             radius_m=100)
        assert [x['amount_minor'] for x in lines()] == [4500]
        assert lines(drop=(17.43, 78.30)) == []                      # a route the other way misses it

    def test_an_airport_charges_on_the_side_it_is_set_for(self, world):
        airport(applies_on='pickup')
        assert lines() == []                                         # a drop at the airport: no pickup fee
        assert [x['amount_minor'] for x in lines(pickup=AIRPORT_DROP, drop=PICKUP)] == [10000]
        TripChargeItem.objects.update(applies_on='pickup_or_drop')
        assert [x['amount_minor'] for x in lines()] == [10000]

    def test_a_class_without_an_amount_is_not_charged(self, world):
        road({'sedan': 250})                                         # autos may not use the ORR
        assert lines('auto') == []

    def test_a_permit_is_charged_when_the_drop_leaves_the_state(self, world):
        item('ts-permit', 'permit', {'sedan': 30000}, remit_to='AP Transport Department',
             geometry={'type': 'Polygon', 'coordinates': [[[78.0, 17.0], [79.0, 17.0], [79.0, 18.0], [78.0, 18.0]]]})
        assert lines() == []
        (line,) = lines(drop=(16.5, 80.6))
        assert line['amount_minor'] == 30000 and line['remit_to'] == 'AP Transport Department'

    def test_a_retired_or_future_item_does_not_apply(self, world):
        road()
        TripChargeItem.objects.update(effective_to=fee_plans.local_day('IN'))
        assert lines() == []
        item('later', 'tolled_road', {'sedan': 250}, geometry={'type': 'LineString', 'coordinates': ROAD},
             corridor_m=60, start=fee_plans.local_day('IN') + timedelta(days=3))
        assert lines() == []


class TestTheQuote:
    def card(self, world):
        RateCard.objects.create(zone=world.hyd, vehicle_type=world.sedan, effective_from=LONG_AGO,
                                base_fare=Decimal('50'), per_km_fare=Decimal('12'), per_min_fare=Decimal('1'),
                                min_fare=Decimal('80'), gst_percent=Decimal('5'))

    def quote(self, world, **kw):
        from servers.pricing.services import quote_fare

        return quote_fare(20, 30, 'sedan', pickup_lat=PICKUP[0], pickup_lon=PICKUP[1], drop_lat=AIRPORT_DROP[0],
                          drop_lon=AIRPORT_DROP[1], **kw)

    def test_rider_extras_are_lines_inside_the_locked_total_and_untaxed(self, world, flag_on):
        self.card(world)
        plain = self.quote(world)
        road()
        airport(applies_on='drop')
        item('parking', 'airport', {'sedan': 3000}, who='driver', applies_on='drop',
             geometry={'type': 'Polygon', 'coordinates': AIRPORT_BOX})
        with mock.patch.object(toll_check, 'schedule') as queued:
            q = self.quote(world)
        rider = [a for a in q['fare_adjustments'] if a['code'] == 'trip_extra']
        assert {a['label'] for a in rider} == {'Toll (estimated): Outer Ring Road', 'Airport pickup fee'}
        assert q['total_fare'] == plain['total_fare'] + sum(a['amount'] for a in rider)
        assert q['trip_extras_minor'] == sum(int(a['amount'] * 100) for a in rider)
        assert q['tax_amount'] == plain['tax_amount']                 # GST is never on a pass-through
        assert {x['who_pays'] for x in q['trip_extras']} == {'rider', 'driver'}   # the driver's: not on the fare
        queued.assert_called_once()                                    # it crosses a toll: one cross-check

    def test_off_unless_the_flag_is_on(self, world):
        self.card(world)
        road()
        q = self.quote(world)
        assert q['trip_extras'] == [] and not any(a['code'] == 'trip_extra' for a in q['fare_adjustments'])

    def test_no_toll_crossed_no_google_call(self, world, flag_on):
        self.card(world)
        airport(applies_on='drop')
        with mock.patch.object(toll_check, 'schedule') as queued:
            self.quote(world)
        queued.assert_not_called()

    def test_the_waiting_rule_is_shown_not_charged(self, world, flag_on):
        self.card(world)
        item('hyd-waiting', 'waiting', {'sedan': 200}, free_minutes=3, scope_code='IN-TG-HYD')
        q = self.quote(world)
        assert q['waiting_terms']['words'] == 'First 3 min of waiting free, then ₹2/min'
        assert q['trip_extras'] == []

    def test_settlement_takes_no_commission_on_extras(self, world):
        from finance_world import make_driver, make_rider, make_trip
        from servers.driver.utils import settlement_amounts
        from servers.ride.models import FarePricing
        from test_pricing_module import make_rule

        make_rule('IN', '20.00', node=world.geo['IN'])
        driver = make_driver(world.geo, 'IN-TG-HYD', 'tc', currency='INR', calling='91')
        trip = make_trip(world.hyd, make_rider('tc', '91'), driver, fare='600')
        before = settlement_amounts(trip)
        FarePricing.objects.create(trip_id=trip, base_fare=0, distance_fare=0, time_fare=0, surge_multiplier=1,
                                   total_fare=Decimal('600'), trip_extras_minor=10000,
                                   trip_extras=[{'item_code': 'rgia', 'amount_minor': 10000, 'who_pays': 'rider'}])
        after = settlement_amounts(trip)
        assert after['gross_minor'] == before['gross_minor'] - 10000
        assert after['commission_minor'] == 10000                     # 20% of ₹500, not of ₹600


class TestGoogleCrossCheck:
    def test_one_check_per_route_per_day(self, world):
        with mock.patch('servers.pricing.tasks.check_route_tolls.delay') as delay:
            assert toll_check.schedule(world.hyd, PICKUP, AIRPORT_DROP) is True
            assert toll_check.schedule(world.hyd, PICKUP, AIRPORT_DROP) is False
        delay.assert_called_once()

    def test_not_while_the_maps_budget_is_spent(self, world):
        with mock.patch.object(toll_check, '_budget_spent', return_value=True), \
                mock.patch('servers.pricing.tasks.check_route_tolls.delay') as delay:
            assert toll_check.schedule(world.hyd, PICKUP, AIRPORT_DROP) is False
        delay.assert_not_called()

    @pytest.mark.parametrize('ratio,status', [('1.05', 'ok'), ('1.5', 'mismatch')])
    def test_more_than_ten_percent_apart_raises_an_ops_alert(self, world, ratio, status):
        from servers.command_centre.models import TOLL_MISMATCH, OperatorAlert

        road()
        ours = lines()[0]['amount_minor']
        google_minor = int(ours * Decimal(ratio))
        answer = {'has_tolls': True, 'price_minor': google_minor, 'polyline': ''}
        with mock.patch.object(toll_check, 'google_tolls', return_value=answer) as call, \
                mock.patch.object(toll_check, '_alert') as alert:
            finding = toll_check.check_route(world.hyd, PICKUP, AIRPORT_DROP)
            toll_check.check_route(world.hyd, PICKUP, AIRPORT_DROP)
        assert call.call_count == 1                                    # cached for 24 h
        assert finding['status'] == status and finding['catalogue_minor'] == ours
        assert alert.called == (status == 'mismatch')
        assert TOLL_MISMATCH == 'pricing.toll_mismatch' and OperatorAlert  # the kind is registered

    def test_google_sees_a_toll_the_catalogue_lacks(self, world):
        with mock.patch.object(toll_check, 'google_tolls',
                               return_value={'has_tolls': True, 'price_minor': 9000, 'polyline': ''}), \
                mock.patch.object(toll_check, '_alert') as alert:
            assert toll_check.check_route(world.hyd, PICKUP, AIRPORT_DROP)['status'] == 'missing'
        alert.assert_called_once()

    def test_the_call_is_metered_as_its_own_sku(self, world, settings):
        from servers.ride import maps_meter
        from servers.ride.maps_models import MapsSkuPrice, MapsUsage

        assert toll_check.SKU in maps_meter.SKUS and MapsSkuPrice.objects.filter(sku=toll_check.SKU).exists()
        response = mock.Mock(status_code=200, json=lambda: {'routes': [{'travelAdvisory': {'tollInfo': {
            'estimatedPrice': [{'currencyCode': 'INR', 'units': '275'}]}}, 'polyline': {'encodedPolyline': ''}}]})
        response.raise_for_status = lambda: None
        with mock.patch('servers.ride.maps_utils.maps_fake', return_value=False), \
                mock.patch('servers.ride.maps_utils.maps_configured', return_value=True), \
                mock.patch('servers.ride.maps_utils._api_key', return_value='k'), \
                mock.patch.object(toll_check.requests, 'post', return_value=response) as post_:
            out = toll_check.google_tolls(PICKUP, AIRPORT_DROP, 'INR', 'IN')
        assert out['price_minor'] == 27500 and out['has_tolls']
        assert post_.call_args.kwargs['json']['extraComputations'] == ['TOLLS']
        assert MapsUsage.objects.filter(sku=toll_check.SKU, country='IN').exists()


class TestTheCatalogueEditor:
    BODY = {'country': 'IN', 'kind': 'airport', 'name': 'Airport pickup fee', 'scope_code': 'IN-TG-HYD',
            'applies_on': 'pickup', 'who_pays': 'rider', 'amounts': {'sedan': 10000},
            'geometry': {'type': 'Polygon', 'coordinates': AIRPORT_BOX}}

    def test_add_waits_for_a_second_person_then_starts_next_day(self, world):
        maker, _ = world.super
        r = post(maker, f'{BASE}/trip-charges/preview/', self.BODY, expect=200)
        assert body_of(r)['changes'][0] == {'label': 'New item', 'from': '—', 'to': 'Airport: Airport pickup fee'}
        r = post(maker, f'{BASE}/trip-charges/edit/', {**self.BODY, 'reason': 'RGIA pickup fee', 'sample': True})
        assert r.status_code == 202, r.content[:300]
        assert not TripChargeItem.objects.exists()
        chk, _ = checker()
        req_id = body_of(r)['approval_request']['id']
        assert approve_request(chk, req_id).status_code == 200
        row = TripChargeItem.objects.get()
        assert (row.code, row.version, row.sample) == ('airport-pickup-fee', 1, True)
        assert row.effective_from == fee_plans.local_day('IN') + timedelta(days=1)
        assert ApprovalRequest.objects.get(pk=req_id).target_id == 'IN:airport-pickup-fee'

    def test_an_edit_is_the_next_version_and_remove_ends_it(self, world):
        airport(applies_on='pickup', sample=True)
        maker, _ = world.super
        chk, _ = checker()
        r = post(maker, f'{BASE}/trip-charges/edit/', {'country': 'IN', 'code': 'rgia', 'amounts': {'sedan': 12000},
                                                      'reason': 'Real figure from the airport operator'})
        changes = body_of(r)['approval_request'] and ApprovalRequest.objects.get(
            pk=body_of(r)['approval_request']['id']).payload['display']['changes']
        assert {'label': 'Airport pickup fee: sample value', 'from': 'QA sample', 'to': 'Real figure'} in changes
        assert {'label': 'Airport pickup fee, Auto', 'from': '₹50', 'to': 'not charged'} in changes
        approve_request(chk, body_of(r)['approval_request']['id'])
        assert TripChargeItem.objects.get(code='rgia', version=2).amounts == {'sedan': 12000}
        r = post(maker, f'{BASE}/trip-charges/edit/', {'country': 'IN', 'code': 'rgia', 'op': 'retire',
                                                      'reason': 'The airport dropped the fee'})
        approve_request(chk, body_of(r)['approval_request']['id'])
        later = fee_plans.local_day('IN') + timedelta(days=2)
        assert 'rgia' not in trip_charges.items_in_force('IN', later)

    def test_bad_input_is_refused(self, world):
        maker, _ = world.super
        cases = [({'amounts': {'sedan': 10050}}, 'WHOLE_RUPEES'), ({'amounts': {'xl': 100}}, 'INVALID_CLASS'),
                 ({'geometry': {'type': 'Polygon', 'coordinates': [[[78.4, 17.2], [78.5, 17.2]]]}}, 'INVALID_GEOMETRY'),
                 ({'kind': 'teleport'}, 'INVALID_KIND'), ({'applies_on': 'never'}, 'INVALID_APPLIES_ON'),
                 ({'kind': 'permit'}, 'REMIT_TO_REQUIRED')]
        for extra, code in cases:
            r = post(maker, f'{BASE}/trip-charges/edit/', {**self.BODY, 'reason': 'A test', **extra})
            assert r.status_code == 400 and error_code(r) == code, (extra, r.content[:200])
        r = post(maker, f'{BASE}/trip-charges/edit/', self.BODY)
        assert error_code(r) == 'REASON_REQUIRED'

    def test_a_per_km_rate_may_carry_paise_and_production_takes_no_sample(self, world, settings):
        settings.IS_PRODUCTION = True
        body = {'country': 'IN', 'kind': 'tolled_road', 'name': 'Outer Ring Road', 'scope_code': 'IN-TG-HYD',
                'amounts': {'sedan': 234}, 'geometry': {'type': 'LineString', 'coordinates': ROAD}, 'sample': True}
        preview = trip_charges.preview('IN', body)
        assert preview['item']['amounts'] == {'sedan': 234} and preview['item']['sample'] is False

    def test_the_list_shows_items_requests_and_toll_spend(self, world):
        airport(applies_on='pickup', sample=True)
        maker, _ = world.super
        r = maker.get(f'{BASE}/trip-charges/')
        india = next(c for c in body_of(r)['results'] if c['country'] == 'IN')
        (row,) = india['items']
        assert row['sample'] and row['amount_words']['sedan'] == '₹100' and row['who_words'].startswith('Rider')
        assert india['toll_checks'] == {'calls_this_month': 0, 'list_cost_usd': '0.0000'}
        assert any(p['scope_code'] == 'IN-TG-HYD' for p in india['places'])
