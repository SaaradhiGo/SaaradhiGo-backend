import logging
import re
from rest_framework import status
from rest_framework.decorators import (
    api_view, permission_classes, parser_classes, throttle_classes,
)
from base.utils import success_response, error_response, generate_otp, send_otp_via_sns
from django.conf import settings
from base.throttles import (
    OtpRequestThrottle, OtpRequestBurstThrottle, OtpVerifyThrottle,
)
from django.core.cache import cache
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from .serializers import UserModelSerializer
from django.db import transaction, IntegrityError
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from servers.rider.models import Rider
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from servers.driver.models import Driver
from base.media import resolve_file_input

logger = logging.getLogger(__name__)
user_model = get_user_model()

# Valid roles for user accounts
VALID_ROLES = ['rider', 'driver', 'admin']
PHONE_REGEX = re.compile(r'^\+?1?\d{9,15}$')
OTP_EXPIRY = 600  # 10 minutes
MAX_OTP_ATTEMPTS = 5


def _validate_phone_number(phone_number):
    """
    Validate phone number format.
    
    Args:
        phone_number: Phone number string
    
    Returns:
        tuple: (is_valid, error_message)
    """
    if not phone_number or not isinstance(phone_number, str):
        return False, "Phone number must be a non-empty string"
    
    if not PHONE_REGEX.match(phone_number):
        return False, "Invalid phone number format. Use E.164 format (e.g., +919876543210)"
    
    return True, None


