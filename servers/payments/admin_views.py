from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from base.utils import success_response, error_response
from servers.payments.models import Payment, TransactionHistory
from servers.payments.serializers import PaymentSerializer, TransactionHistorySerializer
from servers.driver.permissions import IsAdmin
from rest_framework.pagination import PageNumberPagination

@api_view(['GET'])
@permission_classes([IsAdmin])
def admin_list_payments(request):
    """
    Admin view to list all payments.
    Filters:
    - status
    - method
    """
    try:
        payments = Payment.objects.all().order_by('-created_at')

        status_filter = request.query_params.get('status')
        if status_filter:
            payments = payments.filter(status=status_filter)

        method = request.query_params.get('method')
        if method:
            payments = payments.filter(method=method)

        paginator = PageNumberPagination()
        paginator.page_size = 20
        paginator.page_size_query_param = 'page_size'
        paginator.max_page_size = 100

        result_page = paginator.paginate_queryset(payments, request)
        serializer = PaymentSerializer(result_page, many=True)
        return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)
    except Exception as e:
        return error_response(code='INTERNAL_ERROR', message='An error occurred', field='general', issue=str(e), status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAdmin])
def admin_list_transactions(request):
    """
    Admin view to list all transaction history logs.
    Filters:
    - status
    """
    try:
        transactions = TransactionHistory.objects.all().order_by('-created_at')
        
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
