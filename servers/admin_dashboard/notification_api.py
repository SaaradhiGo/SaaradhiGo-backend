"""JSON feed behind the console's notification bell.

WHY THIS MODULE EXISTS
----------------------
`templates/admin_pages/base.html` polls `/api/notifications/` on every console
page load and POSTs to `/api/notifications/<id>/read/` when an item is
dismissed. Neither path was ever routed. Django answered 404 with an HTML error
page, the bell's `res.json()` tried to parse `<!DOCTYPE html>` and every admin
page logged:

    Notification fetch error: SyntaxError: Unexpected token '<'

So the bell has never shown anything, and the console has carried a console
exception on every page since the template was written. Found by driving the
deployed console in a real browser and watching the console, not by reading the
template.

AUTHORISATION
-------------
These endpoints do NOT use `@admin_required`. That decorator *redirects* an
unauthorised caller to the login page, which for a `fetch()` means a 200 full of
HTML and the exact parse error we are removing. A JSON endpoint has to answer
with JSON and a status code, so the same two questions are asked directly:

    is_operator(user)              -- is this ACCOUNT allowed to operate
    ops_mfa.console_access_allowed -- has this SESSION presented its factor

Both must be yes, exactly as `admin_required` requires. A rider who somehow
holds a session cookie gets 403 and an empty list, never another user's rows.

SCOPE
-----
A staff member sees their OWN notifications and nobody else's: the queryset is
filtered on `user_id=request.user` and the mark-read lookup carries the same
filter, so a guessed id belonging to someone else is a 404 rather than a silent
cross-account write. The model is the existing `rider.Notification` -- no new
table, no parallel notification system.
"""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse

from base import ops_mfa
from base.permissions import is_operator
from servers.rider.models import Notification

# How many unread items the bell will show. The dropdown is not a mailbox; it
# scrolls badly past this and the count is what matters.
FEED_LIMIT = 20

# `notif_type` is the model's vocabulary; the template's icon map keys on
# ALERT / WARNING / INFO and falls back to INFO for anything unknown. Mapping
# here keeps that fallback meaningful instead of making every row generic.
_SEVERITY = {
    'sos': 'ALERT',
    'kyc': 'WARNING',
    'payment': 'WARNING',
    'payout': 'WARNING',
    'wallet': 'WARNING',
    'ride_event': 'INFO',
    'system': 'INFO',
    'marketing': 'INFO',
}


def _denied() -> JsonResponse:
    """A JSON 403 — never a redirect into an HTML login page."""
    return JsonResponse(
        {'status': 'forbidden', 'notifications': []},
        status=403,
    )


def _allowed(request: HttpRequest) -> bool:
    return bool(
        is_operator(getattr(request, 'user', None))
        and ops_mfa.console_access_allowed(request)
    )


def _wrong_method(allowed: str) -> JsonResponse:
    response = JsonResponse(
        {'status': 'method_not_allowed', 'notifications': []},
        status=405,
    )
    response['Allow'] = allowed
    return response


def _serialise(notif: Notification) -> dict:
    return {
        'id': notif.id,
        'title': notif.title,
        'message': notif.message,
        # Rendered straight into the dropdown, so it is formatted here rather
        # than leaving the template to parse an ISO string in JavaScript.
        'created_at': notif.created_at.strftime('%d %b %Y, %H:%M'),
        'notification_type': _SEVERITY.get(notif.notif_type, 'INFO'),
    }


def notifications_feed(request: HttpRequest) -> JsonResponse:
    """Unread notifications for the signed-in operator, newest first.

    Returns the contract `base.html` already expects:

        {"status": "ok", "notifications": [{id, title, message,
                                            created_at, notification_type}]}

    Only unread rows are returned because the template uses
    `notifications.length` as the unread badge count.

    Authorisation is decided BEFORE the method is, so an anonymous caller is
    refused rather than told which verbs the route accepts. Django's
    `@require_GET` runs ahead of the view body and would answer 405 to an
    unauthenticated POST -- a refusal, but one that confirms the route exists
    and leaks its shape.
    """
    if not _allowed(request):
        return _denied()

    if request.method != 'GET':
        return _wrong_method('GET')

    rows = (
        Notification.objects
        .filter(user_id=request.user, is_read=False)
        .order_by('-created_at', '-id')[:FEED_LIMIT]
    )
    return JsonResponse({
        'status': 'ok',
        'notifications': [_serialise(n) for n in rows],
    })


def notification_mark_read(request: HttpRequest, notif_id: int) -> JsonResponse:
    """Mark one of the caller's OWN notifications as read.

    Idempotent: marking an already-read notification succeeds and changes
    nothing, so the bell double-clicking or retrying cannot produce an error
    the operator has to think about.

    As above: authorisation first, then method.
    """
    if not _allowed(request):
        return _denied()

    if request.method != 'POST':
        return _wrong_method('POST')

    # The user filter is part of the lookup, not a check afterwards: another
    # operator's id simply does not exist for this caller.
    notif = (
        Notification.objects
        .filter(id=notif_id, user_id=request.user)
        .first()
    )
    if notif is None:
        return JsonResponse(
            {'status': 'not_found', 'message': 'Notification not found.'},
            status=404,
        )

    if not notif.is_read:
        notif.is_read = True
        notif.save(update_fields=['is_read'])

    return JsonResponse({'status': 'ok', 'id': notif.id})
