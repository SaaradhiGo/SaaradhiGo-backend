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
    try:
        driver = Driver.objects.get(id=driver_id)
        
        serializer = KYCApprovalSerializer(driver, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
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
    try:
        driver = Driver.objects.get(id=driver_id)
        driver.delete()
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
    from django.utils import timezone

    try:
        withdrawal = WithdrawalRequest.objects.get(id=withdrawal_id)
    except WithdrawalRequest.DoesNotExist:
        return error_response(
            code="NOT_FOUND",
            message="Withdrawal request not found",
            field="withdrawal_id",
            issue=f"No withdrawal matches id {withdrawal_id}",
            status=status.HTTP_404_NOT_FOUND
        )

    if withdrawal.status != 'pending':
        return error_response(
            code="INVALID_STATUS",
            message="Only pending withdrawals can be approved",
            field="status",
            issue=f"Current status is {withdrawal.status}",
            status=status.HTTP_400_BAD_REQUEST
        )

    withdrawal.status = 'approved'
    withdrawal.processed_at = timezone.now()
    withdrawal.save()

    # Trigger payout creation
    from .services import trigger_payout_creation
    trigger_payout_creation(withdrawal)

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
    from django.utils import timezone

    try:
        withdrawal = WithdrawalRequest.objects.get(id=withdrawal_id)
    except WithdrawalRequest.DoesNotExist:
        return error_response(
            code="NOT_FOUND",
            message="Withdrawal request not found",
            field="withdrawal_id",
            issue=f"No withdrawal matches id {withdrawal_id}",
            status=status.HTTP_404_NOT_FOUND
        )

    if withdrawal.status != 'pending':
        return error_response(
            code="INVALID_STATUS",
            message="Only pending withdrawals can be rejected",
            field="status",
            issue=f"Current status is {withdrawal.status}",
            status=status.HTTP_400_BAD_REQUEST
        )

    admin_notes = request.data.get('admin_notes', '')
    withdrawal.status = 'rejected'
    withdrawal.admin_notes = admin_notes
    withdrawal.processed_at = timezone.now()
    withdrawal.save()

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
    from django.utils import timezone

    action = request.data.get('action')  # 'approve' or 'reject'
    withdrawal_ids = request.data.get('withdrawal_ids', [])

    if action not in ['approve', 'reject']:
        return error_response(
            code="INVALID_ACTION",
            message="Action must be 'approve' or 'reject'",
            field="action",
            issue=f"Invalid action: {action}",
            status=status.HTTP_400_BAD_REQUEST
        )

    if not withdrawal_ids:
        return error_response(
            code="EMPTY_LIST",
            message="No withdrawal IDs provided",
            field="withdrawal_ids",
            issue="Provide at least one withdrawal ID",
            status=status.HTTP_400_BAD_REQUEST
        )

    withdrawals = WithdrawalRequest.objects.filter(id__in=withdrawal_ids, status='pending')
    if not withdrawals.exists():
        return error_response(
            code="NO_PENDING",
            message="No pending withdrawals found for the given IDs",
            field="withdrawal_ids",
            issue="All withdrawals are already processed or do not exist",
            status=status.HTTP_400_BAD_REQUEST
        )

    updated_count = 0
    for withdrawal in withdrawals:
        if action == 'approve':
            withdrawal.status = 'approved'
            withdrawal.processed_at = timezone.now()
            withdrawal.save()
            # Trigger payout creation for approved withdrawals
            from .services import trigger_payout_creation
            trigger_payout_creation(withdrawal)
        else:
            withdrawal.status = 'rejected'
            withdrawal.admin_notes = request.data.get('admin_notes', '')
            withdrawal.processed_at = timezone.now()
            withdrawal.save()
        updated_count += 1

    return success_response({
        'message': f'Successfully {action}d {updated_count} withdrawal(s)',
        'updated_count': updated_count,
    }, status.HTTP_200_OK)