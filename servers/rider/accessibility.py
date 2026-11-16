"""The rider's accessibility note (14 Nov, USA; Idaho Code 49-3713): what the driver should know,
e.g. "I use a folding wheelchair". The trip's driver sees it when they accept.

    GET/PUT/PATCH /rider/accessibility/   {"note": "..."}
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import success_response


@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def accessibility_note(request):
    from .models import Rider

    rider, _ = Rider.objects.get_or_create(user_id=request.user)
    if request.method in ('PUT', 'PATCH'):
        rider.accessibility_note = ' '.join(str(request.data.get('note') or '').split())[:200]
        rider.save(update_fields=['accessibility_note'])
    return success_response({'note': rider.accessibility_note}, status.HTTP_200_OK)
