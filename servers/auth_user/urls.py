from django.urls import path
from .views import (
    request_otp,
    login as token,
    refresh,
    logout,
    update_user,
    get_user_profile,
)
from .admin_views import admin_list_users

urlpatterns = [
    path('otp/', request_otp),
    path('login/', token),
    path('refresh/', refresh),
    path('logout/', logout),
    path('update/', update_user),
    path('profile/', get_user_profile),
    path('admin/users/', admin_list_users),
]