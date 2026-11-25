"""Console final pass (owner, 24 Nov): every count matches the list it links to, safety tickets are
treated like safety, approvals say exactly what changes, and each market reconciles its own gateway."""

import pytest
from django.contrib.auth import get_user_model

from servers.admin_access.models import GeoNode
from servers.approvals.describe import actor_words, describe
from servers.driver.admin_drivers import kyc_state_q
from servers.driver.models import Driver
from servers.payments.reconciliation import gateways_for
from servers.platform_ops.integrations import waits_on
from test_command_centre import world  # noqa: F401 - fixture
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

TODAY = '/api/v1/command-centre/admin/today/'
KYC_QUEUE = '/api/v1/driver/admin/kyc-queue/'


class TestCountsMatchTheirLists:
    def test_kyc_waiting_is_the_kyc_queue_and_uploading_is_separate(self, geo, world):
        GeoNode.objects.filter(code='IN-TG-HYD').update(status='active')
        sa, _ = operator('super_admin', geo)
        hyd = GeoNode.objects.get(code='IN-TG-HYD')

        data = sa.get(TODAY).json()['data']
        city = next(c for c in data['cities'] if c['code'] == 'IN-TG-HYD')
        acts = {a['key']: a for a in city['actions']}
        queue = sa.get(f'{KYC_QUEUE}?scope=IN-TG-HYD').json()['data']

        in_review = Driver.objects.filter(kyc_state_q('in_review'), home_city=hyd).count()
        uploading = Driver.objects.filter(kyc_state_q('pending'), home_city=hyd).count()
        assert acts['kyc']['count'] == in_review == queue['count']
        assert acts['kyc_uploading']['count'] == uploading
        assert 'kyc_state=pending' in acts['kyc_uploading']['link']
        assert acts['kyc_uploading']['label'] == ('driver still uploading documents' if uploading == 1
                                                  else 'drivers still uploading documents')


class TestSafetyTicketsAreSafety:
    def test_a_safety_concern_opens_a_safety_incident(self, django_capture_on_commit_callbacks):
        from servers.safety.models import LINK_TICKET, SafetyIncidentLink
        from servers.support.models import SupportTicket

        user = get_user_model().objects.create_user(phone_number='+919800000001', role='rider')
        with django_capture_on_commit_callbacks(execute=True):
            ticket = SupportTicket.objects.create(user_id=user, issue_type='safety', description='Driver drove unsafely')

        assert ticket.priority == 'urgent'
        assert SafetyIncidentLink.objects.filter(kind=LINK_TICKET, target_id=ticket.id).exists()

    def test_the_concern_waits_in_needs_action(self, geo, django_capture_on_commit_callbacks):
        from servers.support.models import SupportTicket

        sa, _ = operator('super_admin', geo)
        user = get_user_model().objects.create_user(phone_number='+919800000003', role='rider')
        with django_capture_on_commit_callbacks(execute=True):
            SupportTicket.objects.create(user_id=user, issue_type='safety', description='Unsafe driving')

        items = sa.get('/api/v1/command-centre/admin/needs-action/').json()['data']['items']
        assert any('safety concern raised in support' in i['title'] for i in items), items
        board = sa.get('/api/v1/safety/admin/board/').json()['data']
        assert board['lanes']['not_acknowledged'], board['lanes']


class TestApprovalsSayWhatChanges:
    def test_a_daily_fee_plan_names_the_place_and_amounts(self):
        from servers.pricing.fee_plan_models import DriverFeePlan

        plan = DriverFeePlan.objects.create(country='IN', scope_code='IN-TG', version=99, currency='INR', plan_type='daily_pass',
                                            amounts={'auto': {'daily_pass_minor': 2000}, 'sedan': {'daily_pass_minor': 4900}})
        row = type('Row', (), {'payload': {'kind': 'driver_fee_plan', 'plan_id': plan.id}})()

        words = describe(row, 'Change to fare cards')

        assert words.startswith('Telangana daily driver fee: ') or words.startswith('IN-TG daily driver fee: ')
        assert 'Auto ₹20 a day' in words and 'Sedan ₹49 a day' in words and '.00' not in words

    def test_automatic_makers_read_in_words(self):
        assert actor_words('system:weekly_payout') == 'Weekly payout run (automatic)'
        assert actor_words('system:something_new') == 'Something new (automatic)'
        assert actor_words('Asha Rao') == 'Asha Rao'


class TestPlainWordsAndGateways:
    def test_the_us_reconciles_stripe_only(self):
        assert gateways_for('US') == ('stripe',)
        assert gateways_for('IN') == ('razorpay', 'cashfree')

    def test_a_blocker_says_what_and_who(self):
        assert 'DLT' in waits_on(18) and 'Who fixes it: Srini' in waits_on(18)
        assert waits_on(None) == ''
