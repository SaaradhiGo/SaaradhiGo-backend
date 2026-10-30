from django.urls import path

from servers.support.admin_followups import (
    admin_inbox, admin_inbox_read, admin_macro_restore, admin_macro_versions,
    admin_sla_dashboard,
)
from servers.support.admin_support import (
    admin_country_config_update, admin_assignees, admin_macro_detail, admin_macro_preview, admin_macros, admin_note,
    admin_set_category, admin_set_priority, admin_set_status, admin_sla, admin_sla_update,
)
from servers.support.views import (
    add_user_message,
    admin_assign,
    admin_list_tickets,
    admin_reply,
    admin_ticket_detail,
    close_my_ticket,
    create_ticket,
    list_my_tickets,
    ticket_detail,
)


urlpatterns = [
    # User-side
    path('tickets/', list_my_tickets),
    path('tickets/create/', create_ticket),
    path('tickets/<int:ticket_id>/', ticket_detail),
    path('tickets/<int:ticket_id>/messages/', add_user_message),
    path('tickets/<int:ticket_id>/close/', close_my_ticket),
    # Admin / support-staff
    path('admin/tickets/', admin_list_tickets),
    path('admin/tickets/<int:ticket_id>/', admin_ticket_detail),
    path('admin/tickets/<int:ticket_id>/reply/', admin_reply),
    path('admin/tickets/<int:ticket_id>/assign/', admin_assign),
    path('admin/tickets/<int:ticket_id>/assignees/', admin_assignees),
    path('admin/tickets/<int:ticket_id>/note/', admin_note),
    path('admin/tickets/<int:ticket_id>/status/', admin_set_status),
    path('admin/tickets/<int:ticket_id>/priority/', admin_set_priority),
    path('admin/tickets/<int:ticket_id>/category/', admin_set_category),
    path('admin/macros/', admin_macros),
    path('admin/macros/<int:macro_id>/', admin_macro_detail),
    path('admin/macros/<int:macro_id>/preview/', admin_macro_preview),
    path('admin/macros/<int:macro_id>/versions/', admin_macro_versions),
    path('admin/macros/<int:macro_id>/restore/', admin_macro_restore),
    path('admin/inbox/', admin_inbox),
    path('admin/inbox/read/', admin_inbox_read),
    path('admin/sla/', admin_sla),
    path('admin/sla/dashboard/', admin_sla_dashboard),
    path('admin/sla/config/<str:country>/', admin_country_config_update),
    path('admin/sla/<str:country>/<str:priority>/', admin_sla_update),
]
