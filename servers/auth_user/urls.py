from django.urls import path
from .views import (
    request_otp,
    login as token,
    refresh,
    logout,
    update_user,
    get_user_profile,
    update_bank_details,
)
from .admin_views import admin_list_users, admin_login
from .dpdp_views import me_export, me_delete
from .email_auth_views import email_login
from .invite_views import invite_account
from .password_views import (
    change_password,
    confirm_email_verification,
    forgot_password,
    request_email_verification,
    reset_password,
)

urlpatterns = [
    path('otp/', request_otp),
    path('login/', token),
    path('refresh/', refresh),
    path('logout/', logout),
    path('update/', update_user),
    path('profile/', get_user_profile),
    path('bank-details/', update_bank_details),
    path('admin/users/', admin_list_users),
    path('admin/login/', admin_login),
    # DPDP Act 2023 data-subject endpoints.
    path('me/export/', me_export),
    path('me/delete/', me_delete),

    # Email + password. Additive: every path above is unchanged, and
    # phone+OTP remains the primary route for riders and drivers.
    path('email/login/', email_login),
    path('email/verify/request/', request_email_verification),
    path('email/verify/confirm/', confirm_email_verification),
    path('password/change/', change_password),
    path('password/forgot/', forgot_password),
    path('password/reset/', reset_password),

    # Operator-created accounts, including QA test accounts. No password is
    # ever generated or returned -- see invite_views.
    path('admin/invite/', invite_account),
]