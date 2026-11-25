"""25 Nov (owner): My security -- the caller's own signed-in sessions, and sign out the others.

    GET  /api/v1/auth/sessions/                 the live sessions, this one marked
    POST /api/v1/auth/sessions/sign-out-others/ end every session but this one

A session is a refresh token (14 days, rotated on use). The console's server sends the id (`jti`) of
the refresh token its cookie holds as `X-SG-Session`: never the token itself, so the header grants
nothing, and a jti that is not the caller's is ignored. Like mfa_views, these read or end the
CALLER's own sessions only, so they are gated on being an operator, not on a capability.
"""

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.permissions import is_operator
from base.utils import error_response, success_response
from servers.admin_audit.services import record_admin_action


def _live(user):
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    return (OutstandingToken.objects.filter(user=user, expires_at__gt=timezone.now(), blacklistedtoken__isnull=True)
            .order_by('-created_at'))


def _refuse():
    return error_response(code='NOT_AN_OPERATOR', message='This account cannot operate the platform.',
                          field='general', issue='Not an operator', status=status.HTTP_403_FORBIDDEN)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_sessions(request):
    if not is_operator(request.user):
        return _refuse()
    here = request.headers.get('X-SG-Session') or ''
    rows = [{'id': t.pk, 'started_at': t.created_at.isoformat() if t.created_at else None,
             'expires_at': t.expires_at.isoformat(), 'current': bool(here) and t.jti == here}
            for t in _live(request.user)[:50]]
    return success_response({'sessions': rows, 'count': len(rows), 'this_session_known': any(r['current'] for r in rows)},
                            status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def sign_out_others(request):
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

    if not is_operator(request.user):
        return _refuse()
    here = request.headers.get('X-SG-Session') or ''
    if not here or not _live(request.user).filter(jti=here).exists():
        return error_response(code='SESSION_UNKNOWN', message='This session could not be identified; sign out '
                              'everywhere instead, then sign in again.', field='general',
                              issue='No current session', status=status.HTTP_409_CONFLICT)
    ended = 0
    for token in _live(request.user).exclude(jti=here):
        _, made = BlacklistedToken.objects.get_or_create(token=token)
        ended += int(made)
    record_admin_action(request, action='staff_signed_out_other_sessions', target_type='User',
                        target_id=str(request.user.pk), after={'sessions_ended': ended},
                        reason='Signed out other sessions from My security')
    return success_response({'sessions_ended': ended}, status.HTTP_200_OK)
