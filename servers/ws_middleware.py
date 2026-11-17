import logging
from urllib.parse import parse_qs
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

#: Sockets that authenticate with a ticket instead of `?token=`.
ADMIN_WS_PREFIX = '/ws/admin/'


@database_sync_to_async
def get_user_from_ticket(ticket):
    """14 Nov: the website's sockets open with a single-use ticket (servers/auth_user/ws_ticket.py)."""
    from servers.auth_user.ws_ticket import user_id_for

    user_id = user_id_for(ticket)
    if not user_id:
        return AnonymousUser()
    try:
        return get_user_model().objects.get(id=user_id, is_active=True)
    except Exception:
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    """
    Custom middleware for JWT authentication on WebSocket connections.
    
    Usage: Connect with ws://host/ws/path/?ticket=<single-use ticket>
    """

    async def __call__(self, scope, receive, send):
        # The admin live-map socket does not take a JWT in the URL: it takes a
        # single-use ticket (`servers.live_ops.tickets`) and authenticates in the
        # consumer. Leave it anonymous here, whatever `?token=` says.
        if scope.get('path', '').startswith(ADMIN_WS_PREFIX):
            scope['user'] = AnonymousUser()
            return await super().__call__(scope, receive, send)

        # Parse token from query string
        query_string = scope.get('query_string', b'').decode('utf-8')
        query_params = parse_qs(query_string)
        # 16 Nov ruling: a socket address never carries the sign-in token (it ends up in access
        # logs). Only a 30 s single-use ticket from POST /auth/ws-ticket/ signs a socket in.
        ticket_list = query_params.get('ticket', [])
        if ticket_list:
            user = await get_user_from_ticket(ticket_list[0])
            scope['user'] = user if user and user.is_authenticated else AnonymousUser()
        elif 'user' not in scope:
            scope['user'] = AnonymousUser()

        # Extract coordinates if provided
        lat_list = query_params.get('lat', [])
        lng_list = query_params.get('lng', [])
        
        if lat_list:
            scope['lat'] = lat_list[0]
        if lng_list:
            scope['lng'] = lng_list[0]

        return await super().__call__(scope, receive, send)
