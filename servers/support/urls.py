from django.urls import path

from servers.support.views import (
    add_user_message,
    admin_assign,
    admin_list_tickets,
    admin_reply,
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
    path('admin/tickets/<int:ticket_id>/reply/', admin_reply),
    path('admin/tickets/<int:ticket_id>/assign/', admin_assign),
]
