import redis
import logging
from django.conf import settings
from base.utils import success_response, error_response
from rest_framework import status

logger = logging.getLogger(__name__)

# Initialize Redis client with connection pool and error handling
redis_client = None
try:
    if getattr(settings, 'TESTING', False):
        redis_client = None
    else:
        redis_client = redis.Redis.from_url(
            settings.REDIS_URL + '/3',
            decode_responses=True,
            socket_connect_timeout=2,
            socket_keepalive=True,
            socket_keepalive_options={} if hasattr(redis, 'SOCKET_KEEPALIVE_OPTIONS') else None
        )
        # Test the connection
        redis_client.ping()
        logger.info("Stream connection established successfully")
except (redis.ConnectionError, redis.TimeoutError, Exception) as e:
    logger.error(f"Failed to connect to Redis: {str(e)}")
    redis_client = None

def update_driver_location(driver_id, lng, lat):
    if redis_client is None:
        return error_response(
            code="REDIS_CONNECTION_ERROR",
            message="Failed to connect to Redis",
            field="redis",
            issue="Redis connection error",
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    redis_client.xadd('driver_location_stream', {'driver_id': driver_id, 'lng': lng, 'lat': lat}, maxlen=100000)

    return success_response(
        data={
            'message': "Driver location updated successfully",
            'driver_id': driver_id,
            'lng': lng,
            'lat': lat
        },
        status_code=status.HTTP_200_OK
    )

def credit_driver_wallet(trip):
    """Credit driver's wallet with earnings from completed trip."""
    from servers.payments.models import TransactionHistory
    from servers.rider.models import Wallet
    from django.conf import settings
    from decimal import Decimal
    from django.db import transaction

    if not trip.driver_id:
        return

    amount = trip.final_fare or trip.estimated_fare or Decimal('0.00')
    commission_rate = Decimal(str(getattr(settings, 'PLATFORM_COMMISSION_PERCENT', 20)))
    
    commission = (amount * commission_rate) / Decimal('100')
    net_amount = amount - commission

    with transaction.atomic():
        # Credit the driver's wallet
        wallet, created = Wallet.objects.get_or_create(user_id=trip.driver_id.user_id)
        if wallet.balance is None:
            wallet.balance = Decimal('0.00')
        wallet.balance = Decimal(str(wallet.balance)) + net_amount
        wallet.save()

        # Create transaction history for the credit
        TransactionHistory.objects.create(
            trip_id=trip,
            user_id=trip.user_id,
            driver_id=trip.driver_id,
            amount=net_amount,
            method=trip.payment_method or 'online',
            status='completed',
            txn_type='credit',
            user_name=trip.user_id.full_name or trip.user_id.phone_number,
        )