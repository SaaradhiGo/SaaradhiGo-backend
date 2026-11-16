"""The login OTP never travels as a Celery task argument (AUDIT 27.76).

Task arguments go to the Redis broker, the worker's "received" log line and,
through Sentry's CeleryIntegration, to every event from a failing task. The
login view used to enqueue `send_otp_via_sns(phone, "Your OTP for X is
123456. ...")`. Now the text is stashed in the cache and only a random
reference is enqueued; the task reads it, sends it and deletes it.

Also pinned here: the Sentry scrubber (base/sentry_scrub.py) and the log
filter's handling of the OTP as it reads in the SMS text.
"""

import json
import logging
from unittest import mock

import pytest
from botocore.exceptions import BotoCoreError
from celery.exceptions import Retry
from django.core.cache import cache
from rest_framework.test import APIClient

from base import utils as base_utils
from base.logging_filters import PIIRedactionFilter
from base.sentry_scrub import FILTERED, before_breadcrumb, before_send

pytestmark = pytest.mark.django_db

PHONE = '+919888000077'
OTP = '482913'


def _flatten(value):
    return json.dumps(value, default=str)


def _request_otp():
    with mock.patch('servers.auth_user.views.generate_otp', return_value=OTP), \
            mock.patch('servers.auth_user.views.send_otp_via_sns.delay',
                       return_value='task-1') as delay:
        response = APIClient().post('/api/v1/auth/otp/',
                                    {'phone_number': PHONE, 'role': 'rider'}, format='json')
    assert response.status_code == 200, response.content
    delay.assert_called_once()
    return delay.call_args


class _Sns:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def publish(self, **kwargs):
        if self.fail:
            raise BotoCoreError()
        self.sent.append(kwargs)
        return {'MessageId': 'mid-1'}


def _run_task(monkeypatch, sns, phone, ref):
    monkeypatch.setattr(base_utils, 'get_sns_client', lambda: sns)

    def _retry(exc=None, **kw):
        raise Retry(str(exc))

    monkeypatch.setattr(base_utils.send_otp_via_sns, 'retry', _retry)
    return base_utils.send_otp_via_sns.run(phone, ref)


# ---------------------------------------------------------------------------
# The enqueue
# ---------------------------------------------------------------------------

class TestTheLoginEnqueue:
    @pytest.fixture(autouse=True)
    def _no_test_phones(self, settings):
        settings.TEST_PHONE_NUMBERS = {}

    def test_the_task_arguments_carry_no_otp(self):
        call = _request_otp()
        assert OTP not in _flatten(call.args) and OTP not in _flatten(call.kwargs)
        assert 'OTP' not in _flatten(call.args)  # nor the message text at all
        assert call.args[0] == PHONE

    def test_the_reference_resolves_to_the_message_with_the_otp(self, settings):
        call = _request_otp()
        message = cache.get(f'{base_utils.SMS_MESSAGE_CACHE_PREFIX}{call.args[1]}')
        # 13 Nov: the stash carries the template kind and values beside the text (MSG91 DLT).
        message = message['text'] if isinstance(message, dict) else message
        assert message == (f'Your OTP for {settings.PLATFORM_BRAND_NAME} is {OTP}. '
                           f'It will expire in 10 minutes.')


class TestTheSosEnqueue:
    def test_the_emergency_contact_sms_is_enqueued_by_reference(self, django_user_model):
        from servers.sos.models import SOSEvent
        from servers.sos.tasks import dispatch_sos

        user = django_user_model.objects.create_user(
            phone_number='+919888000078', role='rider', full_name='Asha')
        user.emergency_contact = '+919888000079'
        user.save(update_fields=['emergency_contact'])
        event = SOSEvent.objects.create(user=user, user_label='Asha', initiated_by='rider',
                                        event_type='panic', status='open')
        with mock.patch('base.utils.send_otp_via_sns.delay') as delay, \
                mock.patch('servers.auth_user.services.send_push_notification',
                           return_value=True):
            dispatch_sos(event.id)

        delay.assert_called_once()
        phone, ref = delay.call_args.args
        assert phone == '+919888000079'
        assert 'Asha' not in _flatten(delay.call_args.args)
        assert 'SOS' in cache.get(f'{base_utils.SMS_MESSAGE_CACHE_PREFIX}{ref}')['text']


# ---------------------------------------------------------------------------
# The task
# ---------------------------------------------------------------------------

class TestTheTask:
    def test_it_sends_the_stashed_text_and_deletes_it(self, monkeypatch):
        ref = base_utils.stash_sms_message(f'Your OTP is {OTP}')
        sns = _Sns()
        result = _run_task(monkeypatch, sns, PHONE, ref)
        assert result['success'] is True
        assert [m['Message'] for m in sns.sent] == [f'Your OTP is {OTP}']
        assert sns.sent[0]['PhoneNumber'] == PHONE
        assert cache.get(f'{base_utils.SMS_MESSAGE_CACHE_PREFIX}{ref}') is None

    def test_a_retried_send_keeps_the_text_for_the_retry(self, monkeypatch):
        ref = base_utils.stash_sms_message(f'Your OTP is {OTP}')
        with pytest.raises(Retry):
            _run_task(monkeypatch, _Sns(fail=True), PHONE, ref)
        assert cache.get(f'{base_utils.SMS_MESSAGE_CACHE_PREFIX}{ref}') == f'Your OTP is {OTP}'

    def test_a_missing_or_expired_reference_is_terminal_and_sends_nothing(self, monkeypatch):
        sns = _Sns()
        result = _run_task(monkeypatch, sns, PHONE, 'no-such-ref')
        assert result['success'] is False and result['terminal'] is True
        assert sns.sent == []

    def test_plaintext_passed_as_the_reference_is_not_sent(self, monkeypatch):
        """An old-style call (the text itself) must not quietly keep working."""
        sns = _Sns()
        result = _run_task(monkeypatch, sns, PHONE, f'Your OTP is {OTP}')
        assert result['success'] is False and sns.sent == []


