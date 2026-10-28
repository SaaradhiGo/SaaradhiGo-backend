from django.urls import path

from .views import acknowledge_alert, admin_list_alerts

urlpatterns = [
    # Under `admin/` so the structural guard finds both by path.
    path('admin/alerts/', admin_list_alerts),
    path('admin/alerts/<int:alert_id>/acknowledge/', acknowledge_alert),
]
