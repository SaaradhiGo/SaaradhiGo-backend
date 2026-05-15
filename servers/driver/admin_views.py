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