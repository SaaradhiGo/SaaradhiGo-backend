import logging
import random
import secrets
from typing import Dict, Any, Optional
from django.utils import timezone
from rest_framework.response import Response
from django.conf import settings
from django.contrib.auth import get_user_model
from celery import shared_task
from celery.exceptions import Retry
from integrations.sms.sns_otp import (  # noqa: F401  (get_sns_client is patched by tests here)
    OtpTerminalError, OtpTransientError, get_sns_client, publish_otp,
)

logger = logging.getLogger(__name__)

Users = get_user_model()

# Character set for username generation
CHARACTERS = (
    [chr(i) for i in range(ord('a'), ord('z') + 1)] +
    [chr(i) for i in range(ord('A'), ord('Z') + 1)] +
    list(map(str, range(0, 10)))
)



def success_response(data: Any, status_code: int) -> Response:
    """
    Generate a standardized success response.
    
    Args:
        data: Response payload
        status_code: HTTP status code
    
    Returns:
        Response object with success status
    """
    return Response(
        {
            "status": "success",
            "data": data,
            "meta": {
                "timestamp": timezone.localtime(timezone.now())
            }
        },
        status=status_code
    )


def error_response(
    code: str,
    message: str,
    field: str,
    issue: str,
    status: int
) -> Response:
    """
    Generate a standardized error response.
    
    Args:
        code: Error code identifier
        message: Human-readable error message
        field: Field that caused the error
        issue: Detailed issue description
        status: HTTP status code
    
    Returns:
        Response object with error status
    """
    return Response(
        {
            "status": "error",
            "error": {
                "code": code or "UNKNOWN_ERROR",
                "message": message or "An error occurred",
                "details": {
                    "field": field or "general",
                    "issue": issue or "No details available"
                }
            }
        },
        status=status
    )


def generate_otp(n: int) -> str:
    """Generate a cryptographically secure n-digit OTP.

    Previously used random.choices(), which seeds from a non-CSPRNG that an
    attacker who observes a few outputs can predict. For a security token
    that gates account access we use secrets.choice (backed by os.urandom)
    so each digit is independently unpredictable.
    """
    if n <= 0:
        logger.warning(f"Invalid OTP length requested: {n}")
        return ""

    digits = '0123456789'
    return ''.join(secrets.choice(digits) for _ in range(n))


# The SMS text never travels as a task argument. It carries the login OTP, and
# task arguments are copied to places a secret must not go: the Redis broker,
# the worker's "received" log line, and Sentry, whose CeleryIntegration
# attaches args/kwargs to every event from a failing task. The caller stashes
# the text in the cache (Redis, shared by web and worker) under a random id,
# and only that id is enqueued. The task reads the text and deletes it once
# the send is final, so it lives no longer than one delivery.
SMS_MESSAGE_CACHE_PREFIX = 'sms_message:'
SMS_MESSAGE_TTL_SECONDS = 600  # the login OTP's own lifetime; a later send is useless anyway


def stash_sms_message(message: str, ttl: int = SMS_MESSAGE_TTL_SECONDS) -> str:
    """Store an SMS text for `send_otp_via_sns` and return the reference to enqueue."""
    import uuid
    from django.core.cache import cache

    ref = uuid.uuid4().hex
    cache.set(f'{SMS_MESSAGE_CACHE_PREFIX}{ref}', message, ttl)
    return ref


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    retry_backoff=True,
    retry_jitter=True,
)
def send_otp_via_sns(self, phone_number: str, message_ref: str) -> Dict[str, Any]:
    """
    Send OTP via AWS SNS service.

    This is the first step of every rider and driver session, and it is also used
    to notify an SOS. It used to catch every failure and return a dict, which made
    Celery record the task as SUCCEEDED even when no SMS was sent -- and there was
    no retry, so a moment of SNS throttling or a credentials blip meant the OTP
    was silently never delivered and the user simply could not log in.

    Transient failures now raise so Celery retries them, with backoff. Terminal
    failures (a malformed number, an opted-out recipient) still return a dict,
    because retrying those only delays the same answer.

    Args:
        phone_number: Recipient phone number (E.164 format)
        message_ref: what `stash_sms_message` returned. Never the text itself:
            see SMS_MESSAGE_CACHE_PREFIX for why.

    Returns:
        dict: Status and response, or terminal error details.

    Raises:
        Retry: transient AWS or network failure; Celery will try again.
    """
    from django.core.cache import cache

    message_key = f'{SMS_MESSAGE_CACHE_PREFIX}{message_ref}'
    try:
        # Validate inputs
        message = cache.get(message_key) if message_ref else None
        if not phone_number or not message:
            # Missing, expired (the TTL outlived every retry) or already sent.
            # Terminal: retrying cannot bring the text back.
            logger.warning("Missing phone_number or SMS message (expired or already sent)")
            return {
                "success": False,
                "error": "Phone number and message are required",
                "terminal": True,
            }
        
        client = get_sns_client()
        if client is None:
            # Configuration or credentials. Retry: this is commonly transient
            # (a credential refresh, a metadata endpoint hiccup) and returning
            # "success: False" here told Celery the task had completed.
            logger.error(
                "otp_send_failure reason=sns_client_unavailable retry=%s/%s",
                self.request.retries, self.max_retries,
            )
            raise self.retry(exc=RuntimeError('SNS client unavailable'))
        
        message_id = publish_otp(client, phone_number, message)
        cache.delete(message_key)

        logger.info(f"OTP sent successfully to {phone_number}")
        return {
            "success": True,
            "message_id": message_id
        }

    except OtpTerminalError as e:
        # Retrying will not help. Terminal, and reported as such.
        cache.delete(message_key)
        logger.error(
            "otp_send_terminal reason=sns_client_error code=%s", e.code,
        )
        return {
            "success": False,
            "error": e.message,
            "terminal": True,
        }
    except OtpTransientError as e:
        logger.error(
            "otp_send_failure reason=%s retry=%s/%s",
            'botocore_error' if e.code == 'botocore_error' else f'sns_client_error code={e.code}',
            self.request.retries, self.max_retries,
        )
        # Connection, endpoint and timeout errors, throttling and credentials blips: always
        # worth another attempt.
        raise self.retry(exc=e.cause)
    except Retry:
        # self.retry() signals by raising. Never swallow it into the generic
        # handler below, which is what would turn a retry into a silent success.
        raise
    except Exception as e:
        logger.exception(
            "otp_send_failure reason=unexpected retry=%s/%s",
            self.request.retries, self.max_retries,
        )
        raise self.retry(exc=e)


