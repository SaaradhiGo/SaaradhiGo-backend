"""Shared plumbing for the AWS SNS adapters (SMS and push).

SANDBOX-ONLY, BY CONSTRUCTION. An SNS adapter never builds a client, and therefore never
touches the network, unless BOTH switches are on:

  * NOTIFY_LIVE_SEND   the master notification switch (also enforced by `providers.build`);
  * SNS_LIVE_CONFIRMED "the owner confirms the AWS account" (default false).

With either off, `require_confirmed()` raises `SnsNotConfirmed`, and the dispatcher records the
message as `suppressed: sns not confirmed` (it asks `config.sns_blocked()` first, so the adapter
is normally never even constructed). Credentials are read by NAME from settings.
"""

from django.conf import settings

from servers.notifications import config
from servers.notifications.providers.base import (
    LiveSendDisabled, PermanentProviderError, TransientProviderError,
)

SUPPRESSION_REASON = 'sns not confirmed'

_TRANSIENT = {'Throttled', 'Throttling', 'ThrottlingException', 'InternalFailure',
              'InternalError', 'ServiceUnavailable', 'RequestTimeout', 'KMSThrottling'}


class SnsNotConfirmed(LiveSendDisabled):
    """SNS was asked to send while the owner has not confirmed the AWS account."""


def require_confirmed():
    if not config.live_send_enabled():
        raise SnsNotConfirmed('refusing to use AWS SNS: NOTIFY_LIVE_SEND is off')
    if not config.sns_confirmed():
        raise SnsNotConfirmed(f'refusing to use AWS SNS: {SUPPRESSION_REASON} '
                              '(SNS_LIVE_CONFIRMED is off)')


def client():
    """A boto3 SNS client from the SNS_AWS_* settings. Gated by `require_confirmed`."""
    import boto3

    require_confirmed()
    missing = [n for n in ('SNS_AWS_ACCESS_KEY_ID', 'SNS_AWS_SECRET_ACCESS_KEY', 'SNS_AWS_REGION')
               if not getattr(settings, n, '')]
    if missing:
        raise PermanentProviderError('SNS settings are not set: ' + ', '.join(missing))
    return boto3.client(
        'sns', region_name=settings.SNS_AWS_REGION,
        aws_access_key_id=settings.SNS_AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.SNS_AWS_SECRET_ACCESS_KEY)


def call(fn, label, **kwargs):
    """Invoke a client method, mapping botocore failures onto the adapter error types."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        return fn(**kwargs)
    except ClientError as exc:
        code = (exc.response.get('Error') or {}).get('Code', '')
        text = f'SNS refused the {label} ({code})'
        if code in _TRANSIENT:
            raise TransientProviderError(text) from None
        raise PermanentProviderError(text) from None
    except BotoCoreError as exc:
        raise TransientProviderError(f'SNS request failed: {type(exc).__name__}') from None
