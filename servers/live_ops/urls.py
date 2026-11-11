from django.urls import path

from .views import heatmap, snapshot, ws_ticket

urlpatterns = [
    # Under `admin/` so the structural guard and the PII sweep find every route by path.
    path('admin/ws-ticket/', ws_ticket, name='live_ops_ws_ticket'),
    path('admin/snapshot/', snapshot, name='live_ops_snapshot'),
    path('admin/heatmap/', heatmap, name='live_ops_heatmap'),
]
