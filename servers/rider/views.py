import copy
import logging
from decimal import Decimal, InvalidOperation
from rest_framework import status
from base.utils import success_response, error_response
from rest_framework.decorators import api_view, permission_classes
from .serializers import FavoritePlaceSerializer
from rest_framework.permissions import IsAuthenticated
from .models import FavoritePlace
from ..redis_client import nearby_drivers

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def save_favorite_locations(request):
    """
    Save a favorite location for the user.
    
    Expected request data:
    {
        "address_text": str,
        "latitude": float,
        "longitude": float
    }
    """
    try:
        request.data['latitude']=request.data['latitude'][:12]
        request.data['longitude']=request.data['longitude'][:12]
        instance = FavoritePlaceSerializer(data=request.data)
        if instance.is_valid():
            instance.save(user_id=request.user)
            return success_response(
                {"location": instance.data},
                status.HTTP_201_CREATED
            )
        else:
            logger.warning(f"Validation errors: {instance.errors}")
            return error_response(
                code='VALIDATION_ERROR',
                message='Failed to save favorite location',
                field='location',
                issue=str(instance.errors),
                status=status.HTTP_400_BAD_REQUEST
            )
    except AttributeError as e:
        logger.error(f"Profile error: {str(e)}")
        return error_response(
            code='PROFILE_ERROR',
            message='User profile not found',
            field='user',
            issue='User profile issue',
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        logger.error(f"Unexpected error saving favorite location: {str(e)}")
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_favorite_locations(request):
    """
    Retrieve all favorite locations for the authenticated user.
    """
    try:
        query_set = FavoritePlace.objects.filter(user_id=request.user.id)
        data = FavoritePlaceSerializer(query_set, many=True)
        return success_response(data.data, status.HTTP_200_OK)
    except AttributeError as e:
        logger.error(f"Profile error: {str(e)}")
        return error_response(
            code='PROFILE_ERROR',
            message='User profile not found',
            field='user',
            issue='User profile issue',
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching favorite locations: {str(e)}")
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_favorite_location(request, location_id):
    """
    Delete a specific favorite location for the authenticated user.
    
    URL parameters:
    - location_id: ID of the favorite location to delete (int)
    """
    try:
        # Get the favorite location instance
        location = FavoritePlace.objects.get(id=location_id, user_id=request.user)
        
        # Delete the location
        location.delete()
        
        return success_response(
            {"message": "Favorite location deleted successfully"},
            status.HTTP_200_OK
        )
    except FavoritePlace.DoesNotExist:
        logger.warning(f"Favorite location not found for user {request.user.id}: {location_id}")
        return error_response(
            code='NOT_FOUND',
            message='Favorite location not found',
            field='location',
            issue=f'No favorite location found with ID {location_id}',
            status=status.HTTP_404_NOT_FOUND
        )
    except AttributeError as e:
        logger.error(f"Profile error: {str(e)}")
        return error_response(
            code='PROFILE_ERROR',
            message='User profile not found',
            field='user',
            issue='User profile issue',
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        logger.error(f"Unexpected error deleting favorite location: {str(e)}")
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_nearby_drivers(request):
    """
    Get nearby drivers for the rider.
    
    Query parameters:
    - lng: Longitude (float)
    - lat: Latitude (float)
    - radius: Search radius in meters (int, optional, default: 1000)
    - count: Maximum number of results (int, optional, default: 10)
    """
    try:
        # Get parameters from query_params for GET request
        lng = request.query_params.get('lng')
        lat = request.query_params.get('lat')
        radius = request.query_params.get('radius', 1000)
        count = request.query_params.get('count', 10)
        
        # Validate required fields
        if lng is None or lat is None:
            logger.warning("Missing coordinates in nearby drivers request")
            return error_response(
                code='MISSING_FIELDS',
                message='Longitude and latitude are required',
                field='coordinates',
                issue='lng and lat query parameters must be provided',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Try to convert to proper types
        try:
            lng = float(lng)
            lat = float(lat)
            radius = int(radius)
            count = int(count)
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid parameter types: {str(e)}")
            return error_response(
                code='INVALID_TYPE',
                message='Invalid parameter types',
                field='coordinates',
                issue='lng and lat must be floats, radius and count must be integers',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Call Redis function
        drivers = nearby_drivers(lng=lng, lat=lat, radius=radius, count=count)
        
        if drivers is None:
            logger.error("Redis operation failed for nearby drivers")
            return error_response(
                code='REDIS_ERROR',
                message='Failed to retrieve nearby drivers',
                field='general',
                issue='Database query failed',
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        return success_response(drivers, status.HTTP_200_OK)
    
    except Exception as e:
        logger.error(f"Unexpected error getting nearby drivers: {str(e)}")
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# ── Notifications ────────────────────────────────────────
# FCM

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_notification_preferences(request):
    """Return the caller's NotificationPreference (creating defaults
    on first access). Used by the mobile app to render the Settings
    -> Notifications screen."""
    from .models import NotificationPreference
    prefs, _ = NotificationPreference.objects.get_or_create(user_id=request.user)
    return success_response(
        {
            'transactional': prefs.transactional,
            'ride_event': prefs.ride_event,
            'payment': prefs.payment,
            'payout': prefs.payout,
            'sos': prefs.sos,
            'kyc': prefs.kyc,
            'system': prefs.system,
            'marketing': prefs.marketing,
            'promo': prefs.promo,
            'push_enabled': prefs.push_enabled,
            'email_enabled': prefs.email_enabled,
            'sms_enabled': prefs.sms_enabled,
            'user_toggleable': list(NotificationPreference.USER_TOGGLEABLE),
        },
        status.HTTP_200_OK,
    )


@api_view(['PATCH', 'POST'])
@permission_classes([IsAuthenticated])
def update_notification_preferences(request):
    """Patch the caller's NotificationPreference.

    Only categories listed in NotificationPreference.USER_TOGGLEABLE
    may be changed by the user themselves. Attempts to flip a
    locked-on category (transactional, sos, payout) silently ignore
    the value -- we never let the user opt out of safety-critical
    deliveries.
    """
    from .models import NotificationPreference
    prefs, _ = NotificationPreference.objects.get_or_create(user_id=request.user)

    updated = []
    for field in NotificationPreference.USER_TOGGLEABLE:
        if field in request.data:
            setattr(prefs, field, bool(request.data.get(field)))
            updated.append(field)
    for ch in ('push_enabled', 'email_enabled', 'sms_enabled'):
        if ch in request.data:
            setattr(prefs, ch, bool(request.data.get(ch)))
            updated.append(ch)
    if updated:
        prefs.save(update_fields=updated + ['updated_at'])
    return success_response(
        {'updated': updated, 'preferences': {f: getattr(prefs, f) for f in
            NotificationPreference.USER_TOGGLEABLE + ('push_enabled', 'email_enabled', 'sms_enabled')}},
        status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_notifications(request):
    """
    List notifications for the user.
    """
    from .models import Notification
    from .serializers import NotificationSerializer
    from rest_framework.pagination import PageNumberPagination

    notifs = Notification.objects.filter(user_id=request.user).order_by('-id')
    
    paginator = PageNumberPagination()
    paginator.page_size = 20
    result_page = paginator.paginate_queryset(notifs, request)
    serializer = NotificationSerializer(result_page, many=True)
    return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def mark_notification_read(request, notif_id):
    """Mark a specific notification as read."""
    from .models import Notification
    
    try:
        notif = Notification.objects.get(id=notif_id, user_id=request.user)
        notif.is_read = True
        notif.save()
        return success_response({'message': 'Marked as read'}, status.HTTP_200_OK)
    except Notification.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Notification not found',
            field='notif_id',
            issue=f'Notification {notif_id} not found',
            status=status.HTTP_404_NOT_FOUND
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_all_notifications_read(request):
    """Mark all notifications for the user as read."""
    from .models import Notification
    
    Notification.objects.filter(user_id=request.user, is_read=False).update(is_read=True)
    return success_response({'message': 'All notifications marked as read'}, status.HTTP_200_OK)


# ── Wallet ────────────────────────────────────────
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_wallet_balance(request):
    """Get current wallet balance."""
    from .models import Wallet
    
    try:
        wallet = Wallet.objects.get(user_id=request.user, scope=Wallet.SCOPE_RIDER)
        return success_response({'balance': str(wallet.balance)}, status.HTTP_200_OK)
    except Wallet.DoesNotExist:
        # Create wallet if not exists
        wallet = Wallet.objects.create(
            user_id=request.user, scope=Wallet.SCOPE_RIDER, balance=0,
        )
        return success_response({'balance': '0.00'}, status.HTTP_200_OK)



@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_wallet_transactions(request):
    """Get wallet transactions for the user."""
    from .models import WalletTransaction
    from .serializers import WalletTransactionSerializer
    from rest_framework.pagination import PageNumberPagination
    
    transactions = WalletTransaction.objects.filter(user_id=request.user).order_by('-id')
    
    paginator = PageNumberPagination()
    paginator.page_size = 20
    result_page = paginator.paginate_queryset(transactions, request)
    serializer = WalletTransactionSerializer(result_page, many=True)
    return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_payment(request):
    """
    Initiate a direct payment from wallet without Razorpay.
    
    Expected: {
        "amount": "100.00",
        "purpose": "Trip payment",
        "reference_id": "TRIP_123",
        "idempotency_key": "unique-key-123"
    }
    """
    from .models import WalletTransaction, Wallet
    from django.db import transaction
    import uuid
    
    amount = request.data.get('amount')
    purpose = request.data.get('purpose', 'Payment')
    reference_id = request.data.get('reference_id')
    idempotency_key = request.data.get('idempotency_key')
    
    # Validate required fields
    if not amount:
        return error_response(
            code='MISSING_FIELDS',
            message='amount is required',
            field='amount',
            issue='Provide the amount to pay from wallet',
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Generate idempotency key if not provided
    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())
    
    try:
        amount_val = float(amount)
        if amount_val <= 0:
            raise ValueError("Amount must be positive")
    except ValueError:
        return error_response(
            code='INVALID_AMOUNT',
            message='Invalid amount provided',
            field='amount',
            issue='Amount must be a positive number',
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Check for duplicate idempotency key
    existing_txn = WalletTransaction.objects.filter(
        idempotency_key=idempotency_key
    ).first()
    
    if existing_txn:
        return success_response({
            'message': 'Duplicate request detected - using existing transaction',
            'transaction_id': existing_txn.id,
            'status': existing_txn.status,
            'amount': str(existing_txn.amount)
        }, status.HTTP_200_OK)
    
    try:
        with transaction.atomic():
            # Lock wallet row for update
            wallet = Wallet.objects.select_for_update().get(
                user_id=request.user, scope=Wallet.SCOPE_RIDER,
            )
            
            # Check balance with lock
            if float(wallet.balance) < amount_val:
                return error_response(
                    code='INSUFFICIENT_BALANCE',
                    message='Insufficient wallet balance',
                    field='wallet',
                    issue='Wallet balance is less than the amount to pay',
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Deduct amount from wallet
            wallet.balance = float(wallet.balance) - amount_val
            wallet.save()
            
            # Create direct debit transaction
            txn = WalletTransaction.objects.create(
                user_id=request.user,
                amount=amount_val,
                txn_type='debit',
                status='completed',
                purpose=purpose,
                reference_id=reference_id,
                idempotency_key=idempotency_key
            )
            
            logger.info(f"Direct wallet payment successful for user {request.user.id}: "
                        f"Deducted {amount_val}, new balance: {wallet.balance}")
            
            return success_response({
                'transaction_id': txn.id,
                'amount': str(amount_val),
                'new_balance': str(wallet.balance),
                'purpose': purpose,
                'reference_id': reference_id,
                'idempotency_key': idempotency_key,
                'message': 'Payment successful'
            }, status.HTTP_201_CREATED)
            
    except Wallet.DoesNotExist:
        return error_response(
            code='WALLET_NOT_FOUND',
            message='Wallet not found',
            field='wallet',
            issue='User wallet does not exist',
            status=status.HTTP_404_NOT_FOUND
        )
    
    except Exception as e:
        logger.error(f"Direct wallet payment failed: {str(e)}")
        return error_response(
            code='PAYMENT_FAILED',
            message='Payment failed',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )