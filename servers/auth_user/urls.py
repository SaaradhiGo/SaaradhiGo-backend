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
]