from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from base.utils import success_response, error_response
from base.permissions import IsAdmin
from django.contrib.auth import get_user_model
from servers.auth_user.serializers import UserModelSerializer
from rest_framework.pagination import PageNumberPagination

user_model = get_user_model()

@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_list_users(request):
    """
    Admin view to list all users with pagination and filtering.
    Filters:
    - role: rider | driver | admin
    - is_active: true | false
    """
    try:
        users = user_model.objects.all().order_by('-date_joined')

        role = request.query_params.get('role')
        if role:
            users = users.filter(role=role)

        is_active = request.query_params.get('is_active')
        if is_active is not None:
            active_bool = str(is_active).lower() == 'true'
            users = users.filter(is_active=active_bool)

        paginator = PageNumberPagination()
        paginator.page_size = 20
        paginator.page_size_query_param = 'page_size'
        paginator.max_page_size = 100
        
        result_page = paginator.paginate_queryset(users, request)
        serializer = UserModelSerializer(result_page, many=True)
        return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)

    except Exception as e:
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