def wallet_payment(user, amount, purpose='Trip payment', reference_id=None, idempotency_key=None):
    """
    Initiate a direct payment from wallet without Razorpay.
    
    Args:
        user: The user making the payment
        amount: Amount to deduct from wallet
        purpose: Purpose of the payment (default: 'Trip payment')
        reference_id: Reference ID (e.g., trip ID)
        idempotency_key: Unique key to prevent duplicate transactions
    
    Returns:
        dict with success status and transaction details
    """
    import uuid
    from django.db import transaction
    from servers.rider.models import WalletTransaction, Wallet
    
    try:
        # Exact from the request onwards; no float touches the ledger.
        from base.money import parse_input
        amount_val = parse_input(amount)
        if amount_val <= 0:
            return {'success': False, 'error': 'Amount must be positive'}
    except (ValueError, TypeError):
        return {'success': False, 'error': 'Invalid amount provided'}
    
    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())
    
    existing_txn = WalletTransaction.objects.filter(idempotency_key=idempotency_key).first()
    if existing_txn:
        return {
            'success': True,
            'transaction_id': existing_txn.id,
            'status': existing_txn.status,
            'amount': str(existing_txn.amount),
            'duplicate': True
        }
    
    try:
        with transaction.atomic():
            # Rider credits only. A driver's settlement balance lives in a
            # separate Wallet row and must never be spendable on rides.
            wallet = Wallet.objects.select_for_update().get(
                user_id=user, scope=Wallet.SCOPE_RIDER,
            )
            
            from base.money import to_minor
            amount_minor = to_minor(amount_val, wallet.currency)
            from servers.fraud.money import held_minor

            # A reward pending review (AUDIT 27.56) is in the wallet but not spendable.
            if wallet.balance_minor - held_minor(user, wallet.currency) < amount_minor:
                return {'success': False, 'error': 'Insufficient wallet balance'}
            
            wallet.balance_minor = wallet.balance_minor - amount_minor
            wallet.save()
            
            txn = WalletTransaction.objects.create(
                user_id=user,
                currency=wallet.currency,
                amount_minor=amount_minor,
                txn_type='debit',
                status='completed',
                purpose=purpose,
                reference_id=reference_id,
                idempotency_key=idempotency_key
            )
            
            logger.info(f"Direct wallet payment successful for user {user.id}: "
                        f"Deducted {amount_val}, new balance: {wallet.balance}")
            
            return {
                'success': True,
                'transaction_id': txn.id,
                'amount': str(float(amount_val)),  # legacy response text, e.g. '100.0'
                'new_balance': str(wallet.balance),
                'purpose': purpose,
                'reference_id': reference_id,
                'idempotency_key': idempotency_key,
                'message': 'Payment successful'
            }
            
    except Wallet.DoesNotExist:
        return {'success': False, 'error': 'Wallet not found'}
    except Exception as e:
        logger.error(f"Direct wallet payment failed: {str(e)}")
        return {'success': False, 'error': str(e)}

