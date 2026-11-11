"""One refusal type for the platform module: an approvals `ApprovalError`, so a view turns it
into the same error envelope and a caller catching `approvals.ApprovalError` catches both."""

from rest_framework import status as http

from servers.approvals.service import ApprovalError


class PlatformError(ApprovalError):
    code = 'PLATFORM_ERROR'
    status = http.HTTP_400_BAD_REQUEST
    field = 'general'

    def __init__(self, message, *, code=None, status=None, field=None):
        super().__init__(message, code=code, status=status, field=field)
