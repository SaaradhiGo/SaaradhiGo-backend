from django.urls import path

from .views import report_catalogue, report_detail, report_export

urlpatterns = [
    # Under `admin/` so the structural guard finds every route by path.
    path('admin/catalogue/', report_catalogue, name='reports_catalogue'),
    path('admin/<str:report_key>/export/', report_export, name='reports_export'),
    path('admin/<str:report_key>/', report_detail, name='reports_detail'),
]
