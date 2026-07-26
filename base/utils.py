import boto3
import logging
import random
import secrets
from typing import Dict, Any, Optional
from django.utils import timezone
from rest_framework.response import Response
from django.conf import settings
from django.contrib.auth import get_user_model
from celery import shared_task
from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger(__name__)

Users = get_user_model()

# Character set for username generation
CHARACTERS = (
    [chr(i) for i in range(ord('a'), ord('z') + 1)] +
    [chr(i) for i in range(ord('A'), ord('Z') + 1)] +
    list(map(str, range(0, 10)))
)



def get_sns_client():
    """
    Get AWS SNS client with error handling.
    
    Returns:
        boto3 SNS client or None if initialization fails
    """
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


@shared_task
def send_otp_via_sns(phone_number: str, message: str) -> Dict[str, Any]:
    """
    Send OTP via AWS SNS service.
    
    Args:
        phone_number: Recipient phone number (E.164 format)
        message: OTP message content
    
    Returns:
        dict: Status and response or error details
    """
    try:
        # Validate inputs
        if not phone_number or not message:
            logger.warning("Missing phone_number or message")
            return {
                "success": False,
                "error": "Phone number and message are required"
            }
        
        client = get_sns_client()
        if client is None:
            logger.error("SNS client initialization failed")
            return {
                "success": False,
                "error": "AWS SNS service unavailable"
            }
        
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
        
        logger.info(f"OTP sent successfully to {phone_number}")
        return {
            "success": True,
            "message_id": resp.get('MessageId')
        }
    
    except ClientError as e:
        logger.error(f"AWS ClientError sending OTP: {str(e)}")
        return {
            "success": False,
            "error": f"AWS error: {e.response.get('Error', {}).get('Message', str(e))}"
        }
    except BotoCoreError as e:
        logger.error(f"BotoCoreError sending OTP: {str(e)}")
        return {
            "success": False,
            "error": f"AWS service error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error sending OTP via SNS: {str(e)}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


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
        amount_val = float(amount)
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
            
            if float(wallet.balance) < amount_val:
                return {'success': False, 'error': 'Insufficient wallet balance'}
            
            wallet.balance = float(wallet.balance) - amount_val
            wallet.save()
            
            txn = WalletTransaction.objects.create(
                user_id=user,
                amount=amount_val,
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
                'amount': str(amount_val),
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

