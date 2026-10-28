from django.urls import path

from .views import (
    acknowledge_sos,
    admin_list_sos,
    admin_sos_detail,
    false_alarm_sos,
    raise_sos,
    resolve_sos,
    reveal_sos_contact,
)


urlpatterns = [
    path('', raise_sos),

    # The operator surface, under `admin/` like every other operator API
    # so the structural guard (`tests/test_admin_namespace_is_gated.py`)
    # finds it by path.
    path('admin/', admin_list_sos),
    path('admin/<int:sos_id>/', admin_sos_detail),
    path('admin/<int:sos_id>/acknowledge/', acknowledge_sos),
    path('admin/<int:sos_id>/resolve/', resolve_sos),
    path('admin/<int:sos_id>/false-alarm/', false_alarm_sos),
    path('admin/<int:sos_id>/reveal/', reveal_sos_contact),

    # The paths the console calls today (`/sos/<id>/<action>/`). Same view
    # objects, so the same gate; kept so the deployed console keeps working
    # until it moves to the `admin/` paths above.
    path('<int:sos_id>/acknowledge/', acknowledge_sos),
    path('<int:sos_id>/resolve/', resolve_sos),
    path('<int:sos_id>/false-alarm/', false_alarm_sos),
]
