from django.http import JsonResponse
import logging

logger = logging.getLogger(__name__)

class ExceptionHandlingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        response = self.get_response(request)
        return response

    def process_exception(self, request, exception):
        logger.error(f"Unhandled exception: {exception}", exc_info=True)

        return JsonResponse({
            "status": "error",
            "error": {
                "code": "UNKNOWN_ERROR",
                "message": f"{exception}",
                "details": {
                    "field": "general",
                    "issue": "No details available"
                }
            }
        }, status=404)