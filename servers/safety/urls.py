from django.urls import path

from . import admin_views as v

# Everything is under `admin/` like every other operator API, so the structural
# guard (`tests/test_admin_namespace_is_gated.py`) finds it by path.
urlpatterns = [
    path('admin/incidents/', v.safety_incidents),
    path('admin/incidents/<int:incident_id>/', v.safety_incident_detail),
    path('admin/incidents/<int:incident_id>/assignees/', v.safety_assignees),
    path('admin/incidents/<int:incident_id>/assign/', v.safety_assign),
    path('admin/incidents/<int:incident_id>/note/', v.safety_note),
    path('admin/incidents/<int:incident_id>/status/', v.safety_set_status),
    path('admin/incidents/<int:incident_id>/link/', v.safety_link),
    path('admin/incidents/<int:incident_id>/unlink/', v.safety_unlink),
    path('admin/incidents/<int:incident_id>/suspend-driver/', v.safety_suspend_driver),
    path('admin/incidents/<int:incident_id>/block-rider/', v.safety_block_rider),
    path('admin/incidents/<int:incident_id>/refer-police/', v.safety_refer_police),
    path('admin/incidents/<int:incident_id>/close/', v.safety_close),
    path('admin/tickets/<int:ticket_id>/escalate/', v.safety_escalate_ticket),
    path('admin/signals/', v.safety_signals),
    path('admin/signals/<int:signal_id>/dismiss/', v.safety_dismiss_signal),
    path('admin/config/', v.safety_config),
    path('admin/config/<str:country>/', v.safety_config_update),
]
