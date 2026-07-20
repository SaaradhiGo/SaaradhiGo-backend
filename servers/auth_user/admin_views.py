from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from base.utils import success_response, error_response
from base.permissions import IsAdmin
from django.contrib.auth import get_user_model
from servers.auth_user.serializers import UserModelSerializer
from rest_framework.pagination import PageNumberPagination
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

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


@api_view(['POST'])
def admin_login(request):
    """
    Admin login using phone number and password.
    """
    try:
        phone_number = request.data.get('phone_number')
        password = request.data.get('password')

        if not phone_number or not password:
            return error_response(
                code='AUTH_MISSING_CREDENTIALS',
                message='Phone number and password are required',
                field='general',
                issue='Missing credentials',
                status=status.HTTP_400_BAD_REQUEST
            )

        # Format phone number to E.164 if it's not already
        phone_number = str(phone_number).strip()
        if not phone_number.startswith('+'):
            if len(phone_number) == 10:
                phone_number = f'+91{phone_number}'
            elif phone_number.startswith('91') and len(phone_number) == 12:
                phone_number = f'+{phone_number}'

        try:
            user = user_model.objects.get(phone_number=phone_number)
        except user_model.DoesNotExist:
            return error_response(
                code='AUTH_INVALID_CREDENTIALS',
                message='Invalid phone number or password',
                field='general',
                issue='User not found',
                status=status.HTTP_401_UNAUTHORIZED
            )

        if not user.check_password(password):
            return error_response(
                code='AUTH_INVALID_CREDENTIALS',
                message='Invalid phone number or password',
                field='general',
                issue='Incorrect password',
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Verify if the user is an admin
        if user.role != 'admin' and not user.is_staff and not user.is_superuser:
            return error_response(
                code='AUTH_NOT_ADMIN',
                message='User does not have admin privileges',
                field='general',
                issue='Unauthorized access',
                status=status.HTTP_403_FORBIDDEN
            )

        access_token = AccessToken.for_user(user)
        refresh_token = RefreshToken.for_user(user)
        user_serializer = UserModelSerializer(user)

        return success_response(
            data={
                'token': str(access_token),
                'refresh_token': str(refresh_token),
                'user': user_serializer.data
            },
            status_code=status.HTTP_200_OK
        )

    except Exception as e:
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
