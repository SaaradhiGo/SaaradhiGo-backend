"""Provider modes from the console (owner ruling 25 Nov): live, test or off per type and country,
through a second person; keys stay in the environment; test and off hold a channel back even where
the environment would send live; calls in test use the fake bridge and off refuses them."""
import pytest

from servers.approvals.models import ApprovalRequest
from servers.callbridge.providers import CallingUnavailable, FakeProvider, get_provider
from servers.notifications import config as nconfig
from servers.platform_ops.provider_mode import mode_for
from test_finance_actions import approve_request, body_of, checker, error_code, post
from test_pricing_module import geo, operator  # noqa: F401

pytestmark = pytest.mark.django_db
API = '/api/v1/platform/admin/integrations/'


def switch(maker, **body):
    return post(maker, f'{API}mode/', {'type': 'sms', 'country': 'IN', 'mode': 'test', 'reason': 'Before go-live', **body})


class TestProviderMode:
    def test_a_switch_waits_for_a_second_person_then_holds_sms_at_test(self, geo, settings):
        maker, _ = operator('super_admin', geo)
        settings.NOTIFY_LIVE_SEND = True
        settings.NOTIFY_PROVIDERS = {'sms': {'IN': 'msg91'}}
        r = switch(maker)
        assert r.status_code == 202, r.content[:300]
        req = ApprovalRequest.objects.get(pk=body_of(r)['approval_request']['id'])
        assert req.payload['display']['changes'][0]['to'] == 'Test (nobody receives it)'
        assert mode_for('sms', 'IN') is None
        chk, _ = checker()
        assert approve_request(chk, req.id).status_code == 200
        assert mode_for('sms', 'IN') == 'test'
        assert nconfig.effective_provider('sms', 'IN') == ('stub', False)

    def test_calls_follow_the_mode(self, geo):
        maker, _ = operator('super_admin', geo)
        chk, _ = checker()
        approve_request(chk, body_of(switch(maker, type='calling', mode='off'))['approval_request']['id'])
        with pytest.raises(CallingUnavailable):
            get_provider('IN')
        approve_request(chk, body_of(switch(maker, type='calling', mode='test'))['approval_request']['id'])
        assert isinstance(get_provider('IN'), FakeProvider)

    def test_payment_keys_are_not_switchable_and_reasons_are_required(self, geo):
        maker, _ = operator('super_admin', geo)
        r = switch(maker, type='payment_gateway')
        assert r.status_code == 400 and error_code(r) == 'NOT_SWITCHABLE'
        r = switch(maker, reason='')
        assert r.status_code == 400 and error_code(r) == 'REASON_REQUIRED'

    def test_the_status_page_says_which_rows_switch_and_shows_no_key(self, geo):
        maker, _ = operator('super_admin', geo)
        rows = body_of(maker.get(API))['rows']
        calling = [r for r in rows if r['type'] == 'calling']
        assert {r['country'] for r in calling} >= {'IN', 'US'} and all(r['switchable'] for r in calling)
        assert not any(r['switchable'] for r in rows if r['type'] == 'payment_gateway')
