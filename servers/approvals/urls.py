from django.urls import path

from .views import (
    approve_request, cancel_request, change_policy, list_policies,
    list_requests, reject_request, request_detail,
)

# Mounted at api/v1/approvals/. The `admin/` segment is what puts every
# route here inside the namespace `tests/test_admin_namespace_is_gated.py`
# walks, so each one must carry a ScopedPermission.
urlpatterns = [
    path('admin/requests/', list_requests, name='approvals_list'),
    path('admin/requests/<int:request_id>/', request_detail, name='approvals_detail'),
    path('admin/requests/<int:request_id>/approve/', approve_request, name='approvals_approve'),
    path('admin/requests/<int:request_id>/reject/', reject_request, name='approvals_reject'),
    path('admin/requests/<int:request_id>/cancel/', cancel_request, name='approvals_cancel'),
    path('admin/policies/', list_policies, name='approvals_policies'),
    path('admin/policies/change/', change_policy, name='approvals_policy_change'),
]
