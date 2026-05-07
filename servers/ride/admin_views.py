from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from base.utils import success_response, error_response
from servers.ride.models import Trip
from servers.ride.serializers import TripListSerializer
from servers.driver.permissions import IsAdmin
from rest_framework.pagination import PageNumberPagination
from servers.redis_client import get_all_active_drivers, get_all_active_riders

@api_view(['GET'])
@permission_classes([IsAdmin])
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
        ).all().order_by('-created_at')

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
@permission_classes([IsAdmin])
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
