"""The login one-time code over AWS SNS: the vendor half of `base.utils.send_otp_via_sns`.

Moved behind `integrations/` so `base/` holds no vendor import. BEHAVIOUR IS UNCHANGED: same
client construction (AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY), same publish call,
same split between terminal failures (never retried) and transient ones (retried by Celery).
This path is deliberately NOT rewired onto the notifications pipeline here; that is the separate
app catch-up backend item.
"""

import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

logger = logging.getLogger(__name__)

# Transient AWS conditions are worth another attempt. An invalid phone number will not become
# valid, so these stay terminal and are not retried.
SNS_TERMINAL_ERROR_CODES = frozenset({
    'InvalidParameter',
    'InvalidParameterValue',
    'OptedOut',          # the recipient has opted out of SMS
    'AuthorizationError',
})


class OtpTerminalError(Exception):
    """AWS refused the code for good (malformed number, opted out). Do not retry."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class OtpTransientError(Exception):
    """A throttle, credentials blip or network error. Worth another attempt."""

    def __init__(self, code, cause):
        super().__init__(code)
        self.code, self.cause = code, cause


def get_sns_client():
    """AWS SNS client for the login OTP, or None if initialisation fails."""
    try:
        return boto3.client(
            "sns",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
    except Exception as e:
        logger.error(f"Failed to initialize SNS client: {str(e)}")
        return None


def publish_otp(client, phone_number, message):
    """Publish the code. Returns the SNS MessageId.

    Raises OtpTerminalError or OtpTransientError. Any other exception propagates unchanged.
    """
    try:
        resp = client.publish(
            PhoneNumber=phone_number,
            Message=message,
            MessageAttributes={
                'AWS.SNS.SMS.SenderID': {
                    'DataType': 'String',
                    'StringValue': settings.AWS_SNS_SENDER_ID
                },
                'AWS.SNS.SMS.SMSType': {
                    'DataType': 'String',
                    'StringValue': 'Transactional'
                }
            }
        )
    except ClientError as e:
        code = (e.response or {}).get('Error', {}).get('Code', '')
        if code in SNS_TERMINAL_ERROR_CODES:
            raise OtpTerminalError(
                code, f"AWS error: {e.response.get('Error', {}).get('Message', str(e))}") from e
        raise OtpTransientError(code, e) from e
    except BotoCoreError as e:
        # Connection, endpoint and timeout errors all land here. Always worth another attempt.
        raise OtpTransientError('botocore_error', e) from e
    return resp.get('MessageId')
