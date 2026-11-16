"""Sign out of every device (security review, 16 Nov 2026)."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import success_response

from .session_revocation import revoke_all_refresh_tokens


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_all(request):
    """Blacklist every refresh token the caller holds; access tokens lapse within 15 minutes."""
    revoked = revoke_all_refresh_tokens(request.user)
    return success_response({'signed_out_sessions': revoked}, status.HTTP_200_OK)
