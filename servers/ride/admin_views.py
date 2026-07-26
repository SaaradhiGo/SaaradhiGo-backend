from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from base.utils import success_response, error_response
from base.permissions import IsAdmin
from servers.ride.models import Trip, Receipt, ChatMessage, PromoRedemption, FarePricing, Rating
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
def admin_trip_detail(request, trip_id):
    """Single endpoint returning everything ops needs to investigate
    one trip.

    Aggregates across nine tables in a single response so the ops
    console doesn't have to make a fan-out of N requests for every
    detail screen. Heavy fields (full chat history) are paged via
    `?chat_offset=` and `?chat_limit=` query params; by default the
    last 50 messages come inline.

    Permission: IsAdmin only. Riders/drivers use the existing
    /api/v1/ride/trip/<id>/ endpoint for their own trip view.
    """
    chat_limit = max(1, min(int(request.query_params.get('chat_limit', 50)), 200))
    chat_offset = max(0, int(request.query_params.get('chat_offset', 0)))

    try:
        trip = Trip.objects.select_related(
            'status_id', 'driver_id', 'driver_id__user_id',
            'vehicle_id', 'vehicle_id__vehicle_type_id', 'user_id',
            'requested_vehicle_type',
        ).get(id=trip_id)
    except Trip.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Trip not found.',
            field='trip_id', issue=f'No trip {trip_id}',
            status=status.HTTP_404_NOT_FOUND,
        )

    # ---- rider ----
    rider_user = trip.user_id
    rider_payload = None
    if rider_user:
        from servers.rider.models import Rider
        rider_profile = Rider.objects.filter(user_id=rider_user).first()
        rider_payload = {
            'user_id': rider_user.id,
            'phone_number': getattr(rider_user, 'phone_number', None),
            'full_name': getattr(rider_user, 'full_name', None),
            'email': getattr(rider_user, 'email', None),
            'rating': str(rider_profile.rating) if rider_profile else None,
            'rating_count': rider_profile.rating_count if rider_profile else 0,
            'flagged_for_review': bool(rider_profile.flagged_for_review) if rider_profile else False,
            'review_flagged_at': rider_profile.review_flagged_at.isoformat() if rider_profile and rider_profile.review_flagged_at else None,
            'review_cleared_at': rider_profile.review_cleared_at.isoformat() if rider_profile and rider_profile.review_cleared_at else None,
        }

    # ---- driver ----
    driver_payload = None
    if trip.driver_id:
        d = trip.driver_id
        driver_payload = {
            'driver_id': d.id,
            'user_id': d.user_id.id if d.user_id else None,
            'phone_number': d.user_id.phone_number if d.user_id else None,
            'full_name': d.user_id.full_name if d.user_id else None,
            'status': d.status,
            'approved': d.approved,
            'ratings': str(d.ratings),
            'total_trips': d.total_trips,
            'license_expiry': d.license_expiry.isoformat() if d.license_expiry else None,
            'fatigue_lockout_until': d.fatigue_lockout_until.isoformat() if d.fatigue_lockout_until else None,
        }

    # ---- vehicle ----
    vehicle_payload = None
    if trip.vehicle_id:
        v = trip.vehicle_id
        vehicle_payload = {
            'id': v.id,
            'type': v.vehicle_type_id.type if v.vehicle_type_id else None,
            'brand': v.brand,
            'model': v.model,
            'color': v.color,
            'year': v.year,
            'vehicle_number': v.vehicle_number,
            'insurance_expiry': v.insurance_expiry.isoformat() if v.insurance_expiry else None,
            'permit_expiry': v.permit_expiry.isoformat() if v.permit_expiry else None,
            'fitness_expiry': v.fitness_expiry.isoformat() if v.fitness_expiry else None,
            'puc_expiry': v.puc_expiry.isoformat() if v.puc_expiry else None,
        }

    # ---- timeline ----
    timeline = {
        'requested_at': trip.requested_at.isoformat() if trip.requested_at else None,
        'accepted_at': trip.accepted_at.isoformat() if trip.accepted_at else None,
        'reached_at': trip.reached_at.isoformat() if trip.reached_at else None,
        'started_at': trip.started_at.isoformat() if trip.started_at else None,
        'completed_at': trip.completed_at.isoformat() if trip.completed_at else None,
        'cancelled_at': trip.cancelled_at.isoformat() if trip.cancelled_at else None,
    }

    # ---- fare breakdown ----
    fp = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
    fare_breakdown = None
    if fp:
        fare_breakdown = {
            'base_fare': str(fp.base_fare),
            'distance_fare': str(fp.distance_fare),
            'time_fare': str(fp.time_fare),
            'surge_multiplier': str(fp.surge_multiplier),
            'total_fare': str(fp.total_fare),
        }
    fare_payload = {
        'estimated_fare': str(trip.estimated_fare) if trip.estimated_fare is not None else None,
        'final_fare': str(trip.final_fare) if trip.final_fare is not None else None,
        'estimated_distance_km': str(trip.estimated_distance_km) if trip.estimated_distance_km is not None else None,
        'actual_distance_km': str(trip.actual_distance_km) if trip.actual_distance_km is not None else None,
        'surge_multiplier': str(trip.surge_multiplier) if trip.surge_multiplier is not None else None,
        'payment_method': trip.payment_method,
        'payment_status': trip.payment_status,
        'breakdown': fare_breakdown,
    }

    # ---- payments ----
    payments_payload = []
    try:
        from servers.payments.models import Payment
        for p in Payment.objects.filter(trip_id=trip).order_by('-created_at'):
            payments_payload.append({
                'id': p.id,
                'amount': str(p.amount),
                'method': p.method,
                'status': p.status,
                'payment_gateway': getattr(p, 'payment_gateway', None),
                'gateway_order_id': getattr(p, 'gateway_order_id', None) or getattr(p, 'cashfree_order_id', None),
                'gateway_payment_id': getattr(p, 'gateway_payment_id', None) or getattr(p, 'cashfree_payment_id', None),
                'created_at': p.created_at.isoformat() if hasattr(p, 'created_at') and p.created_at else None,
            })
    except Exception:
        # Payment model shape may vary slightly across deploys; never let
        # this break the detail call.
        pass

    # ---- ratings (both sides) ----
    ratings_payload = []
    for r in Rating.objects.filter(trip_id=trip).select_related('rater_id'):
        rater = r.rater_id
        # Derive direction: if rater is the trip's rider, this is
        # rider->driver. Vice versa.
        direction = 'unknown'
        if rater and trip.user_id and rater.id == trip.user_id_id:
            direction = 'rider_to_driver'
        elif rater and trip.driver_id and trip.driver_id.user_id_id and rater.id == trip.driver_id.user_id_id:
            direction = 'driver_to_rider'
        ratings_payload.append({
            'id': r.id,
            'direction': direction,
            'score': r.score,
            'comments': r.comments,
            'created_at': r.created_at.isoformat() if r.created_at else None,
        })

    # ---- receipts ----
    receipts_payload = []
    for rcpt in Receipt.objects.filter(trip_id=trip).order_by('-version'):
        receipts_payload.append({
            'id': rcpt.id,
            'receipt_number': rcpt.receipt_number,
            'version': rcpt.version,
            'total_fare': str(rcpt.total_fare),
            'gst_amount': str(rcpt.gst_amount),
            'sent_to_email': rcpt.sent_to_email,
            'last_sent_at': rcpt.last_sent_at.isoformat() if rcpt.last_sent_at else None,
            'send_failure_reason': rcpt.send_failure_reason or None,
            'pdf_url': rcpt.pdf_file.url if rcpt.pdf_file else None,
        })

    # ---- chat ----
    chat_qs = ChatMessage.objects.filter(trip=trip).order_by('created_at')
    chat_count = chat_qs.count()
    chat_messages = []
    for m in chat_qs[chat_offset:chat_offset + chat_limit]:
        chat_messages.append({
            'id': m.id,
            'sender_role': m.sender_role,
            'body': m.body,
            'is_system': m.is_system,
            'created_at': m.created_at.isoformat() if m.created_at else None,
            'read_at': m.read_at.isoformat() if m.read_at else None,
        })

    # ---- driver cancellations on THIS trip ----
    cancellations_payload = []
    try:
        from servers.driver.models import DriverCancellation
        for c in DriverCancellation.objects.filter(trip=trip).order_by('-created_at'):
            cancellations_payload.append({
                'id': c.id,
                'reason': c.reason,
                'note': c.note,
                'created_at': c.created_at.isoformat() if c.created_at else None,
            })
    except Exception:
        pass

    # ---- SOS events ----
    sos_payload = []
    try:
        from servers.sos.models import SOSEvent
        for e in SOSEvent.objects.filter(trip=trip).order_by('-created_at'):
            sos_payload.append({
                'id': e.id,
                'status': getattr(e, 'status', None),
                'created_at': e.created_at.isoformat() if e.created_at else None,
                'acknowledged_at': getattr(e, 'acknowledged_at', None).isoformat() if getattr(e, 'acknowledged_at', None) else None,
                'resolved_at': getattr(e, 'resolved_at', None).isoformat() if getattr(e, 'resolved_at', None) else None,
            })
    except Exception:
        pass

    # ---- promo redemption ----
    promo_payload = None
    pr = PromoRedemption.objects.filter(trip=trip).select_related('promo').first()
    if pr:
        promo_payload = {
            'code': pr.promo.code,
            'discount_type': pr.promo.discount_type,
            'discount_amount': str(pr.discount_amount),
            'created_at': pr.created_at.isoformat() if pr.created_at else None,
        }

    # ---- pricing zone (where the pickup landed) ----
    zone_payload = None
    try:
        from servers.pricing.services import find_zone_for_point
        z = find_zone_for_point(trip.pickup_lat, trip.pickup_long)
        if z:
            zone_payload = {
                'code': z.code,
                'name': z.name,
                'city': z.city,
                'state_code': z.state_code,
            }
    except Exception:
        pass

    return success_response(
        {
            'id': trip.id,
            'status': trip.status_id.status_code if trip.status_id else None,
            'pickup': {
                'address': trip.pickup_address,
                'lat': str(trip.pickup_lat) if trip.pickup_lat is not None else None,
                'lng': str(trip.pickup_long) if trip.pickup_long is not None else None,
            },
            'destination': {
                'address': trip.destination_address,
                'lat': str(trip.destination_lat) if trip.destination_lat is not None else None,
                'lng': str(trip.destination_long) if trip.destination_long is not None else None,
            },
            'timeline': timeline,
            'rider': rider_payload,
            'driver': driver_payload,
            'vehicle': vehicle_payload,
            'fare': fare_payload,
            'payments': payments_payload,
            'ratings': ratings_payload,
            'receipts': receipts_payload,
            'chat': {
                'total': chat_count,
                'offset': chat_offset,
                'limit': chat_limit,
                'messages': chat_messages,
            },
            'driver_cancellations': cancellations_payload,
            'sos_events': sos_payload,
            'promo': promo_payload,
            'zone': zone_payload,
            'otp': trip.otp,
        },
        status.HTTP_200_OK,
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
