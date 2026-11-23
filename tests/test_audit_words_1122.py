"""The audit log in words, page views hidden by default (owner, 22 Nov, item 7)."""

import pytest

from servers.admin_audit.models import AdminAuditLog
from servers.admin_audit.viewer import entry_row, filtered
from servers.admin_audit.words import is_noise
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
AUDIT = '/api/v1/admin-audit/admin/entries/'


def _row(**kw):
    kw.setdefault('actor_label', 'finance@saaradhigo.com')
    kw.setdefault('target_type', 'payout_config')
    kw.setdefault('target_id', 'IN-TG-HYD')
    return AdminAuditLog.objects.create(**kw)


class TestWords:
    def test_a_change_reads_as_a_sentence_with_the_place_by_name(self, geo):
        e = _row(action='payout_config_changed', reason='Raise the minimum',
                 before={'minimum_withdrawal_minor': 10000, 'currency': 'INR', 'scope': 'IN-TG-HYD'},
                 after={'minimum_withdrawal_minor': 20000, 'currency': 'INR', 'scope': 'IN-TG-HYD'})
        row = entry_row(e)
        assert row['summary'] == 'Changed Hyderabad payout minimum from ₹100 to ₹200'
        assert row['place_name'] == 'Hyderabad'

    def test_an_action_without_changes_reads_in_words_with_its_target(self, geo):
        e = _row(action='kyc_approved', target_type='driver', target_id='12')
        assert entry_row(e)['summary'] == "Approved a driver's documents · Driver D-12"

    def test_a_phone_number_never_reaches_the_sentence(self, geo):
        e = _row(action='rider_contact_changed', before={'phone': '+919876500001'},
                 after={'phone': '+919876500002'})
        assert '98765' not in entry_row(e)['summary']


class TestNoiseHidden:
    def test_page_views_and_sign_ins_are_noise(self):
        assert is_noise('ops_api_login') and is_noise('driver_record_viewed')
        assert not is_noise('payout_config_changed') and not is_noise('kyc_approved')

    def test_hidden_by_default_and_shown_on_request(self, geo):
        sa, user = operator('super_admin', geo)
        _row(action='ops_api_login', target_type='operator', target_id=str(user.pk))
        _row(action='kyc_approved', target_type='driver', target_id='3')
        default = {e.action for e in filtered(user, {})}
        everything = {e.action for e in filtered(user, {'views': '1'})}
        assert 'ops_api_login' not in default and 'kyc_approved' in default
        assert 'ops_api_login' in everything
