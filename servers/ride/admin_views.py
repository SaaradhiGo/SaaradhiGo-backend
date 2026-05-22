from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from base.utils import success_response, error_response
from base.permissions import IsAdmin
from servers.ride.models import Trip
from servers.ride.serializers import TripListSerializer
from rest_framework.pagination import PageNumberPagination
from servers.redis_client import get_all_active_drivers, get_all_active_riders

@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_list_trips(request):
    """
    Admin view to list all trips with pagination and filtering.
    Filters:
    - status: pending | accepted | arriving | in_progress | completed | cancelled
    - driver_id: (int)
    - user_id: (int) (rider)
    """
    try:
        trips = Trip.objects.select_related(
            'status_id', 'driver_id', 'vehicle_id', 'user_id', 'requested_vehicle_type'
        ).all().order_by('-requested_at')

        status_filter = request.query_params.get('status')
        if status_filter:
            trips = trips.filter(status_id__status_code=status_filter)

        driver_id = request.query_params.get('driver_id')
        if driver_id:
            trips = trips.filter(driver_id=driver_id)
            
        user_id = request.query_params.get('user_id')
        if user_id:
            trips = trips.filter(user_id=user_id)

        paginator = PageNumberPagination()
        paginator.page_size = 20
        paginator.page_size_query_param = 'page_size'
        paginator.max_page_size = 100
        
        result_page = paginator.paginate_queryset(trips, request)
        serializer = TripListSerializer(result_page, many=True)
        return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)

    except Exception as e:
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_live_locations(request):
    """
    Admin view to get all active riders and drivers from Redis.
    """
    try:
        drivers = get_all_active_drivers()
        riders = get_all_active_riders()
        
        return success_response({
            "drivers": drivers,
            "riders": riders
        }, status.HTTP_200_OK)
    except Exception as e:
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred fetching live locations',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_dashboard(request):
    """Daily ops dashboard KPIs.

    Returns a single payload the admin web console can render as
    headline tiles. Defaults to "today in Asia/Kolkata" but a `?date=`
    query param (YYYY-MM-DD) lets ops backfill yesterday's numbers.

    Tile set:
      trips.requested / accepted / completed / cancelled
      gmv (sum of completed trips' final_fare)
      drivers.online_now / online_24h
      riders.active_24h
      cancellations.driver_24h (counter for the rolling penalty rule)
      withdrawals.pending / completed_today (Rs)
      receipts.issued_today / send_failures_today
    """
    from datetime import datetime, time, timedelta
    from decimal import Decimal
    from django.utils import timezone
    from django.db.models import Count, Sum
    from servers.driver.models import Driver, DriverCancellation, WithdrawalRequest
    from servers.ride.models import Trip, Receipt

    tz = timezone.get_current_timezone()
    date_str = request.query_params.get('date')
    if date_str:
        try:
            day_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return error_response(
                code='INVALID_DATE', message='date must be YYYY-MM-DD',
                field='date', issue=f'Got {date_str!r}',
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        day_date = timezone.localtime(timezone.now()).date()

    day_start = timezone.make_aware(datetime.combine(day_date, time.min), tz)
    day_end = day_start + timedelta(days=1)
    last_24h = timezone.now() - timedelta(hours=24)

    trip_qs_today = Trip.objects.filter(requested_at__gte=day_start, requested_at__lt=day_end)
    by_status = dict(
        trip_qs_today.values('status_id__status_code')
        .annotate(c=Count('id'))
        .values_list('status_id__status_code', 'c')
    )
    completed_today = Trip.objects.filter(
        completed_at__gte=day_start, completed_at__lt=day_end,
    )
    gmv = completed_today.aggregate(
        s=Sum('final_fare'),
    )['s'] or Decimal('0.00')

    online_now = Driver.objects.filter(status='online').count()
    online_24h = Driver.objects.filter(sessions__started_at__gte=last_24h).distinct().count()
    riders_active_24h = (
        Trip.objects.filter(requested_at__gte=last_24h)
        .values('user_id').distinct().count()
    )
    driver_cancels_24h = DriverCancellation.objects.filter(created_at__gte=last_24h).count()

    withdrawals_pending = WithdrawalRequest.objects.filter(
        status__in=('pending', 'approved', 'processing'),
    ).count()
    withdrawals_completed_today_amount = (
        WithdrawalRequest.objects.filter(
            status='completed', processed_at__gte=day_start, processed_at__lt=day_end,
        ).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
    )

    receipts_today = Receipt.objects.filter(issued_at__gte=day_start, issued_at__lt=day_end)
    receipts_issued = receipts_today.count()
    receipts_failed = receipts_today.exclude(send_failure_reason='').count()

    return success_response(
        {
            'date': day_date.isoformat(),
            'trips': {
                'requested': by_status.get('requested', 0),
                'accepted': by_status.get('accepted', 0),
                'completed': by_status.get('completed', 0),
                'cancelled': by_status.get('cancelled', 0),
                'total': sum(by_status.values()),
            },
            'gmv': str(gmv),
            'drivers': {
                'online_now': online_now,
                'online_24h': online_24h,
            },
            'riders': {
                'active_24h': riders_active_24h,
            },
            'cancellations': {
                'driver_24h': driver_cancels_24h,
            },
            'withdrawals': {
                'pending': withdrawals_pending,
                'completed_today_amount': str(withdrawals_completed_today_amount),
            },
            'receipts': {
                'issued_today': receipts_issued,
                'send_failures_today': receipts_failed,
            },
        },
        status.HTTP_200_OK,
    )
