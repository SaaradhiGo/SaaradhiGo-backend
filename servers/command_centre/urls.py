from django.urls import path

from .views import (
    acknowledge_alert, admin_list_alerts, admin_needs_action, admin_overview, admin_places,
    admin_pulse, admin_viewing, change_threshold, list_thresholds,
)

urlpatterns = [
    # Under `admin/` so the structural guard finds every route by path.
    path('admin/overview/', admin_overview, name='command_centre_overview'),
    path('admin/alerts/', admin_list_alerts, name='command_centre_alerts'),
    path('admin/alerts/<int:alert_id>/acknowledge/', acknowledge_alert,
         name='command_centre_alert_acknowledge'),
    path('admin/thresholds/', list_thresholds, name='command_centre_thresholds'),
    path('admin/thresholds/change/', change_threshold, name='command_centre_threshold_change'),
    path('admin/places/', admin_places, name='command_centre_places'),
    path('admin/needs-action/', admin_needs_action, name='command_centre_needs_action'),
    path('admin/pulse/', admin_pulse, name='command_centre_pulse'),
    path('admin/viewing/', admin_viewing, name='command_centre_viewing'),
]
