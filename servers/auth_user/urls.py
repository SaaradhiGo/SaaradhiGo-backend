from django.conf import settings
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
from .invite_views import invite_operator, invite_test_driver, invite_test_rider
from .mfa_views import mfa_status, regenerate_recovery_codes
from .password_views import (
    change_password,
    confirm_email_verification,
    forgot_password,
    request_email_verification,
    reset_password,
)

from .registration_views import register_with_email

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
    path('email/register/', register_with_email),
    path('email/login/', email_login),
    path('email/verify/request/', request_email_verification),
    path('email/verify/confirm/', confirm_email_verification),
    path('password/change/', change_password),
    path('password/forgot/', forgot_password),
    path('password/reset/', reset_password),

    # Operator invites: `admin.manage` plus a fresh second factor. No
    # password is ever generated or returned -- see invite_views.
    path('admin/invite/', invite_operator),

    # The signed-in operator's OWN second factor: status, and a fresh set
    # of recovery codes against a live authenticator code. Self-service,
    # so gated like `admin-access/me/` (token + is_operator inside) rather
    # than on a capability -- see mfa_views and the allowlist in
    # tests/test_admin_namespace_is_gated.py.
    path('admin/mfa/status/', mfa_status),
    path('admin/mfa/recovery-codes/regenerate/', regenerate_recovery_codes),
]


def qa_only_urlpatterns():
    """Routes that exist OUTSIDE production only.

    Rider and driver TEST-account creation (owner's ruling, 2026-10-28):
    the endpoint does not exist in production builds, and on QA needs
    `rider.write` / `driver.write`. Decided at URLconf build time from
    `settings.IS_PRODUCTION`, so in production the URL does not resolve at
    all -- there is nothing to misconfigure or to gate. The views refuse
    with 404 as well, as a second lock. `tests/test_production_boot_guards`
    resolves these paths in a production-shaped boot and expects
    Resolver404.
    """
    if getattr(settings, 'IS_PRODUCTION', False):
        return []
    return [
        path('admin/invite/test-rider/', invite_test_rider),
        path('admin/invite/test-driver/', invite_test_driver),
    ]


urlpatterns += qa_only_urlpatterns()