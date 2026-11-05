from django.urls import path

from .reveal import reveal_pii
from . import staff_views as staff
from .views import admin_whoami

urlpatterns = [
    # What the caller holds. Used to draw the console; never trusted on the
    # way back in -- every endpoint enforces its own permission and scope.
    path('me/', admin_whoami, name='admin_whoami'),
    # The one door to unmasked personal data: one record, the fields asked
    # for, `pii.view` at the record's node, one audit row per call.
    path('reveal/', reveal_pii, name='admin_reveal_pii'),
    # Staff and roles: Super Admin only (admin.manage at Global); see staff_views.
    path('staff/', staff.staff_list, name='admin_staff_list'),
    path('staff/options/', staff.staff_options, name='admin_staff_options'),
    path('staff/<int:pk>/', staff.staff_detail, name='admin_staff_detail'),
    path('staff/<int:pk>/assignments/', staff.staff_grant, name='admin_staff_grant'),
    path('staff/<int:pk>/deactivate/', staff.staff_deactivate, name='admin_staff_deactivate'),
    path('staff/<int:pk>/reactivate/', staff.staff_reactivate, name='admin_staff_reactivate'),
    path('assignments/<int:pk>/revoke/', staff.assignment_revoke, name='admin_assignment_revoke'),
]
