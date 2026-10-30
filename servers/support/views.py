"""Customer support ticket endpoints.

Surface:
  POST   /api/v1/support/tickets/                  create a ticket
  GET    /api/v1/support/tickets/                  list my tickets
  GET    /api/v1/support/tickets/<id>/             ticket detail + messages
  POST   /api/v1/support/tickets/<id>/messages/    add a reply (user side)
  POST   /api/v1/support/tickets/<id>/close/       close ticket (user side)

Admin / support-staff surface, each gated on a capability IN SCOPE:
  GET    /api/v1/support/admin/tickets/             list, `support.read`
  GET    /api/v1/support/admin/tickets/<id>/        detail, `support.read`
  POST   /api/v1/support/admin/tickets/<id>/reply/  staff reply, `support.reply`
  POST   /api/v1/support/admin/tickets/<id>/assign/ assign, `support.reply`

The operator surface is in `admin_support.py`; its scope rule (trip city,
else a driver's home city, else wherever the rider is visible) is
`scope.tickets_visible_to`. A ticket outside it answers NOT FOUND, never
FORBIDDEN, so an out-of-scope operator cannot walk ids. Internal notes are
never returned here: the message model's default manager leaves them out.
"""
import logging
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

from base.permissions import is_operator
from base.utils import error_response, success_response
from servers.support.models import SupportMessage, SupportTicket
from servers.support.scope import ticket_may, tickets_visible_to
from servers.support.serializers import SupportMessageSerializer, SupportTicketSerializer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User-side
# ---------------------------------------------------------------------------

def _not_found(ticket_id):
    """The one answer for "does not exist" AND "not in your scope"."""
    return error_response(
        code='NOT_FOUND', message='Ticket not found.', field='ticket_id',
        issue=f'Ticket {ticket_id}', status=status.HTTP_404_NOT_FOUND,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_ticket(request):
    issue_type = (request.data.get('issue_type') or 'other').strip()
    description = (request.data.get('description') or '').strip()
    trip_id = request.data.get('trip_id')

    if not description:
        return error_response(
            code='MISSING_FIELDS', message='description is required',
            field='description', issue='Describe the issue in at least a sentence',
            status=status.HTTP_400_BAD_REQUEST,
        )

    trip = None
    if trip_id:
        from servers.ride.models import Trip
        try:
            trip = Trip.objects.get(id=trip_id, user_id=request.user)
        except Trip.DoesNotExist:
            return error_response(
                code='NOT_FOUND', message='Trip not found or not yours.',
                field='trip_id', issue=f'Trip {trip_id}',
                status=status.HTTP_404_NOT_FOUND,
            )

    ticket = SupportTicket.objects.create(
        user_id=request.user,
        trip_id=trip,
        issue_type=issue_type,
        description=description,
    )
    # Mirror the description as the first message in the thread so the
    # admin only has to look at /messages/ for the full conversation.
    SupportMessage.objects.create(
        ticket=ticket, author=request.user, author_role='user', body=description,
    )
    return success_response(SupportTicketSerializer(ticket).data, status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_my_tickets(request):
    qs = SupportTicket.objects.filter(user_id=request.user).prefetch_related('messages').order_by('-created_at')
    status_filter = request.query_params.get('status')
    if status_filter:
        qs = qs.filter(status=status_filter)
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(qs, request)
    data = SupportTicketSerializer(page, many=True).data
    return success_response(paginator.get_paginated_response(data).data, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ticket_detail(request, ticket_id):
    try:
        ticket = SupportTicket.objects.prefetch_related('messages').get(id=ticket_id)
    except SupportTicket.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Ticket not found.', field='ticket_id',
            issue=f'Ticket {ticket_id}', status=status.HTTP_404_NOT_FOUND,
        )
    if ticket.user_id_id != request.user.id:
        # Not the requester. An operator may read it IF they hold
        # `support.read` where the ticket sits; this used to be a bare
        # `is_staff` bypass that let any staff account read any ticket in
        # any country. A non-operator keeps today's answer.
        if not is_operator(request.user):
            return error_response(
                code='FORBIDDEN', message='Not your ticket.', field='ticket_id',
                issue='User mismatch', status=status.HTTP_403_FORBIDDEN,
            )
        visible = tickets_visible_to(request.user, None, SupportTicket.objects.filter(
            id=ticket_id)).exists()
        if not visible:
            return _not_found(ticket_id)
        if not ticket_may(request.user, 'support.read', ticket):
            return error_response(
                code='FORBIDDEN', message='You do not have permission to do that here.',
                field='ticket_id', issue='support.read not held',
                status=status.HTTP_403_FORBIDDEN,
            )
    return success_response(SupportTicketSerializer(ticket).data, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def add_user_message(request, ticket_id):
    body = (request.data.get('body') or '').strip()
    if not body:
        return error_response(
            code='MISSING_FIELDS', message='body is required',
            field='body', issue='Reply cannot be empty',
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        ticket = SupportTicket.objects.get(id=ticket_id, user_id=request.user)
    except SupportTicket.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Ticket not found.', field='ticket_id',
            issue=f'Ticket {ticket_id}', status=status.HTTP_404_NOT_FOUND,
        )
    if ticket.status == 'CLOSED':
        return error_response(
            code='INVALID_STATE', message='Ticket is closed. Open a new one.',
            field='ticket_id', issue='status=CLOSED',
            status=status.HTTP_400_BAD_REQUEST,
        )
    msg = SupportMessage.objects.create(
        ticket=ticket, author=request.user, author_role='user', body=body,
    )
    # If support was waiting on the user, move it back to in-progress.
    if ticket.status == 'WAITING_USER':
        ticket.status = 'IN_PROGRESS'
        ticket.save(update_fields=['status', 'updated_at'])
    return success_response(SupportMessageSerializer(msg).data, status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def close_my_ticket(request, ticket_id):
    try:
        ticket = SupportTicket.objects.get(id=ticket_id, user_id=request.user)
    except SupportTicket.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Ticket not found.', field='ticket_id',
            issue=f'Ticket {ticket_id}', status=status.HTTP_404_NOT_FOUND,
        )
    if ticket.status == 'CLOSED':
        return success_response(SupportTicketSerializer(ticket).data, status.HTTP_200_OK)
    ticket.status = 'CLOSED'
    ticket.resolved_at = timezone.now()
    ticket.save(update_fields=['status', 'resolved_at', 'updated_at'])
    return success_response(SupportTicketSerializer(ticket).data, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Admin / support-staff side
# ---------------------------------------------------------------------------
# The operator console's queue, ticket page and writes live in
# `admin_support`; these names stay importable (and routed) so the URLs the
# console used before keep working, now through the audited code.
from servers.support.admin_support import (  # noqa: E402,F401
    admin_assign, admin_list_tickets, admin_reply, admin_ticket_detail,
)
