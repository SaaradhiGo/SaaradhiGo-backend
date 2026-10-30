from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from base.utils import success_response, error_response
from base.permissions import IsAdmin
from servers.admin_access.bridge import ScopedPermission
from servers.admin_access.scoping import ScopeNotHeld, requested_node
from servers.payments.scope import money_visible_to
from servers.payments.models import TransactionHistory
from servers.payments.serializers import TransactionHistorySerializer
from rest_framework.pagination import PageNumberPagination

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def admin_list_transactions(request):
    """
    Admin view to list all transaction history logs.
    Filters:
    - status
    """
    try:
        # One money-scope rule (servers/payments/scope.py): the driver's home
        # city, narrowed by ?scope= and to where finance.read is actually held.
        try:
            node = requested_node(request)
        except ScopeNotHeld as exc:
            return error_response(
                code='SCOPE_NOT_HELD', message=str(exc), field='scope',
                issue='Requested scope is outside your assignments',
                status=status.HTTP_403_FORBIDDEN)
        transactions = money_visible_to(
            request.user, TransactionHistory, node=node, permission='finance.read',
        ).order_by('-created_at')
        
        status_filter = request.query_params.get('status')
        if status_filter:
            transactions = transactions.filter(status=status_filter)
            
        paginator = PageNumberPagination()
        paginator.page_size = 20
        paginator.page_size_query_param = 'page_size'
        paginator.max_page_size = 100

        result_page = paginator.paginate_queryset(transactions, request)
        serializer = TransactionHistorySerializer(result_page, many=True)
        return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)
    except Exception as e:
        return error_response(code='INTERNAL_ERROR', message='An error occurred', field='general', issue=str(e), status=status.HTTP_500_INTERNAL_SERVER_ERROR)
