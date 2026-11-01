from django.urls import path

from . import views

# Mounted at api/v1/admin-audit/. Under `admin/` so the structural guard finds every route.
# `entries/export/` is listed before `entries/<id>/` so it is not read as an id.
urlpatterns = [
    path('admin/entries/', views.entry_list, name='audit_entries'),
    path('admin/entries/export/', views.entry_export, name='audit_export'),
    path('admin/entries/<int:entry_id>/', views.entry_detail, name='audit_entry'),
]
