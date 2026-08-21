import logging
from urllib.parse import parse_qs
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.tokens import AccessToken
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)


@database_sync_to_async
def get_user_from_token(token_str):
    """
    Validate JWT access token and return the associated user.
    
    Args:
        token_str: JWT access token string
    
    Returns:
        User instance or AnonymousUser if token is invalid
    """
    try:
        User = get_user_model()
        token = AccessToken(token_str)
        user_id = token['user_id']
        return User.objects.get(id=user_id)
    except Exception:
        logger.warning("WebSocket JWT authentication failed")
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    """
    Custom middleware for JWT authentication on WebSocket connections.
    
    Usage: Connect with ws://host/ws/path/?token=<jwt_access_token>
    """

    async def __call__(self, scope, receive, send):
        # Parse token from query string
        query_string = scope.get('query_string', b'').decode('utf-8')
        query_params = parse_qs(query_string)
        token_list = query_params.get('token', [])
        # print(token_list)
        if token_list:
            user = await get_user_from_token(token_list[0])
            if user and user.is_authenticated:
                scope['user'] = user
            else:
                scope['user'] = AnonymousUser()
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
