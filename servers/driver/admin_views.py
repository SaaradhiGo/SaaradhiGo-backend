import logging
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from base.utils import success_response, error_response
from base.permissions import IsAdmin
from servers.driver.models import Driver,Vehicle
from servers.driver.serializers import DriverAdminListSerializer, DriverAdminDetailSerializer, KYCApprovalSerializer, VehicleSerializer
from rest_framework.pagination import PageNumberPagination

logger = logging.getLogger(__name__)

@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def list_drivers_admin(request):
    """
    List all drivers. Can be filtered by 'approved' and 'status'.
    """
    drivers = Driver.objects.all().select_related('user_id').order_by('-id')
    
    # Filter configuration
    approved = request.query_params.get('approved')
    driver_status = request.query_params.get('status')
    
    if approved is not None:
        approved_bool = approved.lower() in ['true', '1', 't', 'y', 'yes']
        drivers = drivers.filter(approved=approved_bool)
        
    if driver_status:
        drivers = drivers.filter(status=driver_status)

    paginator = PageNumberPagination()
    paginator.page_size = 10
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = 50
    page = paginator.paginate_queryset(drivers, request)
    
    serializer = DriverAdminListSerializer(page, many=True)
    return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def retrieve_driver_admin(request, driver_id):
    """
    Retrieve details of a single driver by its ID (along with user profile and vehicles).
    """
    try:
        driver = Driver.objects.prefetch_related('user_id', 'vehicle_set').get(id=driver_id)
        serializer = DriverAdminDetailSerializer(driver)
        return success_response(serializer.data, status.HTTP_200_OK)
    except Driver.DoesNotExist:
        return error_response(
            code="NOT_FOUND",
            message="Driver not found",
            field="driver_id",
            issue=f"No driver matches id {driver_id}",
            status=status.HTTP_404_NOT_FOUND
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def driver_full_detail_admin(request, driver_id):
    """Driver aggregate for the ops console driver-detail page.

    One endpoint, one round-trip. Mirrors the admin_trip_detail
    pattern: ops opens this page when there's a question about a
    driver and we'd rather return everything than fan out to N
    endpoints.

    Aggregates across nine surfaces:
      core driver row (rating, status, fatigue lockout, license expiry)
      user_id (phone, full_name, email)
      vehicles[]  -- every vehicle on file with all four credential
                     expiries; active_vehicle is flagged
      earnings_summary: lifetime earned, today, current-month, last
                        withdrawal date
      sessions[] (last 20)  -- online/offline ledger for the fatigue
                               audit story
      cancellations_24h / cancellations_7d / cancellations_30d counts
      withdrawals[] (last 10)
      recent_trips[] (last 20)
      fatigue_status -- computed via driver.fatigue.get_fatigue_status

    Heavier paginated views (full trip history, full withdrawal log)
    stay on their existing endpoints.
    """
    from datetime import timedelta
    from decimal import Decimal
    from django.db.models import Sum
    from django.utils import timezone

    try:
        driver = Driver.objects.select_related(
            'user_id', 'active_vehicle', 'active_vehicle__vehicle_type_id',
        ).get(id=driver_id)
    except Driver.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Driver not found.',
            field='driver_id', issue=f'No driver {driver_id}',
            status=status.HTTP_404_NOT_FOUND,
        )

    user = driver.user_id
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)
    last_30d = now - timedelta(days=30)

    # ---- vehicles ----
    vehicles_payload = []
    for v in driver.vehicle_set.select_related('vehicle_type_id').all():
        vehicles_payload.append({
            'id': v.id,
            'type': v.vehicle_type_id.type if v.vehicle_type_id else None,
            'brand': v.brand,
            'model': v.model,
            'color': v.color,
            'year': v.year,
            'vehicle_number': v.vehicle_number,
            'status': v.status,
            'insurance_expiry': v.insurance_expiry.isoformat() if v.insurance_expiry else None,
            'permit_expiry': v.permit_expiry.isoformat() if v.permit_expiry else None,
            'fitness_expiry': v.fitness_expiry.isoformat() if v.fitness_expiry else None,
            'puc_expiry': v.puc_expiry.isoformat() if v.puc_expiry else None,
            'is_active_vehicle': bool(driver.active_vehicle_id and driver.active_vehicle_id == v.id),
        })

    # ---- fatigue status (computes live + persists lockout if breached) ----
    fatigue_payload = None
    try:
        from servers.driver.fatigue import get_fatigue_status
        fatigue_payload = get_fatigue_status(driver).to_dict()
    except Exception:
        pass

    # ---- driver sessions (last 20) ----
    sessions_payload = []
    try:
        from servers.driver.models import DriverSession
        for s in DriverSession.objects.filter(driver=driver).order_by('-started_at')[:20]:
            sessions_payload.append({
                'id': s.id,
                'started_at': s.started_at.isoformat() if s.started_at else None,
                'ended_at': s.ended_at.isoformat() if s.ended_at else None,
                'duration_seconds': s.duration_seconds,
                'end_reason': s.end_reason,
            })
    except Exception:
        pass

    # ---- cancellation counts ----
    cancellations_payload = {'last_24h': 0, 'last_7d': 0, 'last_30d': 0}
    try:
        from servers.driver.models import DriverCancellation
        cancellations_payload['last_24h'] = DriverCancellation.objects.filter(
            driver=driver, created_at__gte=last_24h,
        ).count()
        cancellations_payload['last_7d'] = DriverCancellation.objects.filter(
            driver=driver, created_at__gte=last_7d,
        ).count()
        cancellations_payload['last_30d'] = DriverCancellation.objects.filter(
            driver=driver, created_at__gte=last_30d,
        ).count()
    except Exception:
        pass

    # ---- withdrawals (last 10) ----
    withdrawals_payload = []
    try:
        from servers.driver.models import WithdrawalRequest
        for w in WithdrawalRequest.objects.filter(driver=driver).order_by('-requested_at')[:10]:
            withdrawals_payload.append({
                'id': w.id,
                'amount': str(w.amount),
                'status': w.status,
                'payout_method': w.payout_method,
                'requested_at': w.requested_at.isoformat() if w.requested_at else None,
                'processed_at': w.processed_at.isoformat() if w.processed_at else None,
                'payout_status': w.payout_status,
                'failure_count': w.failure_count,
            })
    except Exception:
        pass

    # ---- recent trips (last 20) ----
    recent_trips = []
    try:
        from servers.ride.models import Trip
        for t in Trip.objects.filter(driver_id=driver).select_related('status_id').order_by('-requested_at')[:20]:
            recent_trips.append({
                'id': t.id,
                'status': t.status_id.status_code if t.status_id else None,
                'pickup_address': t.pickup_address,
                'destination_address': t.destination_address,
                'estimated_fare': str(t.estimated_fare) if t.estimated_fare is not None else None,
                'final_fare': str(t.final_fare) if t.final_fare is not None else None,
                'requested_at': t.requested_at.isoformat() if t.requested_at else None,
                'completed_at': t.completed_at.isoformat() if t.completed_at else None,
            })
    except Exception:
        pass

    # ---- earnings summary ----
    earnings_payload = {
        'lifetime': '0.00', 'today': '0.00', 'this_month': '0.00',
        'last_withdrawal_at': driver.last_withdrawal_at.isoformat() if driver.last_withdrawal_at else None,
        'wallet_balance': '0.00',
    }
    try:
        from servers.rider.models import Wallet
        try:
            w = Wallet.objects.get(user_id=user)
            earnings_payload['wallet_balance'] = str(w.balance)
        except Wallet.DoesNotExist:
            pass
    except Exception:
        pass
    try:
        # Driver earnings are recorded as completed trips' final_fare
        # net of platform commission inside the payments flow. For the
        # ops "how much has this driver earned" question, summing
        # completed Trip.final_fare is a reasonable approximation that
        # matches how the dashboard's GMV is computed. The full ledger
        # lives in TransactionHistory + DriverEarning rows (see
        # /driver/earnings/ for the authoritative view).
        from servers.ride.models import Trip
        completed_qs = Trip.objects.filter(
            driver_id=driver, status_id__status_code='completed',
        )
        lifetime = completed_qs.aggregate(s=Sum('final_fare'))['s'] or Decimal('0.00')
        today_amt = completed_qs.filter(completed_at__gte=today_start).aggregate(s=Sum('final_fare'))['s'] or Decimal('0.00')
        month_amt = completed_qs.filter(completed_at__gte=month_start).aggregate(s=Sum('final_fare'))['s'] or Decimal('0.00')
        earnings_payload['lifetime'] = str(lifetime)
        earnings_payload['today'] = str(today_amt)
        earnings_payload['this_month'] = str(month_amt)
    except Exception:
        pass

    payload = {
        'id': driver.id,
        'status': driver.status,
        'approved': driver.approved,
        'ratings': str(driver.ratings),
        'total_trips': driver.total_trips,
        'license_expiry': driver.license_expiry.isoformat() if driver.license_expiry else None,
        'license_doc_url': driver.license_doc.url if driver.license_doc else None,
        'upi_id': driver.upi_id,
        'fatigue_lockout_until': driver.fatigue_lockout_until.isoformat() if driver.fatigue_lockout_until else None,
        'user': {
            'id': user.id if user else None,
            'phone_number': getattr(user, 'phone_number', None) if user else None,
            'full_name': getattr(user, 'full_name', None) if user else None,
            'email': getattr(user, 'email', None) if user else None,
            'is_active': getattr(user, 'is_active', None) if user else None,
        },
        'active_vehicle_id': driver.active_vehicle_id,
        'vehicles': vehicles_payload,
        'fatigue': fatigue_payload,
        'sessions': sessions_payload,
        'cancellations': cancellations_payload,
        'withdrawals': withdrawals_payload,
        'recent_trips': recent_trips,
        'earnings': earnings_payload,
    }
    return success_response(payload, status.HTTP_200_OK)