# ---------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------

def _synthetic_event():
    return {
        'message': f'Your OTP for SaaradhiGo is {OTP}. It will expire in 10 minutes.',
        'exception': {'values': [{
            'type': 'RuntimeError',
            'value': f'SNS publish failed for message "Your OTP for SaaradhiGo is {OTP}"',
            'stacktrace': {'frames': [{
                'function': 'send_otp_via_sns', 'lineno': 10,
                'vars': {'message': f'Your OTP is {OTP}', 'retries': 1},
            }]},
        }]},
        'extra': {
            'celery-job': {
                'task_name': 'base.utils.send_otp_via_sns',
                'args': [PHONE, f'Your OTP for SaaradhiGo is {OTP}.'],
                'kwargs': {'message': f'Your OTP is {OTP}'},
            },
            'otp': OTP,
            'note': f'otp={OTP}',
        },
        'request': {
            'url': 'https://api.example.test/api/v1/auth/login/',
            'method': 'POST',
            'data': {'phone_number': PHONE, 'otp': OTP, 'password': 'aaaa',
                     'refresh_token': 'abc.def.ghi', 'nested': {'client_secret': 's3', 'ok': 'y'}},
            'headers': {'Authorization': 'Bearer abc.def', 'Cookie': 'sessionid=xyz',
                        'X-Api-Key': 'k', 'Content-Type': 'application/json'},
        },
        'tags': {'task': 'send_otp_via_sns'},
    }


class TestTheSentryScrubber:
    def test_no_otp_survives_anywhere_in_the_event(self):
        scrubbed = before_send(_synthetic_event(), {})
        assert OTP not in _flatten(scrubbed)

    def test_celery_job_args_and_kwargs_are_removed(self):
        job = before_send(_synthetic_event(), {})['extra']['celery-job']
        assert job['args'] == FILTERED and job['kwargs'] == FILTERED
        assert job['task_name'] == 'base.utils.send_otp_via_sns'  # still useful

    def test_secret_keys_in_request_data_and_headers_are_filtered(self):
        request = before_send(_synthetic_event(), {})['request']
        for key in ('otp', 'password', 'refresh_token'):
            assert request['data'][key] == FILTERED, key
        assert request['data']['nested'] == {'client_secret': FILTERED, 'ok': 'y'}
        for key in ('Authorization', 'Cookie', 'X-Api-Key'):
            assert request['headers'][key] == FILTERED, key
        assert request['headers']['Content-Type'] == 'application/json'

    def test_the_exception_message_is_scrubbed_but_kept(self):
        value = before_send(_synthetic_event(), {})['exception']['values'][0]
        assert value['value'].startswith('SNS publish failed') and OTP not in value['value']
        assert value['type'] == 'RuntimeError'
        assert value['stacktrace']['frames'][0]['function'] == 'send_otp_via_sns'

    def test_breadcrumbs_are_scrubbed(self):
        crumb = {'category': 'celery', 'message': f'OTP for {PHONE} is {OTP}',
                 'data': {'otp': OTP, 'token': 't'}}
        out = before_breadcrumb(crumb, {})
        assert OTP not in _flatten(out) and out['category'] == 'celery'

    def test_a_scrubber_failure_drops_the_event(self):
        with mock.patch('base.sentry_scrub.scrub', side_effect=RuntimeError('boom')):
            assert before_send(_synthetic_event(), {}) is None

    def test_settings_wire_both_hooks_into_sentry(self):
        import inspect

        import base.settings
        source = inspect.getsource(base.settings)
        assert 'before_send=_sentry_before_send' in source
        assert 'before_breadcrumb=_sentry_before_breadcrumb' in source


# ---------------------------------------------------------------------------
# The log filter
# ---------------------------------------------------------------------------

class TestTheLogFilter:
    @pytest.mark.parametrize('text', [
        f'Your OTP for SaaradhiGo is {OTP}. It will expire in 10 minutes.',
        f'OTP for {PHONE} is {OTP}',
        f'your verification code is {OTP}',
    ])
    def test_the_otp_in_prose_is_redacted(self, text):
        record = logging.LogRecord('t', logging.INFO, __file__, 1, text, (), None)
        PIIRedactionFilter().filter(record)
        assert OTP not in record.getMessage()

    def test_a_phone_number_after_the_word_is_left_to_the_phone_mask(self):
        record = logging.LogRecord('t', logging.INFO, __file__, 1,
                                   f'OTP sent to {PHONE[:5]}***, task_id: abc', (), None)
        PIIRedactionFilter().filter(record)
        assert record.getMessage() == f'OTP sent to {PHONE[:5]}***, task_id: abc'
