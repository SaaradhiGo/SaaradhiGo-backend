"""The rider booking fee as its own setting per place and class (owner ruling, 26 Nov): none by default
and none by default on a daily-fee plan; a change waits for a second person and starts the next local
day; the quote shows the line only above 0."""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from servers.approvals.models import ApprovalRequest
from servers.pricing import booking_fees, fee_plans
from servers.pricing.booking_fee_models import RiderBookingFee
from test_driver_fee_plans import india, plan  # noqa: F401
from test_finance_actions import approve_request, body_of, checker, error_code, post
from test_pricing_module import BASE, geo, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]


@pytest.fixture(autouse=True)
def _clean():
    RiderBookingFee.objects.all().delete()          # the seeded India plan's fee, moved by 0032


def fee(country, scope, amounts, start=date(2026, 1, 1)):
    v = RiderBookingFee.objects.filter(country=country, scope_code=scope).count() + 1
    return RiderBookingFee.objects.create(country=country, scope_code=scope, currency='INR', amounts=amounts,
                                          version=v, effective_from=start)


def tomorrow():
    return fee_plans.local_day('IN') + timedelta(days=1)


class TestBookingFees:
    def test_none_by_default(self, india):
        assert booking_fees.fee_minor_for_zone(india.hyd, 'sedan') == 0
        row = next(c for c in booking_fees.place('IN', '')['classes'] if c['vehicle_type'] == 'sedan')
        assert row['words'] == 'No booking fee' and row['source'] == 'default'

    def test_edit_preview_send_approve_then_next_day(self, india):
        maker, _ = india.super
        body = {'country': 'IN', 'scope_code': '', 'amounts': {'sedan': 500}}
        r = post(maker, f'{BASE}/booking-fees/preview/', body, expect=200)
        ch = body_of(r)['changes'][0]
        assert (ch['from'], ch['to']) == ('No booking fee', '₹5') and body_of(r)['start']['effective_from'] == \
            tomorrow().isoformat()
        r = post(maker, f'{BASE}/booking-fees/edit/', {**body, 'reason': 'Covers the booking cost'})
        assert r.status_code == 202, r.content[:300]
        req = ApprovalRequest.objects.get(pk=body_of(r)['approval_request']['id'])
        assert req.payload['display']['changes'][0]['to'] == '₹5'
        assert not RiderBookingFee.objects.exists()                     # nothing until approved
        chk, _ = checker()
        assert approve_request(chk, req.id).status_code == 200
        assert booking_fees.resolve('IN', ['IN-TG-HYD', 'IN-TG'], 'sedan', fee_plans.local_day('IN'))[0] == 0
        assert booking_fees.resolve('IN', ['IN-TG-HYD', 'IN-TG'], 'sedan', tomorrow())[0] == 500

    def test_a_daily_fee_place_has_none_unless_set_there(self, india):
        fee('IN', '', {'sedan': 500})
        plan(scope='IN-TG-HYD', plan_type='daily_pass', amounts={'sedan': {'daily_pass_minor': 9900}}, version=2)
        today = fee_plans.local_day('IN')
        assert booking_fees.resolve('IN', ['IN-TG-HYD', 'IN-TG'], 'sedan', today)[0] == 0
        assert booking_fees.resolve('IN', ['IN-TG-WGL', 'IN-TG'], 'sedan', today)[0] == 500
        fee('IN', 'IN-TG-HYD', {'sedan': 200})
        assert booking_fees.resolve('IN', ['IN-TG-HYD', 'IN-TG'], 'sedan', today)[0] == 200

    def test_setting_zero_where_there_was_a_fee(self, india):
        fee('IN', 'IN-TG-HYD', {'sedan': 500, 'suv': 500})
        maker, _ = india.super
        r = post(maker, f'{BASE}/booking-fees/edit/', {'country': 'IN', 'scope_code': 'IN-TG-HYD',
                                                         'amounts': {'sedan': 0, 'suv': 0}, 'reason': 'Daily-fee city'})
        chk, _ = checker()
        approve_request(chk, body_of(r)['approval_request']['id'])
        assert booking_fees.resolve('IN', ['IN-TG-HYD'], 'suv', tomorrow())[0] == 0
        assert booking_fees.resolve('IN', ['IN-TG-HYD'], 'suv', fee_plans.local_day('IN'))[0] == 500

    def test_bad_input_is_refused(self, india):
        maker, _ = india.super
        cases = [({'amounts': {'sedan': 550}}, 'WHOLE_RUPEES'), ({'amounts': {'xl': 0}}, 'INVALID_CLASS'),
                 ({'amounts': {'sedan': -100}}, 'INVALID_AMOUNT'), ({'amounts': {'sedan': 0}}, 'NO_CHANGE')]
        for extra, code in cases:
            r = post(maker, f'{BASE}/booking-fees/edit/', {'country': 'IN', 'scope_code': '', 'reason': 'A test',
                                                             **extra})
            assert r.status_code == 400 and error_code(r) == code, (extra, r.content[:200])
        r = post(maker, f'{BASE}/booking-fees/edit/', {'country': 'IN', 'scope_code': '', 'amounts': {'sedan': 500}})
        assert error_code(r) == 'REASON_REQUIRED'

    def test_the_quote_line_only_above_zero(self, india, monkeypatch):
        from servers.platform_ops import flags
        from servers.pricing.services import _rider_platform_fee

        monkeypatch.setattr(flags, 'flag_enabled', lambda *a, **k: True)
        assert _rider_platform_fee(india.hyd, 'sedan', 'INR') == Decimal('0.00')
        fee('IN', '', {'sedan': 500})
        assert _rider_platform_fee(india.hyd, 'sedan', 'INR') == Decimal('5')

    def test_the_command_raises_one_request_for_a_checker(self, india):
        fee('IN', 'IN-TG-HYD', {'suv': 500})
        args = ['raise_booking_fee_change', '--country', 'IN', '--scope', 'IN-TG-HYD', '--amounts', 'sedan=0,suv=0',
                '--reason', 'Owner ruling 26 Nov: no booking fee on the daily-fee plan']
        out = StringIO()
        call_command(*args, stdout=out)
        assert 'raised' in out.getvalue()
        req = ApprovalRequest.objects.get(target_type='rider_booking_fee')
        assert req.status == 'requested' and req.maker_role == 'system'
        out = StringIO()
        call_command(*args, stdout=out)
        assert 'not raised' in out.getvalue() and ApprovalRequest.objects.filter(target_type='rider_booking_fee').count() == 1
