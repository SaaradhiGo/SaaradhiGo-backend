"""Launch round 3, item 4: Exotel for India's masked calls and SOS escalation calls.

QA stays on the fake until keys exist. No network: requests is replaced.
"""
from types import SimpleNamespace
from unittest import mock

import pytest
from django.utils import timezone

from servers.callbridge.providers import CallingUnavailable, FakeProvider, get_provider


def _resp(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body)


@pytest.fixture
def exotel(settings):
    settings.CALL_BRIDGE_PROVIDERS = {'US': 'twilio', 'IN': 'exotel'}
    settings.CALL_BRIDGE_REAL_ENABLED = True
    settings.EXOTEL_ACCOUNT_SID = 'saaradhigo-test'
    settings.EXOTEL_API_KEY, settings.EXOTEL_API_TOKEN = 'key-x', 'token-x'
    settings.EXOTEL_CALLER_ID = '04000000000'
    settings.EXOTEL_SUBDOMAIN = 'api.in.exotel.com'
    settings.EXOTEL_SOS_FLOW_ID = '123456'
    return settings


def _session(minutes=20):
    return SimpleNamespace(pk=7, trip_id=1, expires_at=timezone.now() + timezone.timedelta(minutes=minutes),
                           provider_ref='')


def test_india_uses_exotel(exotel):
    assert get_provider('IN').name == 'exotel'


def test_masked_call_rings_the_caller_then_the_other_party(exotel):
    with mock.patch('integrations.voice.exotel_voice.requests.post',
                    return_value=_resp(200, {'Call': {'Sid': 'ex-1', 'Status': 'in-progress'}})) as post:
        bridge = get_provider('IN').start(_session(), '+919700000001', '+919700000002')
    assert bridge.kind == 'session' and bridge.number == '' and bridge.ref == 'ex-1'
    url = post.call_args.args[0]
    assert url == 'https://api.in.exotel.com/v1/Accounts/saaradhigo-test/Calls/connect.json'
    assert post.call_args.kwargs['auth'] == ('key-x', 'token-x')
    data = post.call_args.kwargs['data']
    assert data['From'] == '+919700000001' and data['To'] == '+919700000002'
    assert data['CallerId'] == '04000000000' and data['CallType'] == 'trans'
    assert 1100 <= int(data['TimeLimit']) <= 1200


def test_time_limit_never_exceeds_exotels_ceiling(exotel):
    with mock.patch('integrations.voice.exotel_voice.requests.post',
                    return_value=_resp(200, {'Call': {'Sid': 'ex-2'}})) as post:
        get_provider('IN').start(_session(minutes=600), '+919700000001', '+919700000002')
    assert post.call_args.kwargs['data']['TimeLimit'] == '14400'


def test_sos_page_rings_into_the_flow(exotel):
    with mock.patch('integrations.voice.exotel_voice.requests.post',
                    return_value=_resp(200, {'Call': {'Sid': 'ex-3'}})) as post:
        ref = get_provider('IN').page('+919700000009', 'SaaradhiGo SOS SOS-1 not acknowledged.', 'sos_escalation')
    data = post.call_args.kwargs['data']
    assert ref == 'ex-3'
    assert data['From'] == '+919700000009' and 'To' not in data
    assert data['Url'] == 'http://my.exotel.com/saaradhigo-test/exoml/start_voice/123456'
    assert data['CustomField'] == 'sos_escalation'


def test_page_without_a_flow_is_unavailable(exotel):
    exotel.EXOTEL_SOS_FLOW_ID = ''
    with pytest.raises(CallingUnavailable):
        get_provider('IN').page('+919700000009', 'm', 'sos_escalation')


def test_refusal_and_missing_keys_are_unavailable(exotel):
    with mock.patch('integrations.voice.exotel_voice.requests.post', return_value=_resp(401, {})):
        with pytest.raises(CallingUnavailable):
            get_provider('IN').start(_session(), '+919700000001', '+919700000002')
    exotel.EXOTEL_API_TOKEN = ''
    with pytest.raises(CallingUnavailable):
        get_provider('IN')


def test_qa_without_keys_stays_on_the_fake(settings):
    settings.CALL_BRIDGE_PROVIDERS = {'US': 'twilio'}
    settings.CALL_BRIDGE_PROVIDER = 'fake'
    assert isinstance(get_provider('IN'), FakeProvider)
