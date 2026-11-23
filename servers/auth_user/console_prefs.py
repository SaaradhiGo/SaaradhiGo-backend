"""The console's own settings on the operator's account (owner, 22 Nov: "Dark is a full theme of its
own, chosen by the person and saved to their account. Light stays the default").

    GET   /api/v1/auth/admin/preferences/          {"theme": "light" | "dark"}
    PATCH /api/v1/auth/admin/preferences/          {"theme": "dark"}

Stored in `UserPreference.preferred_theme`, the same field the apps use, so one person has one
choice. Unset reads as light.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.permissions import is_operator
from base.utils import error_response, success_response

CONSOLE_THEMES = ('light', 'dark')


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def console_preferences(request):
    from .models import UserPreference

    if not is_operator(request.user):
        return error_response(code='NOT_AN_OPERATOR', message='This account cannot operate the platform.',
                              field='general', issue='Not an operator', status=status.HTTP_403_FORBIDDEN)
    if request.method == 'PATCH':
        theme = str((request.data or {}).get('theme') or '').strip().lower()
        if theme not in CONSOLE_THEMES:
            return error_response(code='INVALID_THEME', message='theme must be light or dark.', field='theme',
                                  issue=f'Got {theme!r}', status=status.HTTP_400_BAD_REQUEST)
        UserPreference.objects.update_or_create(user=request.user, defaults={'preferred_theme': theme})
    pref = UserPreference.objects.filter(user=request.user).values_list('preferred_theme', flat=True).first()
    return success_response({'theme': pref if pref in CONSOLE_THEMES else 'light'}, status.HTTP_200_OK)
