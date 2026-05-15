import json
import logging
from django.conf import settings
from django.utils import timezone
from decimal import Decimal, InvalidOperation
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import api_view, permission_classes
from rest_framework.views import APIView
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.db import models, transaction, IntegrityError
from base.utils import success_response, error_response
from servers.payments.models import Payment, TransactionHistory, PaymentGateway, WebhookEvent
from servers.payments.payment_gateways.factory import get_payment_gateway, get_payment_gateway_for_payments, get_payment_gateway_for_payouts
from servers.ride.models import Trip

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_order(request):
    """
    Create a payment order for a completed trip.
    
    Expected: { "trip_id": int }
    Returns: Payment gateway order details to be used by frontend checkout.
    """
    trip_id = request.data.get('trip_id')

    if not trip_id:
        return error_response(
            code='MISSING_FIELDS',
            message='trip_id is required',
            field='trip_id',
            issue='Provide the trip ID to create a payment order',
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        trip = Trip.objects.select_related('status_id', 'driver_id').get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Trip not found',
            field='trip_id',
            issue=f'No trip with id {trip_id}',
            status=status.HTTP_404_NOT_FOUND
        )

    # Verify trip belongs to this user
    if trip.user_id_id != request.user.id:
        return error_response(
            code='FORBIDDEN',
            message='You can only pay for your own trips',
            field='trip_id',
            issue='Trip does not belong to this user',
            status=status.HTTP_403_FORBIDDEN
        )

    # Verify trip is completed
    if not trip.status_id or trip.status_id.status_code != 'completed':
        return error_response(
            code='INVALID_STATE',
            message='Payment can only be made for completed trips',
            field='trip_id',
            issue=f'Trip status: {trip.status_id.status_code if trip.status_id else "pending"}',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Check if payment already exists
    existing = Payment.objects.filter(trip_id=trip, status='completed').first()
    if existing:
        return error_response(
            code='ALREADY_PAID',
            message='Payment already completed for this trip',
            field='trip_id',
            issue=f'Payment {existing.id} already completed',
            status=status.HTTP_409_CONFLICT
        )

    # Use final_fare if available, otherwise estimated_fare
    amount = trip.final_fare or trip.estimated_fare
    if not amount or amount <= 0:
        return error_response(
            code='INVALID_AMOUNT',
            message='No valid fare amount for this trip',
            field='amount',
            issue='Trip has no estimated or final fare',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get payment gateway
    gateway = get_payment_gateway()
    if not gateway:
        return error_response(
            code='PAYMENT_GATEWAY_ERROR',
            message='No payment gateway configured',
            field='payment_gateway',
            issue='Payment gateway not available',
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    # Create payment order using gateway. Pass the real rider phone and email
    # so receipts and Cashfree-dashboard rows carry the actual customer's
    # identity, not the placeholder phone/email that used to ship on every
    # order (security audit H5).
    order = gateway.create_order(
        amount=amount,
        trip_id=trip.id,
        customer_id=str(request.user.id),
        customer_phone=request.user.phone_number,
        customer_email=request.user.email,
    )
    logger.info(f"Payment order created via {gateway.get_name()}: order_id={order.get('order_id') if order else None}")
    if not order:
        return error_response(
            code='PAYMENT_GATEWAY_ERROR',
            message='Failed to create payment order. Please try again.',
            field='payment_gateway',
            issue=f'{gateway.get_name()} order creation failed',
            status=status.HTTP_502_BAD_GATEWAY
        )

    # Create or update Payment record
    payment = Payment.objects.filter(trip_id=trip, user_id=request.user).order_by('-created_at').first()
    
    if payment and payment.status in ['pending', 'processing', 'failed']:
        # Update existing record
        payment.amount = amount
        payment.method = 'online'
        payment.status = 'processing'
        payment.payment_gateway = gateway.get_name()
        payment.gateway_order_id = str(order.get('order_id') or order.get('gateway_order_id', ''))
        payment.gateway_metadata = order
        payment.cashfree_order_id = str(order.get('order_id') or order.get('gateway_order_id', ''))
        payment.cashfree_payment_session_id = order.get('payment_session_id')
        payment.save()
    else:
        # Create new record
        payment = Payment.objects.create(
            trip_id=trip,
            user_id=request.user,
            amount=amount,
            method='online',
            status='processing',
            payment_gateway=gateway.get_name(),
            gateway_order_id=str(order.get('order_id') or order.get('gateway_order_id', '')),
            gateway_metadata=order,
            cashfree_order_id=str(order.get('order_id') or order.get('gateway_order_id', '')),
            cashfree_payment_session_id=order.get('payment_session_id'),
        )
    logger.info(f"Payment created/updated: {payment}")

    # Prepare response based on gateway
    response_data = {
        'payment_id': payment.id,
        'gateway': gateway.get_name(),
        'gateway_order_id': order.get('id') or order.get('order_id'),
        'amount': str(amount),
        'amount_paise': order.get('amount') or str(int(amount * 100)),
        'currency': order.get('currency', 'INR'),
        'trip_id': trip.id,
        'description': f'Payment for Trip #{trip.id}',
        'prefill': {
            'name': request.user.full_name or '',
            'contact': request.user.phone_number or '',
            'email': request.user.email or '',
        }
    }

    # Add Cashfree-specific fields
    response_data['cashfree_payment_session_id'] = order.get('payment_session_id')
    response_data['cashfree_app_id'] = getattr(settings, 'CASHFREE_APP_ID', '')
    response_data['order_token'] = order.get('order_token')

    return success_response(response_data, status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def verify_payment(request):
    """
    Client-side payment verification after payment checkout completes.
    
    Expected: {
        "gateway_order_id": str,
        "gateway_payment_id": str,
        "gateway_signature": str,
        "gateway": "cashfree" (optional, auto-detected from payment)
    }
    """
    gateway_order_id = request.data.get('gateway_order_id')
    # gateway_payment_id = request.data.get('gateway_payment_id')
    # gateway_signature = request.data.get('gateway_signature')
    gateway_name = request.data.get('gateway','cashfree')

    if not all([gateway_order_id]):
        return error_response(
            code='MISSING_FIELDS',
            message='gateway_order_id, gateway_payment_id, and gateway_signature are required',
            field='request_body',
            issue='All payment verification fields must be provided',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Find the payment - try multiple fields for backward compatibility
    try:
        # First try gateway_order_id (new field)
        payment = Payment.objects.select_related('trip_id', 'trip_id__driver_id').get(
            gateway_order_id=gateway_order_id,
            user_id=request.user
        )
    except Payment.DoesNotExist:
        try:
            # Try cashfree_order_id
            payment = Payment.objects.select_related('trip_id', 'trip_id__driver_id').get(
                cashfree_order_id=gateway_order_id,
                user_id=request.user
            )
        except Payment.DoesNotExist:
            return error_response(
                code='NOT_FOUND',
                message='Payment not found for this order',
                field='gateway_order_id',
                issue=f'No payment found for order {gateway_order_id}',
                status=status.HTTP_404_NOT_FOUND
            )

    if payment.status == 'completed':
        return success_response({
            'message': 'Payment already verified',
            'payment_id': payment.id,
            'status': 'completed',
        }, status.HTTP_200_OK)

    # Determine gateway
    if not gateway_name:
        gateway_name = payment.payment_gateway or PaymentGateway.CASHFREE
    
    # Get payment gateway
    gateway = get_payment_gateway(gateway_name)
    if not gateway:
        return error_response(
            code='PAYMENT_GATEWAY_ERROR',
            message=f'Payment gateway {gateway_name} not available',
            field='gateway',
            issue='Payment gateway not configured',
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    # Verify signature using gateway
    is_valid = gateway.verify_payment_signature(
        order_id=gateway_order_id
    )
    if not is_valid:
        payment.status = 'failed'
        payment.save()
        return error_response(
            code='SIGNATURE_INVALID',
            message='Payment verification failed',
            field='gateway_signature',
            issue=f'{gateway.get_name()} signature verification failed',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Mark payment as completed
    with transaction.atomic():
        payment.status = 'completed'
        
        
        payment.save()

        # Update replaced payment if exists (for switched payments)
        if hasattr(payment, 'replaces') and payment.replaces:
            original = payment.replaces
            original.status = 'failed'
            original.switch_reason = 'user_requested'
            original.save()

        # Update trip payment status
        trip = payment.trip_id
        trip.payment_status = 'completed'
        trip.payment_method = 'online'
        trip.save(update_fields=['payment_status', 'payment_method'])

        # Create transaction history
        if trip.driver_id:
            TransactionHistory.objects.create(
                trip_id=trip,
                user_id=request.user,
                driver_id=trip.driver_id,
                amount=payment.amount,
                method='online',
                payment_gateway=gateway.get_name(),
                user_name=request.user.full_name or request.user.phone_number,
                status='completed',
            )

            # Create driver earning for online payment
            from servers.driver.utils import credit_driver_wallet
            credit_driver_wallet(trip)

    return success_response({
        'message': 'Payment verified successfully',
        'payment_id': payment.id,
        'status': 'completed',
        'amount': str(payment.amount),
        'gateway': gateway.get_name(),
    }, status.HTTP_200_OK)


@csrf_exempt
def payment_webhook(request):
    """
    Payment gateway webhook endpoint.
    Handles webhooks from Cashfree.
    No JWT auth — verified via gateway signature header.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    body = request.body
    
    # Cashfree V3 uses x-webhook-signature and x-webhook-timestamp
    signature = request.headers.get('x-webhook-signature') or request.headers.get('X-Cashfree-Signature')
    timestamp = request.headers.get('x-webhook-timestamp')
    
    # Determine gateway from headers
    gateway_name = None
    if signature:
        gateway_name = PaymentGateway.CASHFREE
    
    if not gateway_name:
        return JsonResponse({'error': 'Missing signature header'}, status=400)

    # Get payment gateway
    gateway = get_payment_gateway(gateway_name)
    if not gateway:
        return JsonResponse({'error': f'Gateway {gateway_name} not configured'}, status=503)

    # Verify webhook signature
    if not gateway.verify_webhook_signature(body, signature, timestamp):
        logger.warning(f"Webhook signature verification failed for {gateway_name}")
        return JsonResponse({'error': 'Invalid signature'}, status=400)

    try:
        payload = json.loads(body)

        # Idempotency gate. The signature is a function of (body, timestamp,
        # secret) and is unique per delivery, so it doubles as a perfect
        # dedupe key for both legitimate retries and forged replays.
        event_type = (payload.get('type') or payload.get('event') or '')[:64]
        try:
            WebhookEvent.objects.create(
                gateway=gateway_name,
                dedupe_key=signature[:512],
                event_type=event_type,
                raw_payload=payload,
            )
        except IntegrityError:
            logger.info(
                f"Webhook deduplicated (gateway={gateway_name}, "
                f"sig={signature[:16]}…)"
            )
            return JsonResponse({'status': 'already_processed'}, status=200)

        # Handle Cashfree webhook payload
        response = _handle_cashfree_webhook(payload, gateway)

        # Mark this event as processed for observability.
        try:
            WebhookEvent.objects.filter(
                gateway=gateway_name, dedupe_key=signature[:512]
            ).update(
                processed_at=timezone.now(),
                result='ok' if response.status_code == 200 else 'error',
            )
        except Exception:
            pass

        return response

    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Webhook: Invalid payload: {e}")
        return JsonResponse({'error': 'Invalid payload'}, status=400)
    except Exception as e:
        logger.error(f"Webhook: Error processing: {e}")
        return JsonResponse({'error': 'Internal error'}, status=500)


def _handle_cashfree_webhook(payload, gateway):
    """Handle Cashfree webhook payload."""
    event_type = payload.get('type', '')
    data = payload.get('data', {})
    order_data = data.get('order', {})
    payment_data = data.get('payment', {})
    
    # Extract order and payment IDs (supporting both V2 and V3 structures)
    order_id = order_data.get('order_id') or payload.get('orderId')
    payment_id = payment_data.get('cf_payment_id') or payload.get('referenceId')
    
    # Normalize event name
    is_success = event_type == 'PAYMENT_SUCCESS_WEBHOOK' or payload.get('event') == 'PAYMENT_SUCCESS'
    
    if is_success:
        if not order_id:
            return JsonResponse({'status': 'skipped', 'reason': 'no order_id'}, status=200)

        # Check if it's a Wallet Transaction
        from servers.rider.models import WalletTransaction, Wallet
        wallet_txn_pk = WalletTransaction.objects.filter(
            models.Q(cashfree_order_id=order_id) | models.Q(gateway_order_id=order_id)
        ).values_list('pk', flat=True).first()

        if wallet_txn_pk is not None:
            # Server-verify the amount with the gateway. We never credit on
            # webhook-supplied numbers alone — a forged-but-signed body (e.g.
            # from a stolen secret) would otherwise let an attacker mint
            # balance. The gateway's get_order_status is authoritative.
            order_info = gateway.get_order_status(order_id) if gateway else None
            if not order_info or order_info.get('order_status') != 'PAID':
                logger.warning(
                    f"Cashfree Webhook: order {order_id} not PAID at gateway "
                    f"(status={order_info.get('order_status') if order_info else 'unknown'}); "
                    f"refusing to credit wallet"
                )
                return JsonResponse(
                    {'status': 'skipped', 'reason': 'order not PAID at gateway'},
                    status=200,
                )
            try:
                gateway_amount = Decimal(str(order_info.get('order_amount'))).quantize(Decimal('0.01'))
            except (InvalidOperation, TypeError):
                logger.error(
                    f"Cashfree Webhook: malformed amount from gateway "
                    f"for wallet order {order_id}: {order_info}"
                )
                return JsonResponse(
                    {'status': 'skipped', 'reason': 'malformed gateway amount'},
                    status=200,
                )

            # Atomic + row-locked: prevents a concurrent verify_wallet_payment
            # from double-crediting the same transaction.
            with transaction.atomic():
                wallet_txn = (
                    WalletTransaction.objects.select_for_update().get(pk=wallet_txn_pk)
                )

                if wallet_txn.status == 'completed':
                    return JsonResponse({'status': 'already_completed'}, status=200)

                if wallet_txn.amount != gateway_amount:
                    logger.error(
                        f"Cashfree Webhook: wallet amount mismatch order={order_id} "
                        f"txn={wallet_txn.amount} gateway={gateway_amount}; refusing"
                    )
                    return JsonResponse(
                        {'status': 'skipped', 'reason': 'amount mismatch'},
                        status=200,
                    )

                wallet_txn.status = 'completed'
                wallet_txn.cashfree_payment_id = str(payment_id)
                wallet_txn.gateway_payment_id = str(payment_id)
                wallet_txn.save(update_fields=['status', 'cashfree_payment_id', 'gateway_payment_id'])

                wallet, _ = Wallet.objects.select_for_update().get_or_create(user_id=wallet_txn.user_id)
                wallet.balance = wallet.balance + gateway_amount  # Decimal + Decimal
                wallet.save(update_fields=['balance'])

            logger.info(
                f"Cashfree Webhook: Wallet Top-up {wallet_txn.id} "
                f"completed for user {wallet_txn.user_id.id} amount={gateway_amount}"
            )
            return JsonResponse({'status': 'ok'}, status=200)

        # Check if it's a Trip Payment
        try:
            payment = Payment.objects.select_related('trip_id', 'trip_id__driver_id').get(
                models.Q(cashfree_order_id=order_id) | models.Q(gateway_order_id=order_id)
            )
        except Payment.DoesNotExist:
            logger.warning(f"Cashfree Webhook: No payment/wallet matching order {order_id}")
            return JsonResponse({'status': 'skipped', 'reason': 'payment not found'}, status=200)

        if payment.status == 'completed':
            return JsonResponse({'status': 'already_completed'}, status=200)

        with transaction.atomic():
            payment.status = 'completed'
            payment.cashfree_payment_id = str(payment_id)
            payment.gateway_payment_id = str(payment_id)
            payment.save()

            trip = payment.trip_id
            trip.payment_status = 'completed'
            trip.payment_method = 'online'
            trip.save(update_fields=['payment_status', 'payment_method'])

            if trip.driver_id:
                TransactionHistory.objects.get_or_create(
                    trip_id=trip,
                    gateway_payment_id=str(payment_id),
                    defaults={
                        'user_id': payment.user_id,
                        'driver_id': trip.driver_id,
                        'amount': payment.amount,
                        'method': 'online',
                        'payment_gateway': PaymentGateway.CASHFREE,
                        'cashfree_payment_id': str(payment_id),
                        'user_name': payment.user_id.full_name or payment.user_id.phone_number,
                        'status': 'completed',
                    }
                )

                # Credit driver's wallet for online payment
                from servers.driver.utils import credit_driver_wallet
                credit_driver_wallet(trip)

        logger.info(f"Cashfree Webhook: Payment {payment.id} completed for trip {trip.id}")
        return JsonResponse({'status': 'ok'}, status=200)

    # Handle other Cashfree events
    elif event_type in ['PAYMENT_FAILED_WEBHOOK'] or payload.get('event') == 'PAYMENT_FAILED':
        logger.info(f"Cashfree Webhook: Payment {order_id} failed")
        return JsonResponse({'status': 'ok'}, status=200)

    # Log other events but don't process
    logger.info(f"Cashfree Webhook: Received event {event_type or payload.get('event')}, ignoring")
    return JsonResponse({'status': 'ok'}, status=200)


@csrf_exempt
def payout_webhook(request):
    """
    Payout gateway webhook endpoint.
    Handles webhooks from Cashfree for payout events.
    No JWT auth — verified via gateway signature header.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    body = request.body
    
    # Cashfree V3 uses x-webhook-signature and x-webhook-timestamp
    signature = request.headers.get('x-webhook-signature') or request.headers.get('X-Cashfree-Signature')
    timestamp = request.headers.get('x-webhook-timestamp')
    
    # Determine gateway from headers
    gateway_name = None
    if signature:
        gateway_name = PaymentGateway.CASHFREE
    
    if not gateway_name:
        return JsonResponse({'error': 'Missing signature header'}, status=400)

    # Get payment gateway for payouts
    gateway = get_payment_gateway_for_payouts()
    if not gateway:
        return JsonResponse({'error': f'Gateway {gateway_name} not configured'}, status=503)

    # Verify webhook signature
    if not gateway.verify_webhook_signature(body, signature, timestamp):
        logger.warning(f"Payout webhook signature verification failed for {gateway_name}")
        return JsonResponse({'error': 'Invalid signature'}, status=400)

    try:
        payload = json.loads(body)
        
        # Handle Cashfree payout webhook payload
        return _handle_cashfree_payout_webhook(payload, gateway)

    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Payout webhook: Invalid payload: {e}")
        return JsonResponse({'error': 'Invalid payload'}, status=400)
    except Exception as e:
        logger.error(f"Payout webhook: Error processing: {e}")
        return JsonResponse({'error': 'Internal error'}, status=500)


def _handle_cashfree_payout_webhook(payload, gateway):
    """Handle Cashfree payout webhook payload."""
    event = payload.get('event', '')
    # Extract payout IDs (supporting both V2 and V3 structures)
    payout_id = payload.get('transfer_id') or payload.get('payoutId')
    reference_id = payload.get('cf_transfer_id') or payload.get('referenceId')
    status = payload.get('status')
    failure_reason = payload.get('failureReason', '')
    
    logger.info(f"Cashfree Payout Webhook: Received event {event} for payout {payout_id}, status: {status}")
    
    # Import models here to avoid circular imports
    from servers.driver.models import WithdrawalRequest
    from servers.driver.services import trigger_payout_creation
    
    if not payout_id:
        logger.warning("Cashfree Payout Webhook: No payout_id in payload")
        return JsonResponse({'status': 'skipped', 'reason': 'no payout_id'}, status=200)
    
    # Find withdrawal request by payout_reference_id (stores Cashfree payout ID)
    withdrawal = WithdrawalRequest.objects.filter(payout_reference_id=payout_id).first()
    if not withdrawal:
        logger.warning(f"Cashfree Payout Webhook: No withdrawal found for payout_id {payout_id}")
        return JsonResponse({'status': 'skipped', 'reason': 'withdrawal not found'}, status=200)
    
    # Check if already processed (idempotency)
    if withdrawal.payout_status == status and withdrawal.status == 'completed':
        logger.info(f"Cashfree Payout Webhook: Withdrawal {withdrawal.id} already processed with status {status}")
        return JsonResponse({'status': 'already_processed'}, status=200)
    
    # Handle payout events
    if event == 'TRANSFER_SUCCESS' or event == 'PAYOUT_SUCCESS':
        with transaction.atomic():
            withdrawal.status = 'completed'
            withdrawal.payout_status = status if status else 'success'
            withdrawal.save(update_fields=['status', 'payout_status'])
            
            # Create transaction history entry
            TransactionHistory.objects.get_or_create(
                withdrawal_request=withdrawal,
                gateway_transaction_id=str(reference_id),
                defaults={
                    'user_id': withdrawal.driver.user_id,
                    'driver_id': withdrawal.driver,
                    'amount': withdrawal.amount,
                    'method': withdrawal.payout_method,
                    'payment_gateway': PaymentGateway.CASHFREE,
                    'gateway_payment_id': str(payout_id),
                    'cashfree_payment_id': str(payout_id),
                    'user_name': withdrawal.driver.user_id.full_name or withdrawal.driver.user_id.phone_number,
                    'status': 'completed',
                    'txn_type': 'payout',
                }
            )
        
        logger.info(f"Cashfree Payout Webhook: Withdrawal {withdrawal.id} completed successfully")
        return JsonResponse({'status': 'ok'}, status=200)
    
    elif event == 'TRANSFER_FAILED' or event == 'PAYOUT_FAILED':
        with transaction.atomic():
            withdrawal.status = 'failed'
            withdrawal.payout_status = status if status else 'failed'
            withdrawal.failure_reason = failure_reason[:255] if failure_reason else 'Payout failed via webhook'
            withdrawal.failure_count = (withdrawal.failure_count or 0) + 1
            withdrawal.save(update_fields=['status', 'payout_status', 'failure_reason', 'failure_count'])
            
            # Create transaction history entry for failed payout
            TransactionHistory.objects.get_or_create(
                withdrawal_request=withdrawal,
                gateway_payment_id=str(payout_id),
                defaults={
                    'user_id': withdrawal.driver.user_id,
                    'driver_id': withdrawal.driver,
                    'amount': withdrawal.amount,
                    'method': withdrawal.payout_method,
                    'payment_gateway': PaymentGateway.CASHFREE,
                    'cashfree_payment_id': str(payout_id),
                    'user_name': withdrawal.driver.user_id.full_name or withdrawal.driver.user_id.phone_number,
                    'status': 'failed',
                    'txn_type': 'payout',
                }
            )
        
        logger.warning(f"Cashfree Payout Webhook: Withdrawal {withdrawal.id} failed: {failure_reason}")
        return JsonResponse({'status': 'ok'}, status=200)
    
    elif event == 'PAYOUT_PENDING':
        # Update status but don't change withdrawal status (remains 'processing')
        withdrawal.payout_status = status if status else 'pending'
        withdrawal.save(update_fields=['payout_status'])
        
        logger.info(f"Cashfree Payout Webhook: Withdrawal {withdrawal.id} is pending")
        return JsonResponse({'status': 'ok'}, status=200)
    
    elif event == 'TRANSFER_REVERSED' or event == 'PAYOUT_REVERSED':
        with transaction.atomic():
            withdrawal.status = 'reversed'
            withdrawal.payout_status = status if status else 'reversed'
            withdrawal.save(update_fields=['status', 'payout_status'])
            
            # Create transaction history entry for reversal
            TransactionHistory.objects.get_or_create(
                withdrawal_request=withdrawal,
                gateway_payment_id=str(payout_id),
                defaults={
                    'user_id': withdrawal.driver.user_id,
                    'driver_id': withdrawal.driver,
                    'amount': withdrawal.amount,
                    'method': withdrawal.payout_method,
                    'payment_gateway': PaymentGateway.CASHFREE,
                    'cashfree_payment_id': str(payout_id),
                    'user_name': withdrawal.driver.user_id.full_name or withdrawal.driver.user_id.phone_number,
                    'status': 'reversed',
                    'txn_type': 'payout',
                }
            )
        
        logger.info(f"Cashfree Payout Webhook: Withdrawal {withdrawal.id} reversed")
        return JsonResponse({'status': 'ok'}, status=200)
    
    # Log other events but don't process
    logger.info(f"Cashfree Payout Webhook: Received event {event}, ignoring")
    return JsonResponse({'status': 'ok'}, status=200)


class PaymentPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 50


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def payment_history(request):
    """
    Get payment history for the authenticated user.
    
    Query params:
        ?page=1         - Page number
        ?page_size=10   - Items per page (max 50)
        ?status=completed - Filter by status (optional)
    """
    payments = Payment.objects.filter(
        user_id=request.user
    ).select_related('trip_id').order_by('-created_at')

    status_filter = request.query_params.get('status')
    if status_filter:
        payments = payments.filter(status=status_filter)

    paginator = PaymentPagination()
    page = paginator.paginate_queryset(payments, request)

    data = []
    for p in page:
        payment_data = {
            'id': p.id,
            'trip_id': p.trip_id_id,
            'amount': str(p.amount),
            'method': p.method,
            'status': p.status,
            'payment_gateway': p.payment_gateway,
            'gateway_order_id': p.gateway_order_id,
            'gateway_payment_id': p.gateway_payment_id,
            'created_at': p.created_at.isoformat(),
        }
        
        # Add Cashfree-specific fields
        if p.payment_gateway == PaymentGateway.CASHFREE:
            payment_data['cashfree_order_id'] = p.cashfree_order_id
            payment_data['cashfree_payment_id'] = p.cashfree_payment_id
            payment_data['cashfree_payment_session_id'] = p.cashfree_payment_session_id
        
        data.append(payment_data)

    return paginator.get_paginated_response(data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def refund_payment(request):
    """
    Initiate refund for a cancelled trip.

    Expected: { "trip_id": int }
    Only works if the trip is cancelled and payment was completed online.
    """
    from servers.rider.models import Notification

    trip_id = request.data.get('trip_id')
    if not trip_id:
        return error_response(
            code='MISSING_FIELDS',
            message='trip_id is required',
            field='trip_id',
            issue='Provide the trip ID to refund',
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        trip = Trip.objects.select_related('status_id').get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Trip not found',
            field='trip_id',
            issue=f'Trip {trip_id} does not exist',
            status=status.HTTP_404_NOT_FOUND
        )

    # Only the rider can request a refund
    if trip.user_id_id != request.user.id:
        return error_response(
            code='FORBIDDEN',
            message='Only the rider can request a refund',
            field='trip_id',
            issue='Trip does not belong to this user',
            status=status.HTTP_403_FORBIDDEN
        )

    # Trip must be cancelled
    if not trip.status_id or trip.status_id.status_code != 'cancelled':
        return error_response(
            code='INVALID_STATE',
            message='Refund only available for cancelled trips',
            field='trip_id',
            issue=f'Trip status: {trip.status_id.status_code if trip.status_id else "unknown"}',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Find the completed online payment
    payment = Payment.objects.filter(
        trip_id=trip,
        method='online',
        status='completed',
    ).first()

    if not payment:
        return error_response(
            code='NO_PAYMENT',
            message='No completed online payment found for this trip',
            field='trip_id',
            issue='No refundable payment exists',
            status=status.HTTP_400_BAD_REQUEST
        )

    if payment.status == 'refunded':
        return error_response(
            code='ALREADY_REFUNDED',
            message='Payment has already been refunded',
            field='trip_id',
            issue='Duplicate refund not allowed',
            status=status.HTTP_409_CONFLICT
        )

    # Determine gateway and resolve the order id. Cashfree's refund API is
    # keyed on order_id, NOT the cf_payment_id. The earlier code passed
    # cf_payment_id and the refund would 404 at the gateway.
    gateway_name = payment.payment_gateway or PaymentGateway.CASHFREE
    refund_order_id = None
    if gateway_name == PaymentGateway.CASHFREE:
        refund_order_id = payment.cashfree_order_id or payment.gateway_order_id

    if not refund_order_id:
        return error_response(
            code='NO_PAYMENT_ID',
            message=f'No {gateway_name} order ID found',
            field='gateway_order_id',
            issue=f'Cannot process refund without {gateway_name} order ID',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Get payment gateway
    gateway = get_payment_gateway(gateway_name)
    if not gateway:
        return error_response(
            code='GATEWAY_UNAVAILABLE',
            message=f'Payment gateway {gateway_name} not available',
            field='payment_gateway',
            issue='Payment gateway not configured',
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    from servers.rider.models import Wallet
    from decimal import Decimal

    # The whole refund flow runs inside one atomic block so the gateway call,
    # the payment status flip, the driver wallet reversal, and the ledger row
    # all commit together. select_for_update on the Payment row prevents two
    # concurrent refund requests from both reaching the gateway.
    with transaction.atomic():
        try:
            locked_payment = Payment.objects.select_for_update().select_related(
                'trip_id', 'trip_id__driver_id', 'trip_id__driver_id__user_id'
            ).get(pk=payment.pk)
        except Payment.DoesNotExist:
            return error_response(
                code='NOT_FOUND',
                message='Payment not found',
                field='payment_id',
                issue='Payment row vanished between read and lock',
                status=status.HTTP_404_NOT_FOUND
            )

        if locked_payment.status == 'refunded':
            return error_response(
                code='ALREADY_REFUNDED',
                message='Payment has already been refunded',
                field='trip_id',
                issue='Duplicate refund not allowed',
                status=status.HTTP_409_CONFLICT
            )

        refund = gateway.create_refund(refund_order_id, amount=locked_payment.amount)
        if not refund:
            return error_response(
                code='REFUND_FAILED',
                message='Failed to process refund. Please try again.',
                field='payment_gateway',
                issue=f'{gateway.get_name()} refund creation failed',
                status=status.HTTP_502_BAD_GATEWAY
            )

        locked_payment.status = 'refunded'
        locked_payment.save(update_fields=['status', 'updated_at'])

        trip.payment_status = 'refunded'
        trip.save(update_fields=['payment_status'])

        # If we previously credited the driver for this trip, reverse that
        # credit now. Without this, the platform pays out the driver AND
        # refunds the rider, leaking the full net amount on every cancel.
        driver = locked_payment.trip_id.driver_id
        credit_txn = TransactionHistory.objects.filter(
            trip_id=trip,
            driver_id=driver,
            txn_type='credit',
            status='completed',
        ).order_by('-created_at').first() if driver else None

        if credit_txn:
            credit_amount = Decimal(str(credit_txn.amount))
            driver_wallet = (
                Wallet.objects.select_for_update()
                .filter(user_id=driver.user_id)
                .first()
            )
            if driver_wallet:
                driver_wallet.balance = (
                    Decimal(str(driver_wallet.balance)) - credit_amount
                )
                driver_wallet.save(update_fields=['balance'])

            TransactionHistory.objects.create(
                trip_id=trip,
                user_id=request.user,
                driver_id=driver,
                amount=credit_amount,
                method='online',
                payment_gateway=gateway_name,
                gateway_payment_id=locked_payment.cashfree_payment_id or locked_payment.gateway_payment_id,
                cashfree_payment_id=locked_payment.cashfree_payment_id,
                user_name=request.user.full_name or request.user.phone_number,
                status='completed',
                txn_type='debit',
            )

        # Rider-side refund ledger row. Driver FK is required on the model;
        # populate with the trip's driver if any, else only write the row
        # when we have a driver (which is the typical refundable case).
        if driver:
            TransactionHistory.objects.create(
                trip_id=trip,
                user_id=request.user,
                driver_id=driver,
                amount=locked_payment.amount,
                method='online',
                payment_gateway=gateway_name,
                gateway_payment_id=locked_payment.cashfree_payment_id or locked_payment.gateway_payment_id,
                cashfree_payment_id=locked_payment.cashfree_payment_id,
                user_name=request.user.full_name or request.user.phone_number,
                status='completed',
                txn_type='refund',
                gateway_metadata={'refund_id': refund.get('refund_id') or refund.get('id')},
            )

        Notification.objects.create(
            user_id=request.user,
            title='Refund Processed',
            message=(
                f"Your refund of ₹{locked_payment.amount} for Trip "
                f"#{trip.id} has been initiated. It may take 5-7 business "
                f"days to reflect in your account."
            ),
        )

    return success_response({
        'message': 'Refund initiated successfully',
        'refund_id': refund.get('refund_id') or refund.get('id'),
        'amount': str(locked_payment.amount),
        'trip_id': trip.id,
        'gateway': gateway.get_name(),
    }, status.HTTP_200_OK)


def _settle_payment_if_paid_at_gateway(trip_id_value, gateway):
    """Reconcile this trip's latest non-refunded payment with the gateway.

    If the gateway reports the order as PAID, finalize the local Payment,
    mark the Trip paid, write the rider-side TransactionHistory, and credit
    the driver — all inside one atomic block keyed on Payment.status under
    select_for_update. Idempotent against concurrent webhook delivery: the
    lock + status check is the only gate that lets credit_driver_wallet run.

    Returns True if local state ends up 'completed' (either because we just
    made it so, or because another path beat us to it).
    """
    if not gateway:
        return False

    payment = (
        Payment.objects.filter(trip_id=trip_id_value)
        .exclude(status='refunded')
        .order_by('-created_at')
        .first()
    )
    if not payment:
        return False
    if payment.status == 'completed':
        return True

    order_id = payment.cashfree_order_id or payment.gateway_order_id
    if not order_id:
        return False

    try:
        info = gateway.get_order_status(order_id)
    except Exception as e:
        logger.warning(f"Gateway status check failed for order {order_id}: {e}")
        return False
    if not info or info.get('order_status') != 'PAID':
        return False

    with transaction.atomic():
        p = Payment.objects.select_for_update().select_related(
            'trip_id', 'trip_id__driver_id', 'user_id'
        ).get(pk=payment.pk)
        if p.status == 'completed':
            return True

        p.status = 'completed'
        p.save(update_fields=['status', 'updated_at'])

        trip = p.trip_id
        trip.payment_status = 'completed'
        trip.payment_method = 'online'
        trip.save(update_fields=['payment_status', 'payment_method'])

        if trip.driver_id:
            TransactionHistory.objects.get_or_create(
                trip_id=trip,
                gateway_payment_id=(
                    p.cashfree_payment_id or p.gateway_payment_id or order_id
                ),
                defaults={
                    'user_id': p.user_id,
                    'driver_id': trip.driver_id,
                    'amount': p.amount,
                    'method': 'online',
                    'payment_gateway': p.payment_gateway,
                    'cashfree_payment_id': p.cashfree_payment_id,
                    'user_name': p.user_id.full_name or p.user_id.phone_number,
                    'status': 'completed',
                    'txn_type': 'payment',
                },
            )
            from servers.driver.utils import credit_driver_wallet
            credit_driver_wallet(trip)

    logger.info(
        f"Settled payment {payment.pk} for trip {trip_id_value} via gateway poll "
        f"(order {order_id})"
    )
    return True


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def switch_payment_method(request):
    """
    Switch payment method at trip completion.
    
    Expected: { "trip_id": int, "new_method": "cash" | "upi" | "wallet" | "online" }
    Returns: { "payment_id": int, "status": "pending", "gateway_order_id": str (if online/wallet) }
    
    Per D-01: Only allowed when trip is completed
    Per D-02: Replaces original payment (marks as failed), creates new payment
    Per D-07: Manual retry only - no auto-retry on failure
    """
    trip_id = request.data.get('trip_id')
    new_method = request.data.get('new_method')
    
    if not trip_id or not new_method:
        return error_response(
            code='MISSING_FIELDS',
            message='trip_id and new_method are required',
            field='new_method',
            issue='Provide both trip_id and new_method (cash, upi, wallet, or online)',
            status=status.HTTP_400_BAD_REQUEST
        )
    
    if new_method not in ('cash', 'upi', 'wallet', 'online'):
        return error_response(
            code='INVALID_METHOD',
            message='Invalid payment method',
            field='new_method',
            issue='new_method must be cash, upi, wallet, or online',
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Map UPI/wallet to online for backend
    if new_method in ('upi', 'wallet', 'online'):
        actual_method = 'online'
    elif new_method == 'cash':
        actual_method = 'cash'
    else:
        actual_method = new_method
    
    try:
        trip = Trip.objects.select_related('status_id').get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Trip not found',
            field='trip_id',
            issue=f'No trip with id {trip_id}',
            status=status.HTTP_404_NOT_FOUND
        )
    
    # D-01: Verify trip is completed
    if not trip.status_id or trip.status_id.status_code != 'completed':
        return error_response(
            code='INVALID_STATE',
            message='Payment method can only be switched at trip completion',
            field='trip_status',
            issue='Trip must be completed to switch payment method',
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Verify ownership
    if trip.user_id_id != request.user.id:
        return error_response(
            code='FORBIDDEN',
            message='You can only modify payment for your own trips',
            field='trip_id',
            issue='Trip does not belong to this user',
            status=status.HTTP_403_FORBIDDEN
        )
    
    # Find existing payment
    existing = Payment.objects.filter(trip_id=trip).order_by('-created_at').first()
    if not existing:
        return error_response(
            code='NO_PAYMENT',
            message='No payment found for this trip',
            field='payment',
            issue='Cannot switch payment method without existing payment',
            status=status.HTTP_404_NOT_FOUND
        )

    # Before we mark the existing payment failed and create a new order,
    # check with the gateway whether the existing order has actually been
    # paid. If the rider completed the original checkout right before
    # tapping "switch", the late webhook would otherwise land on a now-
    # deactivated payment row, leaving the trip paid AND a fresh order
    # outstanding (and the driver potentially double-credited).
    poll_gateway = get_payment_gateway_for_payments()
    if _settle_payment_if_paid_at_gateway(trip.id, poll_gateway):
        return error_response(
            code='ALREADY_PAID',
            message='Your previous payment has already succeeded at the gateway',
            field='trip_id',
            issue='Existing order was reported PAID by the gateway',
            status=status.HTTP_409_CONFLICT
        )

    # D-02: Replace original payment
    with transaction.atomic():
        # Re-fetch with a row lock so two concurrent switch requests can't
        # both deactivate the same payment row.
        existing = Payment.objects.select_for_update().get(pk=existing.pk)
        if existing.status == 'completed':
            return error_response(
                code='ALREADY_PAID',
                message='Payment has already been completed',
                field='trip_id',
                issue='Cannot switch a completed payment',
                status=status.HTTP_409_CONFLICT
            )

        existing.status = 'failed'
        existing.switch_reason = 'user_requested'
        existing.save()
        
        # Create new payment
        amount = trip.final_fare or trip.estimated_fare
        
        if actual_method == 'online':
            # Use payment gateway abstraction
            gateway = get_payment_gateway_for_payments()
            order = gateway.create_order(
                amount=float(amount),
                trip_id=trip.id,
                currency='INR',
                customer_id=str(request.user.id),
                customer_phone=request.user.phone_number,
                customer_email=request.user.email,
            )
            new_payment = Payment.objects.create(
                trip_id=trip,
                user_id=request.user,
                amount=amount,
                method='online',
                status='pending',
                payment_gateway=gateway.get_name(),
                gateway_order_id=order.get('order_id') if order else None,
                cashfree_order_id=order.get('order_id') if order else None,
            )
        else:
            new_payment = Payment.objects.create(
                trip_id=trip,
                user_id=request.user,
                amount=amount,
                method='wallet',
                status='processing',
            )
        
        # Link payments
        existing.replaced_by = new_payment
        existing.save()
        
        # Update trip
        trip.payment_method = actual_method
        trip.payment_status = 'pending'
        trip.save(update_fields=['payment_method', 'payment_status'])
    
    response_data = {'payment_id': new_payment.id, 'status': new_payment.status}
    if actual_method == 'online' and new_payment.gateway_order_id:
        response_data['gateway_order_id'] = new_payment.gateway_order_id
        response_data['payment_gateway'] = new_payment.payment_gateway
    
    return success_response(response_data, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def retry_payment(request):
    """
    Manually retry a failed payment.
    
    Expected: { "trip_id": int }
    Per D-07: Manual retry only - no auto-retry
    
    Returns: { "payment_id": int, "gateway_order_id": str, ... }
    """
    trip_id = request.data.get('trip_id')
    
    if not trip_id:
        return error_response(
            code='MISSING_FIELDS',
            message='trip_id is required',
            field='trip_id',
            issue='Provide the trip ID to retry payment',
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        trip = Trip.objects.get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Trip not found',
            field='trip_id',
            issue=f'No trip with id {trip_id}',
            status=status.HTTP_404_NOT_FOUND
        )
    
    if trip.user_id_id != request.user.id:
        return error_response(
            code='FORBIDDEN',
            message='You can only retry payment for your own trips',
            field='trip_id',
            issue='Trip does not belong to this user',
            status=status.HTTP_403_FORBIDDEN
        )
    
    # Get the latest failed payment
    payment = Payment.objects.filter(
        trip_id=trip,
        status='failed'
    ).order_by('-created_at').first()

    if not payment:
        return error_response(
            code='NO_FAILED_PAYMENT',
            message='No failed payment found for this trip',
            field='payment',
            issue='Cannot retry - no failed payment exists',
            status=status.HTTP_404_NOT_FOUND
        )

    # Use payment gateway abstraction
    gateway = get_payment_gateway_for_payments()

    # Before re-issuing an order, ask the gateway whether the failed payment
    # was actually paid. Cashfree may report PAID even when our local row is
    # 'failed' (e.g. the rider closed the app before the verify call). A
    # blind retry would create a second order, the late webhook on the old
    # order would credit the trip via the old row, and a second payment
    # would still be outstanding.
    if _settle_payment_if_paid_at_gateway(trip.id, gateway):
        payment.refresh_from_db()
        return success_response({
            'message': 'Payment already completed at gateway',
            'payment_id': payment.id,
            'status': payment.status,
        }, status.HTTP_200_OK)

    # D-07: Manual retry - create new payment order
    amount = trip.final_fare or trip.estimated_fare

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status == 'completed':
            return success_response({
                'message': 'Payment already completed',
                'payment_id': payment.id,
                'status': 'completed',
            }, status.HTTP_200_OK)

        order = gateway.create_order(
            amount=float(amount),
            trip_id=trip.id,
            currency='INR',
            customer_id=str(request.user.id),
            customer_phone=request.user.phone_number,
            customer_email=request.user.email,
        )

        payment.gateway_order_id = order.get('order_id') if order else None
        payment.cashfree_order_id = order.get('order_id') if order else None
        payment.payment_gateway = gateway.get_name()
        payment.status = 'processing'
        payment.save()

        trip.payment_status = 'processing'
        trip.save(update_fields=['payment_status'])

    return success_response({
        'payment_id': payment.id,
        'gateway_order_id': order.get('order_id') if order else None,
        'payment_gateway': gateway.get_name(),
        'amount': str(amount),
    }, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pending_payments(request):
    """
    Get pending payments (deferred payments that need method selection).
    
    Returns: List of pending payments with trip details for state recovery.
    
    Used by Flutter app to recover state after app restart when user
    hasn't selected payment method for completed trips.
    """
    # Get pending payments for the current user with method 'deferred' and status 'pending'
    pending = Payment.objects.filter(
        user_id=request.user,
        method='deferred',
        status='pending'
    ).select_related('trip_id', 'trip_id__status_id').order_by('-created_at')
    
    if not pending.exists():
        return success_response({
            'pending_payments': [],
            'message': 'No pending payments requiring method selection'
        }, status.HTTP_200_OK)
    
    payments_data = []
    for payment in pending:
        trip = payment.trip_id
        # Only include completed trips (trips that have been completed but payment not selected)
        if trip.status_id and trip.status_id.status_code == 'completed':
            payments_data.append({
                'payment_id': payment.id,
                'trip_id': trip.id,
                'amount': str(payment.amount),
                'created_at': payment.created_at.isoformat() if payment.created_at else None,
                'trip_details': {
                    'pickup_address': trip.pickup_address,
                    'destination_address': trip.destination_address,
                    'completed_at': trip.completed_at.isoformat() if trip.completed_at else None,
                    'driver_name': trip.driver_id.full_name if trip.driver_id else None,
                },
                'payment_options': ['cash', 'wallet', 'online']
            })
    
    return success_response({
        'pending_payments': payments_data,
        'count': len(payments_data),
        'message': f'Found {len(payments_data)} pending payment(s) requiring method selection'
    }, status.HTTP_200_OK)
