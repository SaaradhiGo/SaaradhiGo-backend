"""Audit for the live map: one row per session open and close, none per frame.

Where a person is, right now, is the most sensitive thing the console can
show, so looking at it is itself recorded: who, with which capability, over
which scope, from where, and for how long. A socket writes an OPEN row at
connect and a CLOSE row at disconnect (with how long it ran and why it ended:
a normal close, a revoked assignment). The REST snapshot and heatmap, which
are polled, write one `viewed` row per user per kind per
`VIEW_AUDIT_WINDOW_SECONDS` rather than one per request.

Rows carry capability names, scope paths and city codes. Never a position.
"""

from types import SimpleNamespace

from django.core.cache import cache

from servers.admin_audit.services import record_admin_action

from .config import (
    AUDIT_HEATMAP_VIEWED, AUDIT_SESSION_CLOSED, AUDIT_SESSION_OPENED, AUDIT_SNAPSHOT_VIEWED,
    AUDIT_TICKET_ISSUED,
)

TARGET = 'live_ops_session'
#: A polled view is one session for this long.
VIEW_AUDIT_WINDOW_SECONDS = 300


def request_like(user, asgi_scope=None):
    """What `record_admin_action` reads from a request, built from an ASGI scope."""
    asgi_scope = asgi_scope or {}
    headers = {k.decode('latin-1').lower(): v.decode('latin-1')
               for k, v in asgi_scope.get('headers', [])}
    client = asgi_scope.get('client') or (None, None)
    forwarded = headers.get('x-forwarded-for', '')
    meta = {
        'REMOTE_ADDR': client[0] or '',
        'HTTP_USER_AGENT': headers.get('user-agent', ''),
    }
    if forwarded:
        meta['HTTP_X_FORWARDED_FOR'] = forwarded
    return SimpleNamespace(user=user, META=meta)


def session_opened(request, *, session_id, scope, cities):
    return record_admin_action(
        request, action=AUDIT_SESSION_OPENED, target_type=TARGET, target_id=session_id,
        before={}, after={
            'transport': 'websocket', 'mode': scope.mode, 'capability': scope.capability,
            'scope': scope.describe(), 'cities': cities,
        })


def session_closed(request, *, session_id, scope, seconds, reason, frames):
    return record_admin_action(
        request, action=AUDIT_SESSION_CLOSED, target_type=TARGET, target_id=session_id,
        before={}, after={
            'transport': 'websocket', 'mode': scope.mode if scope else None,
            'capability': scope.capability if scope else None,
            'seconds': int(seconds), 'reason': reason, 'frames_sent': frames,
        })


def ticket_issued(request, *, scope):
    return record_admin_action(
        request, action=AUDIT_TICKET_ISSUED, target_type=TARGET, target_id='ticket',
        before={}, after={'mode': scope.mode, 'capability': scope.capability})


def view_once(request, *, kind, scope, city, detail=None):
    """Audit a polled REST view, at most once per window per user and kind."""
    action = {'snapshot': AUDIT_SNAPSHOT_VIEWED, 'heatmap': AUDIT_HEATMAP_VIEWED}[kind]
    key = f'liveops:viewed:{request.user.id}:{kind}'
    if not cache.add(key, 1, VIEW_AUDIT_WINDOW_SECONDS):
        return None
    return record_admin_action(
        request, action=action, target_type=TARGET, target_id=kind,
        before={}, after={
            'transport': 'rest', 'mode': scope.mode, 'capability': scope.capability,
            'scope': scope.describe(), 'city': city or None, **(detail or {}),
        })
