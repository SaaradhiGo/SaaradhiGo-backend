import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import render

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
        response = self.get_response(request)

        if response.status_code == 404 and request.accepts('text/html'):
            return render(request, 'errors/404.html', status=404)

        if response.status_code == 403 and request.accepts('text/html'):
            return render(request, 'errors/403.html', status=403)

        if response.status_code == 400 and request.accepts('text/html'):
            return render(request, 'errors/400.html', status=400)

        return response

    def process_exception(self, request, exception):
        if isinstance(exception, Http404):
            if request.accepts('text/html'):
                return render(request, 'errors/404.html', status=404)
            return JsonResponse(
                {
                    'status': 'error',
                    'error': {
                        'code': 'NOT_FOUND',
                        'message': 'The requested resource was not found.',
                    },
                },
                status=404,
            )

        if isinstance(exception, PermissionDenied):
            if request.accepts('text/html'):
                return render(request, 'errors/403.html', status=403)
            return JsonResponse(
                {
                    'status': 'error',
                    'error': {
                        'code': 'FORBIDDEN',
                        'message': 'You do not have permission to access this resource.',
                    },
                },
                status=403,
            )

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