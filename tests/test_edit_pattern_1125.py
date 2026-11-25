"""The Edit pattern for fee plans and fare cards (owner brief 25 Nov): Edit -> preview -> send ->
approve -> in force; the version in force stays until approved; an increase waits the 7-day
driver notice; send back and withdraw return the change to a draft; the checker sees the same
before/after and impact; a zone shows only its own market's classes."""
from datetime import timedelta
from decimal import Decimal

import pytest

from servers.approvals.models import ApprovalRequest
from servers.pricing import fee_plans
from servers.pricing.fee_plan_models import DriverFeePlan
from servers.pricing.models import RateCard
from test_driver_fee_plans import india, plan  # noqa: F401
from test_finance_actions import approve_request, body_of, checker, error_code, post
from test_pricing_module import APPROVALS, BASE, LONG_AGO, geo, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]


def _edit(india, cents, **extra):
    maker, _ = india.super
    body = {'amounts': {'sedan': {'per_trip_fee_minor': cents}}, 'reason': 'Quarterly review', **extra}
    return maker, post(maker, f'{BASE}/fee-plans/{india.plan.id}/edit/', body)


class TestFeePlanEdit:
    def test_preview_shows_changes_impact_and_next_day(self, india):
        maker, _ = india.super
        r = post(maker, f'{BASE}/fee-plans/{india.plan.id}/impact/',
                 {'amounts': {'sedan': {'per_trip_fee_minor': 800}}}, expect=200)
        body = body_of(r)
        assert body['changes'] == [{'label': 'Sedan: fee a trip', 'from': '₹10', 'to': '₹8'}]
        trips = [row['trips_per_day'] for row in body['impact']['rows'] if row['vehicle_type'] == 'sedan']
        assert trips == [5, 12, 20]
        assert body['start']['increase'] is False
        assert body['start']['effective_from'] == (fee_plans.local_day('IN') + timedelta(days=1)).isoformat()

    def test_edit_waits_for_approval_then_starts_next_day(self, india):
        maker, r = _edit(india, 800)
        assert r.status_code == 202
        india.plan.refresh_from_db()
        assert india.plan.status == 'active'  # the version in force stays until approved
        req = ApprovalRequest.objects.get(pk=body_of(r)['approval_request']['id'])
        assert req.payload['display']['changes'][0]['to'] == '₹8'
        assert req.payload['display']['impact']['rows']
        chk, _ = checker()
        assert approve_request(chk, req.id).status_code == 200
        new = DriverFeePlan.objects.get(pk=req.payload['plan_id'])
        assert new.status == 'active' and new.version == 2
        assert new.effective_from == fee_plans.local_day('IN') + timedelta(days=1)

    def test_an_increase_waits_the_driver_notice_and_an_earlier_date_is_refused(self, india):
        maker, _ = india.super
        r = post(maker, f'{BASE}/fee-plans/{india.plan.id}/impact/',
                 {'amounts': {'sedan': {'per_trip_fee_minor': 1200}}}, expect=200)
        start = body_of(r)['start']
        assert start['increase'] is True and start['notice_days'] == 7
        assert start['effective_from'] == (fee_plans.local_day('IN') + timedelta(days=7)).isoformat()
        tomorrow = (fee_plans.local_day('IN') + timedelta(days=1)).isoformat()
        _, r = _edit(india, 1200, effective_from=tomorrow)
        assert r.status_code == 400 and error_code(r) == 'NOTICE_PERIOD'

    def test_send_back_and_withdraw_return_it_to_a_draft_the_editor_can_resend(self, india):
        maker, r = _edit(india, 800)
        req_id = body_of(r)['approval_request']['id']
        chk, _ = checker()
        post(chk, f'{APPROVALS}/{req_id}/send-back/', {'reason': 'Make it 9 rupees'}, expect=200)
        draft = DriverFeePlan.objects.get(pk=ApprovalRequest.objects.get(pk=req_id).payload['plan_id'])
        assert draft.status == 'draft'
        r = post(maker, f'{BASE}/fee-plans/{draft.id}/edit/',
                 {'amounts': {'sedan': {'per_trip_fee_minor': 900}}, 'reason': 'As asked'}, expect=202)
        req2 = body_of(r)['approval_request']['id']
        post(maker, f'{APPROVALS}/{req2}/cancel/', {'reason': 'Not now'}, expect=200)
        draft.refresh_from_db()
        assert draft.status == 'draft' and draft.amounts['sedan']['per_trip_fee_minor'] == 900
        india.plan.refresh_from_db()
        assert india.plan.status == 'active'

    def test_history_says_who_decided_and_why(self, india):
        maker, r = _edit(india, 800)
        chk, _ = checker()
        approve_request(chk, body_of(r)['approval_request']['id'])
        rows = body_of(maker.get(f'{BASE}/fee-plans/'))['results']
        v2 = next(p for p in rows if p['version'] == 2)
        assert v2['decision']['status'] == 'executed' and v2['decision']['request_reason'] == 'Quarterly review'

    def test_nothing_changed_is_refused(self, india):
        _, r = _edit(india, 1000)
        assert r.status_code == 400 and error_code(r) == 'NO_CHANGE'


