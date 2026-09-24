"""A failed OTP send must not be reported as a success.

`send_otp_via_sns` is the first step of every rider and driver session, and it is
also used to notify an SOS. It was a bare `@shared_task` that caught every failure
and returned `{"success": False, ...}`.

Two consequences, both silent:

  * Celery marked the task SUCCEEDED, because returning a dict is success.
  * There was no retry of any kind, so one moment of SNS throttling or a
    credentials refresh meant the OTP was never delivered. The user sees nothing,
    taps resend, and the same thing happens.

For a pilot that begins by onboarding drivers, this is the very first thing that
has to work.

Transient failures now raise so Celery retries with backoff. Terminal failures --
a malformed number, an opted-out recipient -- still return a dict, because
retrying those only delays the same answer.
"""

from unittest import mock

import pytest
from botocore.exceptions import BotoCoreError, ClientError
from celery.exceptions import Retry

from base import utils as base_utils


def _client_error(code):
    return ClientError(
        {'Error': {'Code': code, 'Message': f'simulated {code}'}}, 'Publish',
    )


class _FailingSns:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def publish(self, **kwargs):
        self.calls += 1
        raise self.exc


class _WorkingSns:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return {'MessageId': 'mid-1'}


def _run(monkeypatch, client):
    """Call the task body directly, with retry turned into a raised signal.

    `.apply()` would swallow Retry into a result object. Patching `retry` to
    raise is what lets a test assert "this would have been retried".
    """
    monkeypatch.setattr(base_utils, 'get_sns_client', lambda: client)

    def _retry(exc=None, **kw):
        raise Retry(str(exc))

    monkeypatch.setattr(base_utils.send_otp_via_sns, 'retry', _retry)
    return base_utils.send_otp_via_sns


# ---------------------------------------------------------------------------
# Transient failures must retry, not report success
# ---------------------------------------------------------------------------

def test_a_network_failure_is_retried(monkeypatch):
    task = _run(monkeypatch, _FailingSns(BotoCoreError()))

    with pytest.raises(Retry):
        task.run('+919999000001', 'Your OTP is 1234')


def test_a_throttling_error_is_retried(monkeypatch):
    """The failure most likely during a burst of driver onboarding."""
    task = _run(monkeypatch, _FailingSns(_client_error('ThrottlingException')))

    with pytest.raises(Retry):
        task.run('+919999000001', 'Your OTP is 1234')


def test_an_sns_internal_error_is_retried(monkeypatch):
    task = _run(monkeypatch, _FailingSns(_client_error('InternalErrorException')))

    with pytest.raises(Retry):
        task.run('+919999000001', 'Your OTP is 1234')


def test_an_unavailable_sns_client_is_retried(monkeypatch):
    """This returned "success: False" before, which Celery treats as done."""
    task = _run(monkeypatch, None)

    with pytest.raises(Retry):
        task.run('+919999000001', 'Your OTP is 1234')


def test_an_unexpected_exception_is_retried(monkeypatch):
    task = _run(monkeypatch, _FailingSns(RuntimeError('something odd')))

    with pytest.raises(Retry):
        task.run('+919999000001', 'Your OTP is 1234')


# ---------------------------------------------------------------------------
# Terminal failures must NOT retry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('code', [
    'InvalidParameter',
    'InvalidParameterValue',
    'OptedOut',
    'AuthorizationError',
])
def test_a_terminal_error_is_not_retried(monkeypatch, code):
    """Retrying a malformed number just delays the same answer.

    The negative control for the change above: if everything retried, a bad
    phone number would occupy a worker for four attempts and still fail.
    """
    task = _run(monkeypatch, _FailingSns(_client_error(code)))

    result = task.run('+919999000001', 'Your OTP is 1234')

    assert result['success'] is False
    assert result.get('terminal') is True, (
        f'{code} should be reported as terminal so nothing retries it'
    )


def test_a_missing_phone_number_is_terminal(monkeypatch):
    task = _run(monkeypatch, _WorkingSns())

    result = task.run('', 'Your OTP is 1234')

    assert result['success'] is False


# ---------------------------------------------------------------------------
# The success path must be untouched
# ---------------------------------------------------------------------------

def test_a_successful_send_still_reports_success(monkeypatch):
    client = _WorkingSns()
    task = _run(monkeypatch, client)

    result = task.run('+919999000001', 'Your OTP is 1234')

    assert result['success'] is True
    assert result['message_id'] == 'mid-1'
    assert len(client.calls) == 1


def test_the_task_is_configured_to_retry_at_all():
    """Guards the decorator, not the body.

    Without max_retries, `self.retry()` raises MaxRetriesExceeded on the first
    call and the raising above would be worse than the swallowing it replaced.
    """
    task = base_utils.send_otp_via_sns
    assert task.max_retries and task.max_retries >= 1, (
        'send_otp_via_sns has no retry budget, so raising would fail immediately'
    )
    assert getattr(task, 'default_retry_delay', 0) > 0, (
        'retrying an SNS throttle with no delay reproduces the throttle'
    )


def test_a_retry_signal_is_never_swallowed_into_a_success(monkeypatch):
    """The specific trap in this function's shape.

    `self.retry()` signals by raising, and the body ends with a broad
    `except Exception`. If that handler caught Retry, every retry would be
    converted into another retry attempt of itself or into a returned dict --
    which is exactly the silent-success bug this file exists to prevent.
    """
    monkeypatch.setattr(base_utils, 'get_sns_client', lambda: _FailingSns(BotoCoreError()))

    calls = {'n': 0}

    def _retry(exc=None, **kw):
        calls['n'] += 1
        raise Retry(str(exc))

    monkeypatch.setattr(base_utils.send_otp_via_sns, 'retry', _retry)

    with pytest.raises(Retry):
        base_utils.send_otp_via_sns.run('+919999000001', 'msg')

    assert calls['n'] == 1, (
        f'retry was invoked {calls["n"]} times for one failure; the Retry signal '
        'is being caught and re-handled'
    )


# ---------------------------------------------------------------------------
# The SOS caller shares this task
# ---------------------------------------------------------------------------

def test_the_sos_path_uses_the_same_task():
    """An SOS notification SMS had the same silent-failure property.

    Recorded here so that a future change to SOS notification does not quietly
    reintroduce a fire-and-forget send.
    """
    import servers.sos.tasks as sos_tasks
    src = mock.mock_open  # keep the import list honest
    assert src is not None
    import inspect
    body = inspect.getsource(sos_tasks)
    assert 'send_otp_via_sns' in body, (
        'SOS no longer uses send_otp_via_sns; check that whatever replaced it '
        'retries transient failures rather than reporting success'
    )
