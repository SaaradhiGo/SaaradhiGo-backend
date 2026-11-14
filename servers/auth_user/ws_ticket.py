"""A single-use ticket for a rider's or driver's socket (14 Nov, owner).

The website no longer holds a login token in the page: the tokens are HttpOnly cookies on the
web's own server, which adds them to API calls. A WebSocket cannot be opened through that server,
so the page asks for a ticket here (an ordinary authenticated call, through the server) and opens
`/ws/ride/...?ticket=`. Same rules as the console's live-map ticket (servers/live_ops/tickets.py):
30 seconds, works once, only its hash is stored. The apps keep `?token=`.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.live_ops.config import TICKET_TTL_SECONDS
from servers.live_ops.tickets import TicketStoreUnavailable, consume_ticket, issue_ticket

logger = logging.getLogger(__name__)

RIDE_TICKET_PREFIX = 'ridews:ticket:'


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ws_ticket(request):
    try:
        ticket = issue_ticket(request.user.id, prefix=RIDE_TICKET_PREFIX)
    except TicketStoreUnavailable:
        logger.error('ride ws ticket store unavailable')
        return error_response(
            code='UNAVAILABLE', message='Live updates are unavailable. Try again shortly.',
            field='general', issue='Ticket store unavailable', status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return success_response({'ticket': ticket, 'expires_in': TICKET_TTL_SECONDS}, status.HTTP_201_CREATED)


def user_id_for(ticket):
    """The user the ticket was issued to, once; None if unknown, used, expired or unavailable."""
    try:
        return consume_ticket(ticket, prefix=RIDE_TICKET_PREFIX)
    except TicketStoreUnavailable:
        logger.error('ride ws ticket store unavailable')
        return None