class TestFareCardEdit:
    def seed(self, world):
        return RateCard.objects.create(zone=world.hyd, vehicle_type=world.sedan, effective_from=LONG_AGO,
                                       base_fare=Decimal('30'), per_km_fare=Decimal('12'),
                                       per_min_fare=Decimal('2'), min_fare=Decimal('50'))

    def test_edit_carries_before_after_and_sample_fares_and_starts_next_local_day(self, world):
        card = self.seed(world)
        maker, _ = world.super
        day = (fee_plans.local_day('IN', zone=world.hyd) + timedelta(days=1)).isoformat()
        r = maker.patch(f'{BASE}/rate-cards/{card.id}/', {'per_km_fare': '13.00', 'reason': 'Fuel price',
                                                          'effective_day': day}, format='json')
        assert r.status_code == 202, r.content[:300]
        req = ApprovalRequest.objects.get(pk=body_of(r)['approval_request']['id'])
        shown = req.payload['display']
        assert shown['changes'] == [{'label': 'Per km', 'from': '₹12.00', 'to': '₹13.00'}]
        assert [row[0] for row in shown['impact']['rows']][::2] == ['3 km', '8 km', '15 km']
        assert req.payload['effective_from'].startswith(f'{day}T00:00:00+05:30')

    def test_a_zone_shows_only_its_markets_classes(self, world):
        from servers.driver.models import VehicleType

        VehicleType.objects.get_or_create(type='standard')
        maker, _ = world.super
        zones = body_of(maker.get(f'{BASE}/fare-cards/overview/'))['zones']
        hyd = next(z for z in zones if z['code'] == 'IN-TG-HYD')
        offered = {c['vehicle_type_name']: c['offered'] for c in hyd['classes']}
        assert offered['sedan'] is True and offered['standard'] is False
        assert hyd['local_today'] and hyd['launch_state']


def test_the_approval_list_says_the_fare_change_in_words(world):  # noqa: F811
    from servers.approvals.describe import describe

    card = RateCard.objects.create(zone=world.hyd, vehicle_type=world.sedan, effective_from=LONG_AGO,
                                   base_fare=Decimal('30'), per_km_fare=Decimal('12'), per_min_fare=Decimal('2'),
                                   min_fare=Decimal('50'))
    maker, _ = world.super
    r = maker.patch(f'{BASE}/rate-cards/{card.id}/', {'per_km_fare': '13.00', 'reason': 'Fuel price'}, format='json')
    req = ApprovalRequest.objects.get(pk=body_of(r)['approval_request']['id'])
    assert describe(req, 'title') == 'Sedan fare card in Hyderabad: Per km ₹12.00 → ₹13.00'