@api_view(['POST'])
@throttle_classes([OtpRequestBurstThrottle, OtpRequestThrottle])
def request_otp(request):
    """
    Request OTP for authentication.

    Rate limits (per phone_number, configured in settings.REST_FRAMEWORK):
      - 1 request per 30 seconds (anti-spam burst)
      - 5 requests per hour (sustained cap)
    Without these, an attacker could repeatedly request OTPs to either
    exhaust the SNS budget or reset the verify-attempt counter.
    
    Expected request data:
    {
        "phone_number": str (E.164 format, required),
        "role": str (optional, default: "rider")
    }
    """
    try:
        phone_number = request.data.get('phone_number', None)
        role = request.data.get('role', 'rider')
        
        # Validate phone number
        if phone_number is None:
            logger.warning("OTP request without phone number")
            return error_response(
                code="AUTH_MISSING_DETAILS",
                message='Phone number is required',
                field='phone_number',
                issue='Phone number is mandatory',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        is_valid, error_msg = _validate_phone_number(phone_number)
        if not is_valid:
            logger.warning(f"Invalid phone number format: {phone_number[:5]}***")
            return error_response(
                code="AUTH_INVALID_PHONE",
                message=error_msg,
                field='phone_number',
                issue='Invalid phone number format',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate role
        if role not in VALID_ROLES:
            logger.warning(f"Invalid role requested: {role}")
            return error_response(
                code="AUTH_INVALID_ROLE",
                message=f'Role must be one of {VALID_ROLES}',
                field='role',
                issue='Invalid role specified',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Test-phone bypass: if this phone is configured in
        # settings.TEST_PHONE_NUMBERS, use the operator-supplied fixed OTP
        # and skip the AWS SNS round-trip. The login path is unchanged —
        # the cache is populated identically — so attempt counters,
        # role binding, expiry, and post-login flows all still work.
        # Empty TEST_PHONE_NUMBERS (the production default) = no bypass.
        test_otp = settings.TEST_PHONE_NUMBERS.get(phone_number)

        if test_otp:
            otp = test_otp
            task_id = 'test-phone-no-sms'
            logger.info(
                f"OTP test-phone bypass for {phone_number[:5]}*** — "
                f"no SMS sent"
            )
        else:
            # Generate OTP
            otp = generate_otp(6)
            if not otp:
                logger.error("Failed to generate OTP")
                return error_response(
                    code="AUTH_OTP_GENERATION_FAILED",
                    message='Failed to generate OTP',
                    field='otp',
                    issue='OTP generation error',
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        # Store in cache (test phones and real phones share this path)
        cache.set(
            f'otp_role_{phone_number}',
            {'otp': otp, 'role': role, 'attempts': 0},
            OTP_EXPIRY
        )

        if not test_otp:
            try:
                # Send OTP via SNS (async task)
                task_id = send_otp_via_sns.delay(
                    phone_number,
                    f"Your OTP for VahanGo is {otp}. It will expire in 10 minutes."
                )
                logger.info(f"OTP sent to {phone_number[:5]}***, task_id: {task_id}")
            except Exception as e:
                logger.error(f"Failed to queue OTP send task: {str(e)}")
                return error_response(
                    code="AUTH_OTP_REQUEST",
                    message="Unable to send OTP at this moment",
                    field="otp",
                    issue="SMS gateway error",
                    status=status.HTTP_503_SERVICE_UNAVAILABLE
                )

        return success_response(
            data={
                'message': "OTP sent successfully",
                'task_id': str(task_id),
                'expires_in': OTP_EXPIRY,
            },
            status_code=status.HTTP_200_OK,
        )
    
    except Exception as e:
        logger.error(f"Unexpected error in request_otp: {str(e)}")
        return error_response(
            code="AUTH_INTERNAL_ERROR",
            message="An unexpected error occurred",
            field="general",
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
@api_view(['POST'])
@throttle_classes([OtpVerifyThrottle])
def login(request):
    """
    Authenticate user with OTP.

    Rate limit (per phone_number): 10 verify attempts per hour. The
    existing per-OTP attempt counter (MAX_OTP_ATTEMPTS) only caps brute
    force against a single issued OTP; this throttle bounds the attack
    across multiple OTP issues.
    
    Expected request data:
    {
        "phone_number": str (E.164 format, required),
        "otp": str (required),
        "device_token": str (optional)
    }

    Note: an optional `password` field used to be accepted on the very
    first login for a phone and silently `set_password()`-ed onto the
    user. That dual-factor was never actually enforced anywhere later
    (no password-login flow exists), it could be used to clobber a
    chosen password by an attacker who briefly knew an OTP, and it had
    no rotation path. The field is now ignored.
    """
    try:
        # Validate required fields first
        phone_number = request.data.get('phone_number', None)
        otp = request.data.get('otp', None)
        device_token = request.data.get('device_token', None)
        
        if phone_number is None:
            logger.warning("Login attempt without phone number")
            return error_response(
                code='AUTH_PHONE_REQUIRED',
                message='Phone number is required',
                field='phone_number',
                issue='Missing required field',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if otp is None:
            logger.warning(f"Login attempt without OTP for phone: {phone_number[:5]}***")
            return error_response(
                code='AUTH_OTP_REQUIRED',
                message='OTP is required',
                field='otp',
                issue='Missing required field',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get OTP data from cache
        cache_key = f'otp_role_{phone_number}'
        cache_data = cache.get(cache_key, {})
        role = cache_data.get('role', 'rider')
        sent_otp = cache_data.get('otp', None)
        attempts = cache_data.get('attempts', 0)
        
        # Check if OTP has expired
        if sent_otp is None:
            logger.warning(f"OTP expired or not found for: {phone_number[:5]}***")
            return error_response(
                code='AUTH_OTP_EXPIRED',
                message='OTP has expired',
                field='otp',
                issue='OTP not found or expired',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check attempt limit
        if attempts >= MAX_OTP_ATTEMPTS:
            logger.warning(f"Too many login attempts for: {phone_number[:5]}***")
            return error_response(
                code='AUTH_TOO_MANY_ATTEMPTS',
                message="Too many failed attempts. Please request a new OTP.",
                field='otp',
                issue='Maximum login attempts exceeded',
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
        
        # Verify OTP
        if otp != sent_otp:
            cache_data['attempts'] = attempts + 1
            cache.set(cache_key, cache_data, OTP_EXPIRY)
            logger.warning(f"Invalid OTP attempt ({attempts + 1}/{MAX_OTP_ATTEMPTS}) for: {phone_number[:5]}***")
            return error_response(
                code="AUTH_INVALID_OTP",
                message='OTP is incorrect',
                field='otp',
                issue='Provided OTP does not match',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Clear OTP from cache after successful verification
        cache.delete(cache_key)
        
        try:
            with transaction.atomic():
                try:
                    user = user_model.objects.get(phone_number=phone_number)
                    # If existing user logs in as a different role, we might want to update it or reject, but let's keep it simple
                    created = False
                except user_model.DoesNotExist:
                    # Password not accepted — see docstring.
                    user = user_model.objects.create_user(
                        phone_number=phone_number,
                        role=role,
                    )
                    created = True
                
                if created:
                    # Set up new user
                    logger.info(f"New user created with phone: {phone_number[:5]}***")
                    
                    # Create user profile based on role
                    if role == 'rider':
                        rider = Rider.objects.create(user_id=user)
                        rider.save()
                        logger.info(f"Rider profile created for user: {user.id}")
                    elif role == 'driver':
                        driver_profile = Driver.objects.create(user_id=user)
                        driver_profile.save()
                        logger.info(f"Driver profile created for user: {user.id}")
                    elif role == 'admin':
                        logger.info(f"Admin user created: {user.id}")
                    else:
                        logger.error(f"Invalid role during user creation: {role}")
                        raise ValueError(f"Invalid role: {role}")
                else:
                    logger.info(f"Existing user authenticated: {phone_number[:5]}***")
        
        except IntegrityError as e:
            logger.error(f"IntegrityError during login: {str(e)}")
            return error_response(
                code='AUTH_PROFILE_ERROR',
                message='User profile creation failed',
                field='profile',
                issue=f'Database integrity error: {str(e)}',
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        except Exception as e:
            logger.error(f"Error during user creation or profile setup: {str(e)}")
            # Try to retrieve existing user
            try:
                user = user_model.objects.get(phone_number=phone_number)
                logger.info(f"Retrieved existing user after error: {phone_number[:5]}***")
            except user_model.DoesNotExist:
                logger.error(f"User does not exist after failed creation: {phone_number[:5]}***")
                return error_response(
                    code='AUTH_USER_NOT_FOUND',
                    message='User could not be retrieved',
                    field='user',
                    issue='User not found in database',
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        
        # Generate tokens
        try:
            user.fcm_token=device_token
            user.save()
            access_token = AccessToken.for_user(user)
            refresh_token = RefreshToken.for_user(user)
        except Exception as e:
            logger.error(f"Error generating tokens for user {user.id}: {str(e)}")
            return error_response(
                code='AUTH_TOKEN_GENERATION_FAILED',
                message='Failed to generate authentication tokens',
                field='tokens',
                issue='Token generation error',
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        # Serialize user data
        user_serializer = UserModelSerializer(user)
        
        logger.info(f"Login successful for: {phone_number[:5]}***")
        return success_response(
            data={
                'token': str(access_token),
                'refresh_token': str(refresh_token),
                'user': user_serializer.data
            },
            status_code=status.HTTP_200_OK
        )
    
    except Exception as e:
        logger.error(f"Unexpected error in login: {str(e)}")
        return error_response(
            code='AUTH_INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
@api_view(['POST'])
def refresh(request):
    """
    Refresh access token using refresh token.
    
    Expected request data:
    {
        "refresh_token": str (required)
    }
    """
    try:
        refresh_token = request.data.get('refresh_token', None)
        
        if refresh_token is None:
            logger.warning("Token refresh attempt without refresh token")
            return error_response(
                code='AUTH_REFRESH_TOKEN_MISSING',
                message='Refresh token is required',
                field='refresh_token',
                issue='Refresh token not provided',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            old_refresh = RefreshToken(refresh_token)
            # Capture the user id BEFORE blacklisting (the blacklist call
            # may invalidate the in-memory object).
            user_id = old_refresh['user_id']

            # Rotation: blacklist the just-used refresh token so it cannot
            # be redeemed twice. Combined with ROTATE_REFRESH_TOKENS +
            # BLACKLIST_AFTER_ROTATION in settings, this makes a stolen
            # refresh token single-use.
            try:
                old_refresh.blacklist()
            except AttributeError:
                # token_blacklist app not installed — log loudly so we
                # notice the rotation isn't actually in effect.
                logger.error(
                    "Refresh-token rotation requested but token_blacklist "
                    "is not installed."
                )

            # Mint a fresh refresh + access token pair for the same user.
            user = user_model.objects.get(pk=user_id)
            new_refresh_obj = RefreshToken.for_user(user)
            new_refresh_token = str(new_refresh_obj)
            new_access_token = str(new_refresh_obj.access_token)

            logger.info(f"Token refreshed and rotated for user {user_id}")
            return success_response(
                data={
                    'token': new_access_token,
                    'refresh_token': new_refresh_token,
                },
                status_code=status.HTTP_200_OK
            )
        
        except InvalidToken as e:
            logger.warning(f"Invalid refresh token: {str(e)}")
            return error_response(
                code='AUTH_INVALID_REFRESH_TOKEN',
                message='Refresh token is invalid',
                field='refresh_token',
                issue='The provided refresh token is invalid or malformed',
                status=status.HTTP_400_BAD_REQUEST
            )
        except TokenError as e:
            logger.warning(f"Token error during refresh: {str(e)}")
            return error_response(
                code='AUTH_TOKEN_ERROR',
                message='Token processing error',
                field='refresh_token',
                issue='An error occurred while processing the token',
                status=status.HTTP_400_BAD_REQUEST
            )
    
    except Exception as e:
        logger.error(f"Unexpected error in refresh: {str(e)}")
        return error_response(
            code='AUTH_INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    """Log out the caller by blacklisting their refresh token.

    Expected request data:
        { "refresh_token": str (required) }

    Why a logout endpoint exists at all when tokens have a TTL:
      - The access token will expire on its own in 15 min, but the
        refresh token has a 14-day life. Without a logout-side
        blacklist, a user who taps "log out" can have their stolen
        refresh token still produce new access tokens for two weeks.
      - Pairs with ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION
        in settings to make stolen refresh tokens both single-use AND
        explicitly revocable on logout.
    """
    refresh_token = request.data.get('refresh_token')
    if not refresh_token:
        return error_response(
            code='AUTH_REFRESH_TOKEN_MISSING',
            message='Refresh token is required',
            field='refresh_token',
            issue='Provide the refresh token to blacklist',
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        token = RefreshToken(refresh_token)
        # The endpoint is authenticated, so we know who is calling. Guard
        # against a malicious caller trying to log someone else out.
        token_user_id = token.get('user_id')
        if token_user_id is not None and str(token_user_id) != str(request.user.id):
            logger.warning(
                f"Logout attempt with mismatched user_id "
                f"(caller={request.user.id}, token={token_user_id})"
            )
            return error_response(
                code='AUTH_TOKEN_MISMATCH',
                message='Refresh token does not belong to this user',
                field='refresh_token',
                issue='Authenticated user does not own the supplied refresh token',
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            token.blacklist()
        except AttributeError:
            logger.error(
                "Logout requested but token_blacklist app is not installed; "
                "refresh token cannot be revoked."
            )
            return error_response(
                code='AUTH_BLACKLIST_UNAVAILABLE',
                message='Token revocation is currently unavailable',
                field='general',
                issue='token_blacklist app not configured',
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        logger.info(f"User {request.user.id} logged out (refresh blacklisted)")
        return success_response(
            data={'message': 'Logged out'},
            status_code=status.HTTP_200_OK,
        )
    except (InvalidToken, TokenError) as e:
        logger.warning(f"Invalid refresh token at logout: {e}")
        return error_response(
            code='AUTH_INVALID_REFRESH_TOKEN',
            message='Refresh token is invalid',
            field='refresh_token',
            issue='The provided refresh token is invalid or already revoked',
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error(f"Unexpected error in logout: {e}")
        return error_response(
            code='AUTH_INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def update_user(request):
    """
    Update authenticated user information.
    
    Supports both JSON and multipart form data.
    File uploads are delegated to Django storage.
    
    Expected request data (any/all fields are optional):
    {
        "full_name": str,
        "email": str,
        "gender": str,
        "dob": str (YYYY-MM-DD),
        "house_no": str,
        "street": str,
        "city": str,
        "zip_code": str,
        "emergency_contact": str,
        "avatar": file or null,
        "phone_number": str
    }
    """
    try:
        user_id = request.user.id
        
        # Retrieve user
        try:
            user = user_model.objects.get(id=user_id)
        except user_model.DoesNotExist:
            logger.error(f"User not found during update: {user_id}")
            return error_response(
                code='AUTH_USER_NOT_FOUND',
                message='User not found',
                field='user',
                issue='The authenticated user does not exist',
                status=status.HTTP_404_NOT_FOUND
            )
        
        update_data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
        avatar_provided, avatar_value, avatar_error = resolve_file_input(request, 'avatar')
        if avatar_error:
            return error_response(
                code='UPLOAD_FAILED',
                message=avatar_error,
                field='avatar',
                issue=avatar_error,
                status=status.HTTP_400_BAD_REQUEST
            )
        if avatar_provided:
            update_data['avatar'] = avatar_value
        
        if not update_data:
            logger.warning(f"Update request with no data for user: {user_id}")
            return error_response(
                code='AUTH_NO_UPDATE_DATA',
                message='No update data provided',
                field='data',
                issue='Request body is empty',
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            user_serializer = UserModelSerializer(
                user,
                data=update_data,
                partial=True
            )
            
            if not user_serializer.is_valid():
                logger.warning(f"Validation errors during user update: {user_serializer.errors}")
                return error_response(
                    code='AUTH_VALIDATION_ERROR',
                    message='Invalid update data',
                    field='user_data',
                    issue=str(user_serializer.errors),
                    status=status.HTTP_400_BAD_REQUEST
                )

            user_serializer.save()
            logger.info(f"User profile updated successfully: {user_id}")
            return success_response(
                user_serializer.data,
                status.HTTP_200_OK
            )
        
        except Exception as e:
            logger.error(f"Error during user data update: {str(e)}")
            return error_response(
                code='AUTH_UPDATE_ERROR',
                message='Failed to update user information',
                field='user_data',
                issue=str(e),
                status=status.HTTP_400_BAD_REQUEST
            )
    
    except Exception as e:
        logger.error(f"Unexpected error in update_user: {str(e)}")
        return error_response(
            code='AUTH_INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_user_profile(request):
    """
    Get authenticated user information.
    """
    try:
        user_id = request.user.id
        
        # Retrieve user
        try:
            user = user_model.objects.get(id=user_id)
        except user_model.DoesNotExist:
            logger.error(f"User not found during get profile: {user_id}")
            return error_response(
                code='AUTH_USER_NOT_FOUND',
                message='User not found',
                field='user',
                issue='The authenticated user does not exist',
                status=status.HTTP_404_NOT_FOUND
            )
            
        user_serializer = UserModelSerializer(user)
        return success_response(
            user_serializer.data,
            status.HTTP_200_OK
        )
    except Exception as e:
        logger.error(f"Unexpected error in get_user_profile: {str(e)}")
        return error_response(
            code='AUTH_INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
