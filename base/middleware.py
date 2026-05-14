import logging

from django.conf import settings
from django.http import JsonResponse

logger = logging.getLogger(__name__)


class ExceptionHandlingMiddleware:
    """Last-line exception handler for unhandled errors below DRF.

    Two prior bugs are fixed here:

    1. The previous version returned HTTP 404 for every uncaught
       exception. 404 says "not found" — wrong for a database error or
       a logic bug, and breaks correct retry/alerting upstream. Now we
       return 500.

    2. The previous version put str(exception) into the response body.
       That leaks internals — stack-trace fragments, SQL syntax,
       table/column names, file paths — to any external caller. Now
       we log full detail server-side and return a generic message
       (with a debug-only `detail` field when DEBUG=True).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        logger.exception(
            "Unhandled exception on %s %s",
            request.method,
            request.get_full_path(),
        )

        body = {
            "status": "error",
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred. Please try again.",
                "details": {
                    "field": "general",
                    "issue": "Internal server error",
                },
            },
        }
        if getattr(settings, 'DEBUG', False):
            body["error"]["details"]["debug"] = repr(exception)

        return JsonResponse(body, status=500)