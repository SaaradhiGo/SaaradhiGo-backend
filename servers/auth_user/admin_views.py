from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from base.utils import success_response, error_response
from base.permissions import IsAdmin, is_operator
from servers.admin_dashboard import login_guard
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
    """Password sign-in for an operator, issuing a JWT pair.

    THREE DEFECTS THIS VIEW HAD
    ---------------------------
    **It was completely unthrottled.** No `throttle_classes`, and the project sets
    `DEFAULT_THROTTLE_RATES` without `DEFAULT_THROTTLE_CLASSES`, so nothing applied.
    The Django console form at `/login/` was measured at 12 failed logins in 7.8
    seconds before `login_guard` was added; this endpoint had exactly that exposure
    and no guard at all -- an unmetered password oracle for the accounts that
    approve KYC and release payouts. It now shares `login_guard` with the console,
    so the two operator sign-in surfaces cannot have different brute-force
    resistance.

    **Its authorization gate was the weakest of four.** It refused only when
    `role != 'admin' AND not is_staff AND not is_superuser` -- an "any of three"
    gate, so `is_staff` alone was enough. A rider carrying `is_staff` could mint an
    operator token here while `IsAdmin` refused them everywhere else. Now
    `is_operator`, which is the one definition.

    **It enumerated users.** An unknown phone returned `issue='User not found'` and
    a wrong password returned `issue='Incorrect password'` -- two distinguishable
    401s, which is a clean oracle for which numbers hold accounts. A third distinct
    403 (`AUTH_NOT_ADMIN`) then told an attacker which of those accounts are
    operators. All three are now one identical refusal.
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

        # One refusal for every failure below: unknown phone, wrong password,
        # correct password on a non-operator account, and locked out. Anything
        # that distinguishes them is an oracle.
        def _refuse():
            return error_response(
                code='AUTH_INVALID_CREDENTIALS',
                message='Invalid phone number or password',
                field='general',
                issue='Invalid credentials',
                status=status.HTTP_401_UNAUTHORIZED
            )

        if login_guard.is_locked(request, phone_number):
            return _refuse()

        try:
            user = user_model.objects.get(phone_number=phone_number)
        except user_model.DoesNotExist:
            login_guard.record_failure(request, phone_number)
            return _refuse()

        if not user.check_password(password):
            login_guard.record_failure(request, phone_number)
            return _refuse()

        # A CORRECT password on a non-operator account still counts as a failure.
        # Otherwise every rider and driver account is an unthrottled oracle for
        # password guessing -- the attacker simply aims at a non-operator.
        if not is_operator(user):
            login_guard.record_failure(request, phone_number)
            return _refuse()

        login_guard.clear(request, phone_number)
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
