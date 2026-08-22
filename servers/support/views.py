"""Customer support ticket endpoints.

Surface:
  POST   /api/v1/support/tickets/                  create a ticket
  GET    /api/v1/support/tickets/                  list my tickets
  GET    /api/v1/support/tickets/<id>/             ticket detail + messages
  POST   /api/v1/support/tickets/<id>/messages/    add a reply (user side)
  POST   /api/v1/support/tickets/<id>/close/       close ticket (user side)

Admin / support-staff surface (IsAdmin):
  GET    /api/v1/support/admin/tickets/            list all
  POST   /api/v1/support/admin/tickets/<id>/reply/ staff reply
  POST   /api/v1/support/admin/tickets/<id>/assign/ assign to a staff user
  POST   /api/v1/support/admin/tickets/<id>/status/ set status
"""
import logging
from datetime import datetime

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

from base.permissions import IsAdmin
from base.utils import error_response, success_response
from servers.support.models import SupportMessage, SupportTicket
from servers.support.serializers import SupportMessageSerializer, SupportTicketSerializer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User-side
# ---------------------------------------------------------------------------

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
    if ticket.user_id_id != request.user.id and not request.user.is_staff:
        return error_response(
            code='FORBIDDEN', message='Not your ticket.', field='ticket_id',
            issue='User mismatch', status=status.HTTP_403_FORBIDDEN,
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

@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_list_tickets(request):
    qs = SupportTicket.objects.prefetch_related('messages').all().order_by('-created_at')
    status_filter = request.query_params.get('status')
    if status_filter:
        qs = qs.filter(status=status_filter)
    issue_filter = request.query_params.get('issue_type')
    if issue_filter:
        qs = qs.filter(issue_type=issue_filter)
    paginator = PageNumberPagination()
    paginator.page_size = 25
    page = paginator.paginate_queryset(qs, request)
    return success_response(paginator.get_paginated_response(SupportTicketSerializer(page, many=True).data).data, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_reply(request, ticket_id):
    body = (request.data.get('body') or '').strip()
    next_status = (request.data.get('status') or '').strip()  # optional state change
    if not body:
        return error_response(
            code='MISSING_FIELDS', message='body is required',
            field='body', issue='Reply cannot be empty',
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        ticket = SupportTicket.objects.get(id=ticket_id)
    except SupportTicket.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Ticket not found.', field='ticket_id',
            issue=f'Ticket {ticket_id}', status=status.HTTP_404_NOT_FOUND,
        )
    msg = SupportMessage.objects.create(
        ticket=ticket, author=request.user, author_role='support', body=body,
    )
    fields = ['updated_at']
    if next_status in dict(SupportTicket.STATUS_CHOICES):
        ticket.status = next_status
        fields.append('status')
        if next_status == 'CLOSED':
            ticket.resolved_at = timezone.now()
            fields.append('resolved_at')
    elif ticket.status == 'OPEN':
        ticket.status = 'IN_PROGRESS'
        fields.append('status')
    ticket.save(update_fields=fields)
    return success_response(SupportMessageSerializer(msg).data, status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_assign(request, ticket_id):
    staff_user_id = request.data.get('assigned_to')
    if not staff_user_id:
        return error_response(
            code='MISSING_FIELDS', message='assigned_to is required',
            field='assigned_to', issue='Staff user id',
            status=status.HTTP_400_BAD_REQUEST,
        )
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        staff = User.objects.get(id=staff_user_id, is_staff=True)
    except User.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Staff user not found.',
            field='assigned_to', issue=f'User {staff_user_id}',
            status=status.HTTP_404_NOT_FOUND,
        )
    try:
        ticket = SupportTicket.objects.get(id=ticket_id)
    except SupportTicket.DoesNotExist:
        return error_response(
            code='NOT_FOUND', message='Ticket not found.', field='ticket_id',
            issue=f'Ticket {ticket_id}', status=status.HTTP_404_NOT_FOUND,
        )
    ticket.assigned_to = staff
    ticket.save(update_fields=['assigned_to', 'updated_at'])
    return success_response(SupportTicketSerializer(ticket).data, status.HTTP_200_OK)
