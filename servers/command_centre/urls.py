from django.urls import path

from .views import (
    acknowledge_alert, admin_list_alerts, admin_overview, change_threshold,
    list_thresholds,
)

urlpatterns = [
    # Under `admin/` so the structural guard finds every route by path.
    path('admin/overview/', admin_overview, name='command_centre_overview'),
    path('admin/alerts/', admin_list_alerts, name='command_centre_alerts'),
    path('admin/alerts/<int:alert_id>/acknowledge/', acknowledge_alert,
         name='command_centre_alert_acknowledge'),
    path('admin/thresholds/', list_thresholds, name='command_centre_thresholds'),
    path('admin/thresholds/change/', change_threshold, name='command_centre_threshold_change'),
]
