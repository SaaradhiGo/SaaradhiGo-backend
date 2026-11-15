"""US round 6 (Srini's ruling, 15 Nov): one Finance person refunds up to a per-trip total and a daily
total alone; above either a checker approves. A refund never touches the driver's earnings. The
receipt shows each refund and the net charged; "Ready" for Stripe only when Stripe confirms it."""
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command

from servers.approvals import executors
from servers.approvals.models import ApprovalRequest
from servers.approvals.service import write_policy_version
from servers.payments.models import Payment
from servers.rider.models import Wallet, WalletTransaction
from test_trips_module import _trip, detail, policies, refund, world  # noqa: F401
from test_sos_support_zones_matrix import geo, operator  # noqa: F401

pytestmark = pytest.mark.django_db


def paid_trip(world, fare):
    t = _trip(world['rider'], world['hyd'], driver=world['paid'].driver_id, fare=fare, paid=True,
              method='online')
    Payment.objects.create(trip_id=t, user_id=world['rider'], amount_minor=fare, method='online',
                           currency='INR', status='completed')
    return t


@pytest.fixture
def limits(policies):
    """Rs 50 a trip in total and Rs 250 a day for one Finance person (the US ruling's shape)."""
    write_policy_version(country='IN', action_key='finance.refund', limit_kind='money', currency='INR',
                         maker_limit=5_000, maker_daily_limit=25_000, cumulative_per_target=True,
                         reason='test')


class TestRefundLimits:
    def test_per_trip_total_not_per_refund(self, geo, world, limits):
        client, _ = operator('finance', geo)
        trip = paid_trip(world, 20_000)
        assert refund(client, trip, 3_000, key='a').status_code == 200          # alone
        assert refund(client, trip, 2_000, key='b').status_code == 200          # 5,000 in all: alone
        r = refund(client, trip, 1, key='c')                                    # 5,001: a checker
        assert r.status_code == 202 and r.json()['data']['state'] == 'pending_approval'

    def test_daily_total_per_person(self, geo, world, limits):
        client, user = operator('finance', geo)
        for i in range(5):                                                      # 5 x 5,000 = 25,000
            assert refund(client, paid_trip(world, 9_000), 5_000, key=f'd{i}').status_code == 200
        assert refund(client, paid_trip(world, 9_000), 1, key='over').status_code == 202
        other, _ = operator('finance', geo)                                     # someone else: own day
        assert refund(other, paid_trip(world, 9_000), 5_000, key='o').status_code == 200

    def test_the_refund_form_gets_the_limits_and_what_is_left(self, geo, world, limits):
        client, _ = operator('finance', geo)
        trip = paid_trip(world, 20_000)
        refund(client, trip, 3_000, key='a')
        got = client.get(detail(trip)).json()['data']['actions']['refund_limits']
        assert got == {'action_key': 'finance.refund', 'currency': 'INR', 'per_trip_minor': 5_000,
                       'per_trip_left_minor': 2_000, 'per_day_minor': 25_000, 'used_today_minor': 3_000,
                       'per_day_left_minor': 22_000, 'alone_up_to_minor': 2_000}

    def test_a_refund_never_touches_the_drivers_earnings(self, geo, world, limits):
        client, _ = operator('finance', geo)
        trip = paid_trip(world, 20_000)
        driver_user = trip.driver_id.user_id
        before = list(WalletTransaction.objects.filter(user_id=driver_user).values_list('id', flat=True))
        balance = list(Wallet.objects.filter(user_id=driver_user).values_list('balance_minor', flat=True))
        assert refund(client, trip, 5_000, key='a').status_code == 200
        assert list(WalletTransaction.objects.filter(user_id=driver_user).values_list('id', flat=True)) == before
        assert list(Wallet.objects.filter(user_id=driver_user).values_list('balance_minor', flat=True)) == balance


