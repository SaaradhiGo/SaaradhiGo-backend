"""Attribute every access write made during an HTTP request to its user.

The access models are audited by signal (`admin_audit.access_audit`), and a
signal receiver cannot see the request. This middleware opens an
`audit_context` carrying the request for the whole of its handling, so any
assignment, role, scope or authority-flag write a view makes -- directly or
three calls down -- is recorded against the operator who made it, with
their client address, without the view having to know the log exists.

The request is stored, not `request.user`: for the API the user is only
known once DRF has authenticated the bearer token inside the view, and DRF
then sets it on this same request object. The receiver reads it at the
moment the row is written, which is after that.
"""

from .context import audit_context


class AuditActorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with audit_context(request=request):
            return self.get_response(request)
