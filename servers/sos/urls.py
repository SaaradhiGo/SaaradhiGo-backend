from django.urls import path

from .views import (
    raise_sos, acknowledge_sos, resolve_sos, false_alarm_sos, admin_list_sos,
)


urlpatterns = [
    path('', raise_sos),
    path('admin/', admin_list_sos),
    path('<int:sos_id>/acknowledge/', acknowledge_sos),
    path('<int:sos_id>/resolve/', resolve_sos),
    path('<int:sos_id>/false-alarm/', false_alarm_sos),
]