@api_view(['PATCH'])
@permission_classes([IsAuthenticated, IsAdmin])
def update_kyc_status_admin(request, driver_id):
    """
    Approve or reject KYC for a driver (updates 'approved' and/or 'status').
    """
    from servers.admin_audit.services import record_admin_action

    try:
        driver = Driver.objects.get(id=driver_id)
        before = {
            'approved': driver.approved,
            'status': driver.status,
        }

        serializer = KYCApprovalSerializer(driver, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            driver.refresh_from_db()
            after = {
                'approved': driver.approved,
                'status': driver.status,
            }
            record_admin_action(
                request,
                action='kyc_update',
                target_type='driver',
                target_id=driver.id,
                before=before,
                after=after,
                reason=request.data.get('reason', ''),
            )
            return success_response(serializer.data, status.HTTP_200_OK)

        return error_response(
            code="VALIDATION_ERROR",
            message="Invalid update data",
            field=list(serializer.errors.keys())[0],
            issue=str(serializer.errors),
            status=status.HTTP_400_BAD_REQUEST
        )
    except Driver.DoesNotExist:
        return error_response(
            code="NOT_FOUND",
            message="Driver not found",
            field="driver_id",
            issue=f"No driver matches id {driver_id}",
            status=status.HTTP_404_NOT_FOUND
        )

@api_view(['DELETE'])
@permission_classes([IsAuthenticated, IsAdmin])
def delete_driver_admin(request, driver_id):
    """
    Delete a driver profile and their associated objects.
    """
    from servers.admin_audit.services import record_admin_action

    try:
        driver = Driver.objects.get(id=driver_id)
        snapshot = {
            'approved': driver.approved,
            'status': driver.status,
            'phone': getattr(driver.user_id, 'phone_number', None),
            'full_name': getattr(driver.user_id, 'full_name', None),
        }
        driver.delete()
        record_admin_action(
            request,
            action='driver_deleted',
            target_type='driver',
            target_id=driver_id,
            before=snapshot,
            after={'deleted': True},
            reason=request.data.get('reason', '') if hasattr(request, 'data') else '',
        )
        return success_response({"message": "Driver deleted successfully"}, status.HTTP_200_OK)
    except Driver.DoesNotExist:
        return error_response(
            code="NOT_FOUND",
            message="Driver not found",
            field="driver_id",
            issue=f"No driver matches id {driver_id}",
            status=status.HTTP_404_NOT_FOUND
        )
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def get_vehicle_details(request, driver_id):
    """
    Get details of a vehicle by its ID.
    """
    try:
        vehicle_objects = Vehicle.objects.filter(driver_id=driver_id)
        serializer = VehicleSerializer(vehicle_objects, many=True)
        return success_response(serializer.data, status.HTTP_200_OK)
    except Vehicle.DoesNotExist:
        return error_response(
            code="NOT_FOUND",
            message="Vehicle not found",
            field="vehicle_id",
            issue=f"No vehicle matches id {vehicle_id}",
            status=status.HTTP_404_NOT_FOUND
        )


# ── Admin Withdrawal Management ──────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def list_withdrawals_admin(request):
    """
    GET /api/admin/withdrawals/
    List all withdrawal requests with filtering (status, driver, date range).
    """
    from .models import WithdrawalRequest
    from .serializers import WithdrawalRequestSerializer
    from rest_framework.pagination import PageNumberPagination
    from django.db.models import Q
    from django.utils import timezone
    from datetime import timedelta

    withdrawals = WithdrawalRequest.objects.all().select_related('driver', 'driver__user_id').order_by('-requested_at')

    # Filter by status
    status_filter = request.query_params.get('status')
    if status_filter:
        withdrawals = withdrawals.filter(status=status_filter)

    # Filter by driver ID
    driver_id = request.query_params.get('driver_id')
    if driver_id:
        withdrawals = withdrawals.filter(driver_id=driver_id)

    # Filter by date range (start_date, end_date)
    start_date = request.query_params.get('start_date')
    end_date = request.query_params.get('end_date')
    if start_date:
        try:
            start = timezone.datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            withdrawals = withdrawals.filter(requested_at__gte=start)
        except ValueError:
            pass
    if end_date:
        try:
            end = timezone.datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            withdrawals = withdrawals.filter(requested_at__lte=end)
        except ValueError:
            pass

    paginator = PageNumberPagination()
    paginator.page_size = 10
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = 50
    page = paginator.paginate_queryset(withdrawals, request)
    serializer = WithdrawalRequestSerializer(page, many=True)
    return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def approve_withdrawal_admin(request, withdrawal_id):
    """
    POST /api/admin/withdrawals/{id}/approve/
    Approve request (status: PENDING → APPROVED).
    """
    from .models import WithdrawalRequest
    from django.db import transaction
    from django.utils import timezone
    from servers.admin_audit.services import record_admin_action

    # The whole state-flip-and-payout-dispatch runs inside one atomic
    # block with select_for_update on the WithdrawalRequest row so two
    # admins clicking Approve at the same time cannot both pass the
    # status=='pending' check and both fire trigger_payout_creation
    # (which would double-deduct the driver's wallet).
    try:
        with transaction.atomic():
            try:
                withdrawal = (
                    WithdrawalRequest.objects.select_for_update()
                    .select_related('driver', 'driver__user_id')
                    .get(id=withdrawal_id)
                )
            except WithdrawalRequest.DoesNotExist:
                return error_response(
                    code="NOT_FOUND",
                    message="Withdrawal request not found",
                    field="withdrawal_id",
                    issue=f"No withdrawal matches id {withdrawal_id}",
                    status=status.HTTP_404_NOT_FOUND,
                )

            if withdrawal.status != 'pending':
                return error_response(
                    code="INVALID_STATUS",
                    message="Only pending withdrawals can be approved",
                    field="status",
                    issue=f"Current status is {withdrawal.status}",
                    status=status.HTTP_400_BAD_REQUEST,
                )

            before = {'status': withdrawal.status, 'amount': str(withdrawal.amount)}
            withdrawal.status = 'approved'
            withdrawal.processed_at = timezone.now()
            withdrawal.save(update_fields=['status', 'processed_at'])

            record_admin_action(
                request,
                action='withdrawal_approved',
                target_type='withdrawal_request',
                target_id=withdrawal.id,
                before=before,
                after={'status': 'approved', 'amount': str(withdrawal.amount)},
                reason=request.data.get('reason', '') if hasattr(request, 'data') else '',
            )
    except Exception as e:
        logger.error(f"approve_withdrawal_admin atomic block failed: {e}")
        return error_response(
            code="INTERNAL_ERROR",
            message="Failed to approve withdrawal",
            field="general",
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # Gateway dispatch happens OUTSIDE the atomic block so we don't hold
    # the row lock for the duration of the Cashfree round-trip. The
    # approve-side state flip is already durable; if the payout fails,
    # the gateway hook flips the row to 'failed' and ops can retry.
    try:
        from .services import trigger_payout_creation
        trigger_payout_creation(withdrawal)
    except Exception as e:
        logger.error(
            f"approve_withdrawal_admin: trigger_payout_creation failed for "
            f"withdrawal {withdrawal.id}: {e}"
        )

    from .serializers import WithdrawalRequestSerializer
    serializer = WithdrawalRequestSerializer(withdrawal)
    return success_response(serializer.data, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def reject_withdrawal_admin(request, withdrawal_id):
    """
    POST /api/admin/withdrawals/{id}/reject/
    Reject request with admin notes (status: PENDING → REJECTED).
    """
    from .models import WithdrawalRequest
    from django.db import transaction
    from django.utils import timezone
    from servers.admin_audit.services import record_admin_action

    admin_notes = request.data.get('admin_notes', '')

    try:
        with transaction.atomic():
            try:
                withdrawal = (
                    WithdrawalRequest.objects.select_for_update()
                    .get(id=withdrawal_id)
                )
            except WithdrawalRequest.DoesNotExist:
                return error_response(
                    code="NOT_FOUND",
                    message="Withdrawal request not found",
                    field="withdrawal_id",
                    issue=f"No withdrawal matches id {withdrawal_id}",
                    status=status.HTTP_404_NOT_FOUND,
                )

            if withdrawal.status != 'pending':
                return error_response(
                    code="INVALID_STATUS",
                    message="Only pending withdrawals can be rejected",
                    field="status",
                    issue=f"Current status is {withdrawal.status}",
                    status=status.HTTP_400_BAD_REQUEST,
                )

            before = {'status': withdrawal.status, 'amount': str(withdrawal.amount)}
            withdrawal.status = 'rejected'
            withdrawal.admin_notes = admin_notes
            withdrawal.processed_at = timezone.now()
            withdrawal.save(update_fields=['status', 'admin_notes', 'processed_at'])

            record_admin_action(
                request,
                action='withdrawal_rejected',
                target_type='withdrawal_request',
                target_id=withdrawal.id,
                before=before,
                after={'status': 'rejected', 'admin_notes': admin_notes},
                reason=admin_notes,
            )
    except Exception as e:
        logger.error(f"reject_withdrawal_admin atomic block failed: {e}")
        return error_response(
            code="INTERNAL_ERROR",
            message="Failed to reject withdrawal",
            field="general",
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    from .serializers import WithdrawalRequestSerializer
    serializer = WithdrawalRequestSerializer(withdrawal)
    return success_response(serializer.data, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def bulk_action_withdrawal_admin(request):
    """
    POST /api/admin/withdrawals/bulk-action/
    Bulk approve/reject multiple requests.
    """
    from .models import WithdrawalRequest
    from django.db import transaction
    from django.utils import timezone
    from servers.admin_audit.services import record_admin_action

    # Cap to prevent an admin (or a compromised admin session) from
    # locking the entire withdrawals table or processing the platform's
    # full backlog in a single accidental request.
    BULK_MAX = 50

    action = request.data.get('action')  # 'approve' or 'reject'
    withdrawal_ids = request.data.get('withdrawal_ids', [])
    admin_notes = request.data.get('admin_notes', '')

    if action not in ['approve', 'reject']:
        return error_response(
            code="INVALID_ACTION",
            message="Action must be 'approve' or 'reject'",
            field="action",
            issue=f"Invalid action: {action}",
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not withdrawal_ids:
        return error_response(
            code="EMPTY_LIST",
            message="No withdrawal IDs provided",
            field="withdrawal_ids",
            issue="Provide at least one withdrawal ID",
            status=status.HTTP_400_BAD_REQUEST,
        )

    if len(withdrawal_ids) > BULK_MAX:
        return error_response(
            code="TOO_MANY",
            message=f"At most {BULK_MAX} withdrawals can be processed at once",
            field="withdrawal_ids",
            issue=f"Received {len(withdrawal_ids)} ids; limit is {BULK_MAX}",
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Process each row inside its own atomic + select_for_update so:
    #  - two concurrent bulk calls can't both flip the same row
    #  - a transient failure on one row doesn't roll back the rest
    #  - each audit row is written within the same transaction as its
    #    state flip
    approved_for_payout = []
    updated_ids = []
    skipped_ids = []

    for wid in withdrawal_ids:
        try:
            with transaction.atomic():
                try:
                    w = WithdrawalRequest.objects.select_for_update().get(id=wid)
                except WithdrawalRequest.DoesNotExist:
                    skipped_ids.append(wid)
                    continue

                if w.status != 'pending':
                    skipped_ids.append(wid)
                    continue

                before = {'status': w.status, 'amount': str(w.amount)}
                if action == 'approve':
                    w.status = 'approved'
                    w.processed_at = timezone.now()
                    w.save(update_fields=['status', 'processed_at'])
                    record_admin_action(
                        request,
                        action='withdrawal_approved',
                        target_type='withdrawal_request',
                        target_id=w.id,
                        before=before,
                        after={'status': 'approved', 'amount': str(w.amount)},
                        reason='bulk action',
                    )
                    approved_for_payout.append(w)
                else:
                    w.status = 'rejected'
                    w.admin_notes = admin_notes
                    w.processed_at = timezone.now()
                    w.save(update_fields=['status', 'admin_notes', 'processed_at'])
                    record_admin_action(
                        request,
                        action='withdrawal_rejected',
                        target_type='withdrawal_request',
                        target_id=w.id,
                        before=before,
                        after={'status': 'rejected', 'admin_notes': admin_notes},
                        reason=admin_notes or 'bulk action',
                    )
                updated_ids.append(w.id)
        except Exception as e:
            logger.error(f"bulk_action_withdrawal_admin: row {wid} failed: {e}")
            skipped_ids.append(wid)

    # Payouts dispatch outside the per-row transactions to avoid holding
    # locks through the gateway round-trip.
    if approved_for_payout:
        from .services import trigger_payout_creation
        for w in approved_for_payout:
            try:
                trigger_payout_creation(w)
            except Exception as e:
                logger.error(
                    f"bulk_action_withdrawal_admin: payout dispatch failed "
                    f"for withdrawal {w.id}: {e}"
                )

    return success_response({
        'message': f'Bulk {action} processed',
        'updated_ids': updated_ids,
        'skipped_ids': skipped_ids,
        'updated_count': len(updated_ids),
    }, status.HTTP_200_OK)