class TestPolicyChangeIsMakerChecker:
    def test_the_ruling_opens_a_request_and_writes_only_when_approved(self, geo, policies):
        out = StringIO()
        call_command('propose_approval_policy', country='IN', action='finance.refund', limit=5_000,
                     daily_limit=25_000, per_target=True, ruling="Srini's ruling (test)", stdout=out)
        req = ApprovalRequest.objects.get(action_key='approvals.policy_change')
        assert (req.status, req.maker_label) == ('requested', 'system:owner_ruling')
        from servers.approvals.service import active_policy
        assert active_policy('IN', 'finance.refund').maker_daily_limit is None   # nothing yet
        executors.execute_policy_change(req)                                     # the checker's approval
        p = active_policy('IN', 'finance.refund')
        assert (p.maker_limit, p.maker_daily_limit, p.cumulative_per_target) == (5_000, 25_000, True)


class TestReceiptShowsTheRefund:
    def test_refund_lines_and_net_charged_in_html_and_invoice(self, geo, world, limits):
        from servers.ride.models import TripMoneyAction
        from servers.ride.receipts import _render_receipt_html, receipt_invoice

        trip = paid_trip(world, 20_000)
        TripMoneyAction.objects.create(trip=trip, kind='refund', amount_minor=15_000, currency='INR',
                                       method='gateway', status='completed', reason='test')
        inv = receipt_invoice(trip, total=Decimal('200.00'), gst_amount=Decimal('0'), currency='INR')
        assert [(x['label'], x['amount']) for x in inv['refunds']] == [('Refunded to your card', '150.00')]
        assert inv['net_charged'] == '50.00'
        html = _render_receipt_html(trip=trip, rider=trip.user_id, receipt_number='SG-T', gst_rate=Decimal('0'),
                                    gst_amount=Decimal('0'), fare_pricing=None, currency='INR')
        assert 'Refunded to your card' in html and 'Net charged' in html


class TestStripeReadyOnlyWhenStripeSaysSo:
    def drv(self, acct='acct_qa_test_seed', status='ready'):
        saved = []
        return SimpleNamespace(pk=30, stripe_account_id=acct, stripe_onboarding_status=status,
                               save=lambda update_fields: saved.append(update_fields))

    @pytest.mark.parametrize('answer, expected', [
        ({'_unknown_account': True}, 'not_started'),
        ({'payouts_enabled': True, 'details_submitted': True, 'capabilities': {'transfers': 'active'}}, 'ready'),
        ({'payouts_enabled': True, 'details_submitted': True, 'capabilities': {'transfers': 'inactive'}},
         'in_progress'),
        ({'payouts_enabled': False, 'details_submitted': False}, 'in_progress'),
    ])
    def test_status(self, monkeypatch, settings, answer, expected):
        from servers.driver import stripe_connect
        settings.STRIPE_SECRET_KEY = 'sk_test_x'
        monkeypatch.setattr(stripe_connect.vendor, 'stripe_account', lambda _id: answer)
        d = self.drv()
        assert stripe_connect.refresh_status(d) == expected
        assert (d.stripe_account_id == '') is (expected == 'not_started')

    def test_a_placeholder_is_not_a_payout_handle(self, monkeypatch, settings):
        from servers.driver import stripe_connect
        from servers.payments.weekly_payout import has_payout_handle
        settings.STRIPE_SECRET_KEY = 'sk_test_x'
        monkeypatch.setattr(stripe_connect.vendor, 'stripe_account', lambda _id: {'_unknown_account': True})
        assert has_payout_handle(self.drv(), 'USD') is False
        monkeypatch.setattr(stripe_connect.vendor, 'stripe_account', lambda _id: {
            'payouts_enabled': True, 'details_submitted': True, 'capabilities': {'transfers': 'active'}})
        d = self.drv(acct='acct_real', status='in_progress')
        d.pk = 31
        assert has_payout_handle(d, 'USD') is True
