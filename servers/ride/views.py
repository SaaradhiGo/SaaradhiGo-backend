import logging
from decimal import Decimal
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from base.utils import success_response, error_response
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import api_view, permission_classes
from servers.ride.models import Trip, FarePricing
from servers.ride.serializers import TripListSerializer, TripDetailSerializer
from servers.ride.utils import estimate_amount, validate_distance
from servers.driver.permissions import IsDriver
from servers.redis_client import nearby_drivers, publish_ride_request
from django.db import transaction

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def estimate_fare(request):
    """
    Estimate fare for a trip before rider confirms.
    
    Expected request data:
    {
        "pickup_lat": float,
        "pickup_long": float,
        "destination_lat": float,
        "destination_long": float,
        "distance_km": float,
        "duration_min": float,
        "vehicle_type": str (e.g. "sedan", "suv")
    }
    """
    pickup_lat = request.data.get('pickup_lat')
    pickup_long = request.data.get('pickup_long')
    destination_lat = request.data.get('destination_lat')
    destination_long = request.data.get('destination_long')
    distance_km = request.data.get('distance_km')
    duration_min = request.data.get('duration_min')
    vehicle_type = request.data.get('vehicle_type')

    # Validate required fields
    required = {
        'pickup_lat': pickup_lat, 'pickup_long': pickup_long,
        'destination_lat': destination_lat, 'destination_long': destination_long,
        'distance_km': distance_km, 'duration_min': duration_min,
    }
    missing = [k for k, v in required.items() if v is None]
    if missing:
        return error_response(
            code='MISSING_FIELDS',
            message=f'Missing required fields: {", ".join(missing)}',
            field='request_body',
            issue='All coordinate, distance, and duration fields are required',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Validate numeric types
    try:
        pickup_lat = float(pickup_lat)
        pickup_long = float(pickup_long)
        destination_lat = float(destination_lat)
        destination_long = float(destination_long)
        distance_km = float(distance_km)
        duration_min = float(duration_min)
    except (ValueError, TypeError):
        return error_response(
            code='INVALID_TYPE',
            message='All numeric fields must be valid numbers',
            field='coordinates',
            issue='pickup_lat, pickup_long, destination_lat, destination_long, distance_km, duration_min must be floats',
            status=status.HTTP_400_BAD_REQUEST
        )

    if distance_km <= 0 or duration_min <= 0:
        return error_response(
            code='INVALID_VALUE',
            message='Distance and duration must be positive values',
            field='distance_km / duration_min',
            issue='Values must be greater than 0',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Service-area enforcement. Phase-0 only serves Hyderabad metro; both
    # pickup and drop must sit inside the polygon. Without this check, a
    # tampered client can have us produce fares for any city.
    from base.service_area import validate_service_area
    area_ok, area_msg = validate_service_area(
        pickup_lat, pickup_long, destination_lat, destination_long,
    )
    if not area_ok:
        return error_response(
            code='OUT_OF_SERVICE_AREA',
            message=area_msg,
            field='pickup / destination',
            issue='Coordinates fall outside the SaaradhiGo Hyderabad service area',
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Server-side distance + duration (authoritative for fare).
    is_valid, validated_km, validated_min, msg = validate_distance(
        distance_km, duration_min, pickup_lat, pickup_long, destination_lat, destination_long
    )
    if not is_valid:
        return error_response(
            code='DISTANCE_INVALID',
            message=msg,
            field='distance_km / duration_min',
            issue=f'Server could not validate distance: {msg}',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Fare is computed from the SERVER-computed validated_km/min, not the
    # client-supplied numbers. Trusting the client distance was the audit's
    # primary fare-tampering vector.
    fare = estimate_amount(
        validated_km,
        validated_min,
        vehicle_type=vehicle_type,
        pickup_lat=pickup_lat,
        pickup_long=pickup_long,
        rider_id=request.user.id,
    )

    return success_response({
        'estimated_fare': str(fare['total_fare']),
        'fare_breakdown': {
            'base_fare': str(fare['base_fare']),
            'distance_fare': str(fare['distance_fare']),
            'time_fare': str(fare['time_fare']),
            'surge_multiplier': str(fare['surge_multiplier']),
            'min_fare_applied': fare['min_fare_applied'],
        },
        'vehicle_type': fare['vehicle_type'],
        'pricing_source': fare['source'],
        'distance_km': distance_km,
        'duration_min': duration_min,
        'validated_km': validated_km,
        'validated_min': validated_min,
    }, status.HTTP_200_OK)


# @api_view(['POST'])
# @permission_classes([IsAuthenticated])
# def ride_request(request):
#     """
#     Create a new ride request.
    
#     Expected request data:
#     {
#         "pickup_lat": float,
#         "pickup_long": float,
#         "destination_lat": float,
#         "destination_long": float,
#         "pickup_address": str (optional),
#         "destination_address": str (optional),
#         "distance_km": float,
#         "duration_min": float,
#         "vehicle_type": str (optional, e.g. "sedan")
#     }
#     """
#     pickup_lat = request.data.get('pickup_lat')
#     pickup_long = request.data.get('pickup_long')
#     destination_lat = request.data.get('destination_lat')
#     destination_long = request.data.get('destination_long')
#     pickup_address = request.data.get('pickup_address', '')
#     destination_address = request.data.get('destination_address', '')
#     distance_km = request.data.get('distance_km')
#     duration_min = request.data.get('duration_min')
#     vehicle_type = request.data.get('vehicle_type')

#     # Validate pickup coordinates
#     if not pickup_lat or not pickup_long:
#         return error_response(
#             code='MISSING_FIELDS',
#             message='Pickup latitude and longitude are required',
#             field='pickup_coordinates',
#             issue='pickup_lat and pickup_long must be provided',
#             status=status.HTTP_400_BAD_REQUEST
#         )

#     # Validate destination coordinates
#     if not destination_lat or not destination_long:
#         return error_response(
#             code='MISSING_FIELDS',
#             message='Destination latitude and longitude are required',
#             field='destination_coordinates',
#             issue='destination_lat and destination_long must be provided',
#             status=status.HTTP_400_BAD_REQUEST
#         )

#     # Validate coordinate types
#     try:
#         pickup_lat = float(pickup_lat)
#         pickup_long = float(pickup_long)
#         destination_lat = float(destination_lat)
#         destination_long = float(destination_long)
#     except (ValueError, TypeError):
#         return error_response(
#             code='INVALID_TYPE',
#             message='Coordinates must be valid numbers',
#             field='coordinates',
#             issue='All coordinate fields must be floats',
#             status=status.HTTP_400_BAD_REQUEST
#         )

#     # Parse distance and duration (optional but recommended)
#     try:
#         distance_km = float(distance_km) if distance_km is not None else None
#         duration_min = float(duration_min) if duration_min is not None else None
#     except (ValueError, TypeError):
#         distance_km = None
#         duration_min = None

#     # Estimate fare
#     fare = estimate_amount(
#         distance_km=distance_km or 0,
#         duration_min=duration_min or 0,
#         vehicle_type=vehicle_type
#     )
#     estimated_fare = fare['total_fare']

#     # Resolve VehicleType for storing on Trip
#     from servers.driver.models import VehicleType, Vehicle
#     requested_vt = None
#     if vehicle_type:
#         requested_vt = VehicleType.objects.filter(type__iexact=vehicle_type).first()

#     try:
#         with transaction.atomic():
#             trip_obj = Trip.objects.create(
#                 user_id=request.user,
#                 pickup_lat=pickup_lat,
#                 pickup_long=pickup_long,
#                 destination_lat=destination_lat,
#                 destination_long=destination_long,
#                 pickup_address=pickup_address,
#                 destination_address=destination_address,
#                 estimated_fare=estimated_fare,
#                 estimated_distance_km=Decimal(str(distance_km)) if distance_km else None,
#                 surge_multiplier=fare['surge_multiplier'],
#                 requested_vehicle_type=requested_vt,
#             )

#             # Create FarePricing breakdown record
#             FarePricing.objects.create(
#                 trip_id=trip_obj,
#                 base_fare=fare['base_fare'],
#                 distance_fare=fare['distance_fare'],
#                 time_fare=fare['time_fare'],
#                 surge_multiplier=fare['surge_multiplier'],
#                 total_fare=fare['total_fare'],
#             )

#             # Publish ride request to Redis Stream
#             publish_ride_request(
#                 ride_id=trip_obj.id,
#                 rider_id=request.user.id,
#                 pickup_lng=pickup_long,
#                 pickup_lat=pickup_lat,
#                 destination_lng=destination_long,
#                 destination_lat=destination_lat,
#             )

#         # Find nearby drivers and filter by vehicle type
#         drivers = nearby_drivers(lng=pickup_long, lat=pickup_lat, radius=5000, count=50)
#         nearby_count = 0
#         if drivers:
#             if vehicle_type:
#                 # Extract driver IDs from Redis results
#                 driver_ids = []
#                 for d in drivers:
#                     dk = d[0] if isinstance(d, (list, tuple)) else d
#                     if isinstance(dk, str) and dk.startswith('driver:'):
#                         driver_ids.append(dk.split(':')[1])
#                 # Filter by vehicle type
#                 if driver_ids:
#                     nearby_count = Vehicle.objects.filter(
#                         driver_id__id__in=driver_ids,
#                         vehicle_type_id__type__iexact=vehicle_type,
#                         status='active',
#                     ).values('driver_id').distinct().count()
#             else:
#                 nearby_count = len(drivers)

#         return success_response(
#             {
#                 'trip_id': trip_obj.id,
#                 'estimated_fare': str(trip_obj.estimated_fare),
#                 'fare_breakdown': {
#                     'base_fare': str(fare['base_fare']),
#                     'distance_fare': str(fare['distance_fare']),
#                     'time_fare': str(fare['time_fare']),
#                     'surge_multiplier': str(fare['surge_multiplier']),
#                 },
#                 'nearby_drivers_count': nearby_count,
#                 'message': 'Ride request created successfully',
#             },
#             status.HTTP_201_CREATED
#         )

#     except Exception as e:
#         logger.error(f"Error creating ride request: {str(e)}")
#         return error_response(
#             code='INTERNAL_ERROR',
#             message='Failed to create ride request',
#             field='general',
#             issue=str(e),
#             status=status.HTTP_500_INTERNAL_SERVER_ERROR
#         )


class TripPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 50


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ride_history(request):
    """
    Get ride history for the authenticated rider.
    Returns paginated list of past trips, newest first.
    
    Query params:
        ?page=1         - Page number
        ?page_size=10   - Items per page (max 50)
        ?status=completed - Filter by status (optional)
    """
    trips = Trip.objects.filter(
        user_id=request.user
    ).select_related(
        'status_id', 'driver_id', 'driver_id__user_id', 'vehicle_id', 'vehicle_id__vehicle_type_id'
    ).order_by('-requested_at')

    # Optional status filter
    status_filter = request.query_params.get('status')
    if status_filter:
        trips = trips.filter(status_id__status_code=status_filter)

    paginator = TripPagination()
    page = paginator.paginate_queryset(trips, request)
    serializer = TripListSerializer(page, many=True)

    return paginator.get_paginated_response(serializer.data)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsDriver])
def driver_history(request):
    """
    Get trip history for the authenticated driver.
    Returns paginated list of past trips, newest first.
    
    Query params:
        ?page=1         - Page number
        ?page_size=10   - Items per page (max 50)
        ?status=completed - Filter by status (optional)
    """
    try:
        driver = request.user.driver
    except Exception:
        return error_response(
            code='NOT_DRIVER',
            message='No driver profile found for this user',
            field='user',
            issue='User does not have a driver profile',
            status=status.HTTP_403_FORBIDDEN
        )

    trips = Trip.objects.filter(
        driver_id=driver
    ).select_related(
        'status_id', 'user_id', 'vehicle_id', 'vehicle_id__vehicle_type_id'
    ).order_by('-requested_at')

    # Optional status filter
    status_filter = request.query_params.get('status')
    if status_filter:
        trips = trips.filter(status_id__status_code=status_filter)

    paginator = TripPagination()
    page = paginator.paginate_queryset(trips, request)
    serializer = TripListSerializer(page, many=True)

    return paginator.get_paginated_response(serializer.data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def trip_detail(request, trip_id):
    """
    Get detailed info for a single trip, including fare breakdown and ratings.
    Only the rider or assigned driver can view.
    Checks Redis cache first for active trips before querying PostgreSQL.
    """
    from servers.redis_client import get_cached_trip

    # --- Cache-ahead read for active trips ---
    cached = get_cached_trip(trip_id)
    if cached:
        # Verify the requesting user is the rider or driver from cache
        cached_rider_id = cached.get('rider_id')
        cached_driver_id = cached.get('driver_id')
        is_rider = str(request.user.id) == str(cached_rider_id)
        is_driver = (
            cached_driver_id and
            hasattr(request.user, 'driver') and
            str(request.user.driver.id) == str(cached_driver_id)
        )
        if not is_rider and not is_driver:
            return error_response(
                code='FORBIDDEN',
                message='You do not have access to this trip',
                field='trip_id',
                issue='Only the rider or assigned driver can view this trip',
                status=status.HTTP_403_FORBIDDEN
            )
        # Return lightweight cached response
        return success_response({
            'trip_id': trip_id,
            'status': cached.get('status'),
            'rider_id': cached.get('rider_id'),
            'driver_id': cached.get('driver_id'),
            'pickup_lat': cached.get('pickup_lat'),
            'pickup_lng': cached.get('pickup_lng'),
            'destination_lat': cached.get('destination_lat'),
            'destination_lng': cached.get('destination_lng'),
            'estimated_fare': cached.get('estimated_fare'),
            'payment_method': cached.get('payment_method'),
            'source': 'cache',
        }, status.HTTP_200_OK)

    # --- Cache miss: fall back to full DB query ---
    try:
        trip = Trip.objects.select_related(
            'status_id', 'driver_id', 'driver_id__user_id',
            'vehicle_id', 'vehicle_id__vehicle_type_id'
        ).prefetch_related(
            'fare_pricing', 'ratings', 'ratings__rater_id'
        ).get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Trip not found',
            field='trip_id',
            issue=f'No trip with id {trip_id}',
            status=status.HTTP_404_NOT_FOUND
        )

    # Access check: must be the rider or the assigned driver
    is_rider = trip.user_id_id == request.user.id
    is_driver = (
        trip.driver_id and
        hasattr(request.user, 'driver') and
        trip.driver_id_id == request.user.driver.id
    )
    if not is_rider and not is_driver:
        return error_response(
            code='FORBIDDEN',
            message='You do not have access to this trip',
            field='trip_id',
            issue='Only the rider or assigned driver can view this trip',
            status=status.HTTP_403_FORBIDDEN
        )

    serializer = TripDetailSerializer(trip, context={'request': request})
    return success_response(serializer.data, status.HTTP_200_OK)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def trip_driver_details(request,trip_id):
    from servers.redis_client import get_cached_trip
    
    # Check cache first for rapid response. NOTE: this endpoint returns
    # DRIVER details — the rider's OTP is never returned here. The OTP is
    # only visible to the rider via /ride/active/ or /ride/trip/<id>/.
    cached = get_cached_trip(trip_id)
    if cached and 'driver_id' in cached:
        return success_response({
            'id': trip_id,
            'status': cached.get('status'),
            'driver_name': cached.get('driver_name'),
            'driver_phone': cached.get('driver_phone'),
            'driver_rating': cached.get('driver_rating'),
            'vehicle_info': {
                'vehicle_number': cached.get('vehicle_number'),
                'brand': cached.get('vehicle_brand'),
                'model': cached.get('vehicle_model'),
                'color': cached.get('vehicle_color')
            },
            'source': 'cache'
        }, status.HTTP_200_OK)

    # Fall back to DB query
    try:
        trip = Trip.objects.select_related(
            'status_id', 'driver_id', 'driver_id__user_id',
            'vehicle_id', 'vehicle_id__vehicle_type_id'
        ).get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Trip not found',
            field='trip_id',
            issue=f'No trip with id {trip_id}',
            status=status.HTTP_404_NOT_FOUND
        )

    data = {
        'id': trip.id,
        'status': trip.status_id.status_code if trip.status_id else 'pending',
        'driver_name': str(trip.driver_id) if trip.driver_id else None,
        'driver_phone': trip.driver_id.user_id.phone_number if trip.driver_id and trip.driver_id.user_id else None,
        'driver_rating': str(trip.driver_id.ratings) if trip.driver_id else None,
        'vehicle_info': None,
        'source': 'database'
    }

    if trip.vehicle_id:
        v = trip.vehicle_id
        data['vehicle_info'] = {
            'vehicle_number': v.vehicle_number,
            'brand': v.brand,
            'model': v.model,
            'color': v.color
        }
        
    return success_response(data, status.HTTP_200_OK)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def rate_trip(request):
    """
    Submit a rating for a completed trip.

    Expected: {
        "trip_id": int,
        "score": int (1-5),
        "comments": str (optional)
    }

    - Riders rate drivers, drivers rate riders.
    - One rating per user per trip.
    - Auto-updates the rated user's average rating.
    """
    from servers.ride.models import Rating
    from servers.driver.models import Driver
    from servers.rider.models import Rider
    from django.db.models import Avg, F

    trip_id = request.data.get('trip_id')
    score = request.data.get('score')
    comments = request.data.get('comments', '')

    if not trip_id or score is None:
        return error_response(
            code='MISSING_FIELDS',
            message='trip_id and score are required',
            field='trip_id,score',
            issue='Both trip_id and score must be provided',
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        score = int(score)
    except (ValueError, TypeError):
        return error_response(
            code='INVALID_SCORE',
            message='Score must be an integer',
            field='score',
            issue='Score must be a number between 1 and 5',
            status=status.HTTP_400_BAD_REQUEST
        )

    if score < 1 or score > 5:
        return error_response(
            code='INVALID_SCORE',
            message='Score must be between 1 and 5',
            field='score',
            issue=f'Received {score}, expected 1-5',
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        trip = Trip.objects.select_related('driver_id', 'user_id', 'status_id').get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Trip not found',
            field='trip_id',
            issue=f'Trip {trip_id} does not exist',
            status=status.HTTP_404_NOT_FOUND
        )

    # Must be completed
    if not trip.status_id or trip.status_id.status_code != 'completed':
        return error_response(
            code='TRIP_NOT_COMPLETED',
            message='You can only rate a completed trip',
            field='trip_id',
            issue='Trip is not in completed status',
            status=status.HTTP_400_BAD_REQUEST
        )

    user = request.user
    is_rider = str(trip.user_id_id) == str(user.id)
    is_driver = trip.driver_id and str(trip.driver_id.user_id_id) == str(user.id)

    if not is_rider and not is_driver:
        return error_response(
            code='FORBIDDEN',
            message='You are not a participant of this trip',
            field='trip_id',
            issue='Only the rider or driver can rate this trip',
            status=status.HTTP_403_FORBIDDEN
        )

    # Check for duplicate rating
    if Rating.objects.filter(trip_id=trip, rater_id=user).exists():
        return error_response(
            code='ALREADY_RATED',
            message='You have already rated this trip',
            field='trip_id',
            issue='Duplicate rating not allowed',
            status=status.HTTP_409_CONFLICT
        )

    with transaction.atomic():
        rating = Rating.objects.create(
            trip_id=trip,
            rater_id=user,
            score=score,
            comments=comments
        )

        # Recompute the rated person's average across ALL their trips.
        #
        # The previous query filtered `rater_id=trip.user_id`, which scoped
        # the average to ratings given by *this single rider*, then excluded
        # the driver themselves. That meant a driver's "average score" was
        # actually "average score the current rider has given this driver"
        # — usually one or two data points, often the just-submitted score.
        #
        # The correct query for a driver's average is: every Rating whose
        # trip's driver was this driver AND whose rater was that trip's
        # rider. F('trip_id__user_id') checks the rater of each row equals
        # the rider of THAT row's trip — confirming it's a rider→driver
        # rating, not the other direction.
        if is_rider and trip.driver_id:
            driver = trip.driver_id
            avg = Rating.objects.filter(
                trip_id__driver_id=driver,
                rater_id=F('trip_id__user_id'),
            ).aggregate(avg_score=Avg('score'))['avg_score']
            if avg:
                driver.ratings = round(Decimal(str(avg)), 2)
                driver.save(update_fields=['ratings'])

        elif is_driver:
            # Driver -> rider rating. Routed through the rating-decay
            # service so flag-for-review + soft-block thresholds are
            # honoured. Previous code did a raw running average that
            # never touched flagged_for_review.
            try:
                rider = Rider.objects.get(user_id=trip.user_id)
                from servers.rider.rating_decay import apply_rider_rating
                apply_rider_rating(rider, score)
            except Rider.DoesNotExist:
                pass

    return success_response({
        'rating_id': rating.id,
        'trip_id': trip.id,
        'score': rating.score,
        'comments': rating.comments,
        'message': 'Rating submitted successfully',
    }, status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def receipt_pdf(request, trip_id):
    """Return a signed URL to the latest Receipt's PDF for this trip.

    Rider-only (or admin). Returns 404 if no receipt exists yet, or if
    the receipt has no PDF (very early trips, or PDF generation
    failed). Use /receipt/resend/ to force re-issue, which retries the
    PDF render too.
    """
    from servers.ride.models import Receipt, Trip
    try:
        trip = Trip.objects.get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Trip not found.',
            field='trip_id', issue=f'No trip {trip_id}',
            status=status.HTTP_404_NOT_FOUND,
        )
    if not (trip.user_id_id == request.user.id or request.user.is_staff):
        return error_response(
            code='FORBIDDEN', message='Not your trip.',
            field='trip_id', issue='User mismatch',
            status=status.HTTP_403_FORBIDDEN,
        )
    receipt = Receipt.objects.filter(trip_id=trip).order_by('-version', '-id').first()
    if not receipt or not receipt.pdf_file:
        return error_response(
            code='PDF_UNAVAILABLE',
            message='No PDF available for this trip yet. Try resending the receipt.',
            field='trip_id', issue='no pdf_file on latest Receipt',
            status=status.HTTP_404_NOT_FOUND,
        )
    return success_response(
        {
            'trip_id': trip.id,
            'receipt_number': receipt.receipt_number,
            'pdf_url': receipt.pdf_file.url,
            'version': receipt.version,
        },
        status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def apply_promo_endpoint(request):
    """Preview a promo code against a fare quote.

    Body: {"code": str, "fare": "150.00",
           "pickup_lat": ..., "pickup_long": ...}
    The pickup is used to scope zone-bound promos. We don't redeem
    yet; that happens once the trip is created. The response is the
    same shape as PromoResult.to_dict().
    """
    from servers.ride.promos import apply_promo
    from servers.pricing.services import find_zone_for_point
    code = (request.data.get('code') or '').strip()
    fare = request.data.get('fare')
    if not code or fare is None:
        return error_response(
            code='MISSING_FIELDS', message='code + fare required',
            field='code,fare', issue='Both required',
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        fare_dec = Decimal(str(fare))
    except Exception:
        return error_response(
            code='INVALID_TYPE', message='fare must be numeric',
            field='fare', issue=str(fare),
            status=status.HTTP_400_BAD_REQUEST,
        )
    zone = None
    p_lat = request.data.get('pickup_lat')
    p_lon = request.data.get('pickup_long')
    if p_lat is not None and p_lon is not None:
        try:
            zone = find_zone_for_point(float(p_lat), float(p_lon))
        except Exception:
            zone = None
    result = apply_promo(code, request.user, fare_dec, zone=zone)
    http_status = status.HTTP_200_OK if result.ok else status.HTTP_400_BAD_REQUEST
    return success_response(result.to_dict(), http_status) if result.ok else error_response(
        code=result.reason or 'PROMO_INVALID',
        message=result.description or 'Promo could not be applied.',
        field='code', issue=result.reason,
        status=http_status,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def trip_chat_history(request, trip_id):
    """Paginated chat history for a trip.

    Both rider and driver may read; admin too. Messages are returned
    in chronological order. Marks all the caller's unread messages as
    read on each fetch (cheap query, sets read_at).
    """
    from servers.ride.models import ChatMessage, Trip
    from django.utils import timezone
    try:
        trip = Trip.objects.select_related('driver_id').get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Trip not found.',
            field='trip_id', issue=f'No trip {trip_id}',
            status=status.HTTP_404_NOT_FOUND,
        )
    is_rider = trip.user_id_id == request.user.id
    is_driver = trip.driver_id and trip.driver_id.user_id_id == request.user.id
    if not (is_rider or is_driver or request.user.is_staff):
        return error_response(
            code='FORBIDDEN', message='Not a participant.',
            field='trip_id', issue='User mismatch',
            status=status.HTTP_403_FORBIDDEN,
        )

    qs = ChatMessage.objects.filter(trip=trip).order_by('created_at')
    paginator = PageNumberPagination()
    paginator.page_size = 50
    page = paginator.paginate_queryset(qs, request)
    payload = [
        {
            'id': m.id,
            'sender_role': m.sender_role,
            'body': m.body,
            'is_system': m.is_system,
            'created_at': m.created_at.isoformat(),
            'read_at': m.read_at.isoformat() if m.read_at else None,
        }
        for m in page
    ]
    # Mark peer messages as read for this caller.
    role = 'rider' if is_rider else ('driver' if is_driver else None)
    if role:
        ChatMessage.objects.filter(trip=trip).exclude(
            sender_role=role,
        ).filter(read_at__isnull=True).update(read_at=timezone.now())
    return paginator.get_paginated_response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def resend_receipt(request, trip_id):
    """Re-send the rider's receipt for a completed trip.

    The rider, or an admin, may trigger this. Sends the most recent
    Receipt row's stored html_body via email; does NOT create a new
    Receipt version (for that, an admin uses Django admin to issue a
    new version after a fare adjustment).
    """
    from servers.ride.models import Trip, Receipt
    from servers.ride.receipts import issue_receipt

    try:
        trip = Trip.objects.select_related('user_id', 'status_id').get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Trip not found.', field='trip_id',
            issue=f'No trip with id={trip_id}', status=status.HTTP_404_NOT_FOUND,
        )

    if not (trip.user_id_id == request.user.id or request.user.is_staff):
        return error_response(
            code='FORBIDDEN', message='Only the rider or an admin may resend.',
            field='trip_id', issue='User mismatch',
            status=status.HTTP_403_FORBIDDEN,
        )

    if not trip.status_id or trip.status_id.status_code != 'completed':
        return error_response(
            code='INVALID_STATE',
            message='Receipts are only available for completed trips.',
            field='trip_id',
            issue=f'Trip status is {trip.status_id.status_code if trip.status_id else "unknown"!r}',
            status=status.HTTP_400_BAD_REQUEST,
        )

    receipt = issue_receipt(trip, force_resend=True)
    if not receipt:
        return error_response(
            code='RECEIPT_UNAVAILABLE', message='Could not issue receipt.',
            field='trip_id', issue='issue_receipt returned None',
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return success_response(
        {
            'trip_id': trip.id,
            'receipt_number': receipt.receipt_number,
            'sent_to': receipt.sent_to_email,
            'last_sent_at': receipt.last_sent_at.isoformat() if receipt.last_sent_at else None,
            'failure_reason': receipt.send_failure_reason or None,
        },
        status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsDriver])
def driver_cancel_trip(request, trip_id):
    """Driver cancels a trip AFTER accepting it (before the trip starts).

    Body: {"reason": "no_show|vehicle_issue|safety|personal|other",
           "note": "optional free-text"}

    Cancelling a trip carries a penalty:
        * Recorded in DriverCancellation (24h rolling counter).
        * 3 cancels in 24h -> 1-hour online lockout
          (Driver.fatigue_lockout_until).
        * Rating decremented by 0.1 (clamped at 0).

    Only the trip's driver may call this, and only while the trip is
    in 'accepted' or 'reached' state. Once the trip is in 'in_progress'
    the driver cannot cancel from the app -- they must use support.
    """
    from django.utils import timezone
    from servers.ride.models import Trip, TripStatus
    from servers.driver.fatigue import apply_cancellation_penalty
    from servers.rider.models import Notification

    reason = (request.data.get('reason') or '').strip()
    note = (request.data.get('note') or '').strip()
    valid_reasons = {'no_show', 'vehicle_issue', 'safety', 'personal', 'other'}
    if reason not in valid_reasons:
        return error_response(
            code='INVALID_REASON',
            message=f'reason must be one of: {", ".join(sorted(valid_reasons))}',
            field='reason',
            issue=f'Got reason={reason!r}',
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with transaction.atomic():
            trip = Trip.objects.select_for_update().select_related(
                'status_id', 'driver_id', 'driver_id__user_id', 'user_id',
            ).get(id=trip_id)

            if not trip.driver_id or trip.driver_id.user_id_id != request.user.id:
                return error_response(
                    code='FORBIDDEN',
                    message='Only the assigned driver can cancel this trip.',
                    field='trip_id',
                    issue='Driver mismatch',
                    status=status.HTTP_403_FORBIDDEN,
                )

            current_state = (trip.status_id.status_code if trip.status_id else '').lower()
            if current_state not in ('accepted', 'reached'):
                return error_response(
                    code='INVALID_STATE',
                    message=(
                        'A driver-initiated cancel is only allowed before the '
                        'trip starts. Use support if the trip is already in '
                        'progress.'
                    ),
                    field='trip_id',
                    issue=f'Trip is in {current_state!r}',
                    status=status.HTTP_400_BAD_REQUEST,
                )

            cancelled_status, _ = TripStatus.objects.get_or_create(
                status_code='cancelled',
                defaults={'description': 'Trip cancelled'},
            )
            trip.status_id = cancelled_status
            trip.cancelled_at = timezone.now()
            trip.save(update_fields=['status_id', 'cancelled_at'])

            driver = trip.driver_id
            penalty = apply_cancellation_penalty(driver, trip, reason, note)

        # Free the driver from the Redis active-trip map so they can be
        # offered new requests once any lockout expires.
        try:
            from servers.redis_client import clear_driver_active_trip
            clear_driver_active_trip(driver.id)
        except Exception:  # noqa: BLE001
            logger.exception('clear_driver_active_trip failed for driver=%s', driver.id)

        try:
            Notification.objects.create(
                user_id=trip.user_id,
                title='Driver cancelled your trip',
                message=(
                    f"Your assigned driver cancelled Trip #{trip.id}. "
                    f"We're finding another driver. Reason: {reason.replace('_', ' ')}."
                ),
                notif_type='ride_event',
                trip=trip,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('Rider cancel notification failed: %s', exc)

        return success_response(
            {
                'trip_id': trip.id,
                'status': 'cancelled',
                'penalty': penalty.to_dict(),
            },
            status.HTTP_200_OK,
        )

    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Trip not found.',
            field='trip_id',
            issue=f'No trip with id={trip_id}',
            status=status.HTTP_404_NOT_FOUND,
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def rider_cancel_trip(request, trip_id):
    """Rider cancels their own trip.

    Body (optional):
        {"reason": "no_show|wrong_location|too_long|safety|other",
         "note": "optional free-text"}

    The rider can cancel in any active state: 'requested', 'accepted',
    'reached', or 'in_progress'. If a driver is assigned, they are
    notified via push + WebSocket and their active-trip flag is cleared.
    If an online payment was completed, a refund is initiated.
    """
    from django.utils import timezone
    from servers.ride.models import Trip, TripStatus
    from servers.ride.utils import process_refund_on_cancel
    from servers.rider.models import Notification
    from servers.auth_user.services import send_push_notification

    reason = (request.data.get('reason') or '').strip()
    note = (request.data.get('note') or '').strip()

    # Optional: validate reason if provided
    if reason:
        valid_reasons = {'no_show', 'wrong_location', 'too_long', 'safety', 'other'}
        if reason not in valid_reasons:
            return error_response(
                code='INVALID_REASON',
                message=f'reason must be one of: {", ".join(sorted(valid_reasons))}',
                field='reason',
                issue=f'Got reason={reason!r}',
                status=status.HTTP_400_BAD_REQUEST,
            )

    try:
        with transaction.atomic():
            trip = Trip.objects.select_for_update().select_related(
                'status_id', 'driver_id', 'driver_id__user_id', 'user_id',
            ).get(id=trip_id)

            # 1. Authorization: only the trip's rider
            if trip.user_id_id != request.user.id:
                return error_response(
                    code='FORBIDDEN',
                    message='Only the rider who booked this trip can cancel it.',
                    field='trip_id',
                    issue='Rider mismatch',
                    status=status.HTTP_403_FORBIDDEN,
                )

            # 2. State validation
            current_state = (
                trip.status_id.status_code if trip.status_id else ''
            ).lower()
            if current_state in ('completed', 'cancelled'):
                return error_response(
                    code='INVALID_STATE',
                    message=f'Trip is already {current_state}.',
                    field='trip_id',
                    issue=f'Cannot cancel a {current_state} trip',
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if current_state not in ('requested', 'accepted', 'reached', 'in_progress'):
                return error_response(
                    code='INVALID_STATE',
                    message=f'Cannot cancel trip in {current_state!r} state.',
                    field='trip_id',
                    issue='Unexpected trip state',
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # 3. Apply cancellation
            cancelled_status, _ = TripStatus.objects.get_or_create(
                status_code='cancelled',
                defaults={'description': 'Trip cancelled'},
            )
            trip.status_id = cancelled_status
            trip.cancelled_at = timezone.now()
            trip.save(update_fields=['status_id', 'cancelled_at'])

            # 4. Process refund if applicable
            refund_result = process_refund_on_cancel(trip)

            # 5. Notification for the rider
            try:
                Notification.objects.create(
                    user_id=trip.user_id,
                    title='Ride Cancelled',
                    message='Your ride has been cancelled.',
                    notif_type='ride_event',
                    trip=trip,
                )
            except Exception as exc:
                logger.warning('Rider cancel notification failed: %s', exc)

            # 6. Push notification to rider
            try:
                send_push_notification(
                    trip.user_id,
                    'Ride Cancelled',
                    'Your ride has been cancelled.',
                    {'trip_id': str(trip.id), 'type': 'ride_cancelled'},
                )
            except Exception as exc:
                logger.warning('Rider cancel push failed: %s', exc)

            driver_id = trip.driver_id_id

        # --- End of transaction.atomic() ---

        # 7. Clear driver's active trip in Redis (if assigned)
        if driver_id:
            try:
                from servers.redis_client import clear_driver_active_trip
                clear_driver_active_trip(driver_id)
            except Exception:
                logger.exception(
                    'clear_driver_active_trip failed for driver=%s', driver_id
                )

        # 8. Invalidate trip cache in Redis
        try:
            from servers.redis_client import invalidate_trip
            invalidate_trip(trip.id)
        except Exception:
            logger.exception('invalidate_trip failed for trip=%s', trip.id)

        # 9. Notify driver if assigned (push + in-app notification)
        if driver_id:
            try:
                Notification.objects.create(
                    user_id=trip.driver_id.user_id,
                    title='Ride Cancelled by Rider',
                    message=(
                        f'Rider cancelled Trip #{trip.id}.'
                        + (f' Reason: {reason.replace("_", " ")}.' if reason else '')
                    ),
                    notif_type='ride_event',
                    trip=trip,
                )
            except Exception as exc:
                logger.warning('Driver cancel notification failed: %s', exc)

            try:
                send_push_notification(
                    trip.driver_id.user_id,
                    'Ride Cancelled',
                    f'Rider cancelled Trip #{trip.id}.',
                    {'trip_id': str(trip.id), 'type': 'ride_cancelled'},
                )
            except Exception as exc:
                logger.warning('Driver cancel push failed: %s', exc)

        # 10. Build response
        response_data = {
            'trip_id': trip.id,
            'status': 'cancelled',
            'cancelled_at': trip.cancelled_at.isoformat(),
        }
        if refund_result.get('refunded'):
            response_data['refund'] = {
                'amount': str(refund_result['amount']),
                'status': 'initiated',
            }

        return success_response(response_data, status.HTTP_200_OK)

    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Trip not found.',
            field='trip_id',
            issue=f'No trip with id={trip_id}',
            status=status.HTTP_404_NOT_FOUND,
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_active_trip(request):
    """
    Get the user's active trip for state recovery.
    Returns the current active trip (not completed/cancelled) if any.

    Used by Flutter app on launch to determine what screen to display.
    Returns 404 if no active trip exists.
    """
    # Active trips are those not completed and not cancelled
    active_statuses = ['requested','accepted', 'reached', 'in_progress']

    trip = Trip.objects.filter(
        user_id=request.user,
        status_id__status_code__in=active_statuses
    ).select_related(
        'status_id', 'driver_id', 'driver_id__user_id',
        'vehicle_id', 'vehicle_id__vehicle_type_id'
    ).order_by('-requested_at').first()

    # If no active trip, check for recently completed (within 1 hour)
    # This handles the case where app was killed after trip completion
    if not trip:
        from django.utils import timezone
        from datetime import timedelta

        one_hour_ago = timezone.now() - timedelta(hours=1)
        trip = Trip.objects.filter(
            user_id=request.user,
            status_id__status_code='completed',
            completed_at__gte=one_hour_ago
        ).select_related(
            'status_id', 'driver_id', 'vehicle_id'
        ).order_by('-completed_at').first()

    if not trip:
        return error_response(
            code='NO_ACTIVE_TRIP',
            message='No active trip found',
            field='trip',
            status=status.HTTP_404_NOT_FOUND
        )

    serializer = TripDetailSerializer(trip, context={'request': request})
    return success_response(serializer.data, status.HTTP_200_OK)

from servers.redis_client import check_maps_rate_limit, get_cached_map_data, cache_map_data
from servers.ride.maps_utils import google_places_autocomplete, google_directions

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def proxy_geocode(request):
    """
    Proxy for Google Places Autocomplete/Geocode.
    Expected data: {"input": "search string", "location": "lat,lng" (optional)}
    """
    user_id = request.user.id
    if not check_maps_rate_limit(user_id, limit=100, period=3600):
        return error_response(
            code='RATE_LIMIT_EXCEEDED',
            message='You have exceeded your API rate limit',
            field='api',
            issue='Too many requests to maps proxy',
            status=status.HTTP_429_TOO_MANY_REQUESTS
        )

    search_text = request.data.get('input')
    location = request.data.get('location')

    if not search_text:
        return error_response(
            code='MISSING_FIELDS',
            message='input field is required',
            field='input',
            issue='Search text not provided',
            status=status.HTTP_400_BAD_REQUEST
        )

    cache_key = f"maps:geocode:{search_text.lower().strip()}:{location or ''}"
    cached_data = get_cached_map_data(cache_key)
    if cached_data:
        return success_response(cached_data, status.HTTP_200_OK)

    result = google_places_autocomplete(search_text, location=location)
    if result is None:
        return error_response(
            code='API_ERROR',
            message='Failed to fetch data from Maps API',
            field='api',
            issue='Upstream service error',
            status=status.HTTP_502_BAD_GATEWAY
        )

    # Cache for 30 days
    cache_map_data(cache_key, result, ttl_seconds=30*24*60*60)
    return success_response(result, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def proxy_directions(request):
    """
    Proxy for Google Directions.
    Expected data: {
        "origin_lat": float, "origin_lng": float,
        "dest_lat": float, "dest_lng": float
    }
    """
    user_id = request.user.id
    if not check_maps_rate_limit(user_id, limit=100, period=3600):
        return error_response(
            code='RATE_LIMIT_EXCEEDED',
            message='You have exceeded your API rate limit',
            field='api',
            issue='Too many requests to maps proxy',
            status=status.HTTP_429_TOO_MANY_REQUESTS
        )

    origin_lat = request.data.get('origin_lat')
    origin_lng = request.data.get('origin_lng')
    dest_lat = request.data.get('dest_lat')
    dest_lng = request.data.get('dest_lng')

    if not all([origin_lat, origin_lng, dest_lat, dest_lng]):
        return error_response(
            code='MISSING_FIELDS',
            message='origin and destination coordinates are required',
            field='coordinates',
            issue='Missing lat/lng fields',
            status=status.HTTP_400_BAD_REQUEST
        )

    # Round to 4 decimal places for caching (~11m precision)
    try:
        origin_lat = round(float(origin_lat), 4)
        origin_lng = round(float(origin_lng), 4)
        dest_lat = round(float(dest_lat), 4)
        dest_lng = round(float(dest_lng), 4)
    except (ValueError, TypeError):
        return error_response(
            code='INVALID_TYPE',
            message='Coordinates must be numbers',
            field='coordinates',
            issue='Invalid type',
            status=status.HTTP_400_BAD_REQUEST
        )

    cache_key = f"maps:directions:{origin_lat},{origin_lng}:{dest_lat},{dest_lng}"
    cached_data = get_cached_map_data(cache_key)
    if cached_data:
        return success_response(cached_data, status.HTTP_200_OK)

    result = google_directions(origin_lat, origin_lng, dest_lat, dest_lng)
    if result is None:
        return error_response(
            code='API_ERROR',
            message='Failed to fetch data from Maps API',
            field='api',
            issue='Upstream service error',
            status=status.HTTP_502_BAD_GATEWAY
        )

    # Cache for 5 minutes
    cache_map_data(cache_key, result, ttl_seconds=300)
    return success_response(result, status.HTTP_200_OK)