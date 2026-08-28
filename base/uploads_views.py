"""Presigned direct-to-S3 upload endpoint.

POST /api/v1/uploads/presign/ with {"kind": "rc_doc", "content_type": "image/png"}
returns {key, upload_url, expires_in}. The client PUTs the bytes straight to
S3 (with the exact signed Content-Type) and then submits `key` to the normal
resource endpoints — file bytes never transit Django, which keeps Daphne
workers free of long blocking uploads.

Permissions: any authenticated user may presign `avatar`; the document kinds
(rc_doc, license_doc, vehicle_pic) require a Driver profile.
"""
import logging

from django.core.exceptions import ValidationError
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

from base.s3 import generate_presigned_put, kind_spec
from base.utils import error_response, success_response

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def presign_upload(request):
    kind = request.data.get('kind')
    content_type = request.data.get('content_type')

    try:
        spec = kind_spec(kind)
    except ValidationError as exc:
        return error_response(
            code='INVALID_UPLOAD_KIND',
            message=str(exc.messages[0]),
            field='kind',
            issue=str(exc.messages),
            status=status.HTTP_400_BAD_REQUEST,
        )

    if kind != 'avatar' and getattr(request.user, 'driver', None) is None:
        return error_response(
            code='DRIVER_REQUIRED',
            message='Only drivers can request uploads for this document type',
            field='kind',
            issue=f"Kind '{kind}' requires a driver profile",
            status=status.HTTP_403_FORBIDDEN,
        )

    if not isinstance(content_type, str) or content_type.split(';')[0] not in spec['types']:
        return error_response(
            code='INVALID_CONTENT_TYPE',
            message='Unsupported content type for this upload',
            field='content_type',
            issue=f"Allowed for '{kind}': {sorted(spec['types'])}",
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        payload = generate_presigned_put(kind, content_type.split(';')[0])
    except ValidationError as exc:
        return error_response(
            code='PRESIGN_FAILED',
            message=str(exc.messages[0]),
            field='upload',
            issue=str(exc.messages),
            status=status.HTTP_400_BAD_REQUEST,
        )

    logger.info(f"Issued presigned upload key={payload['key']} user={request.user.id}")
    return success_response(
        {
            'kind': kind,
            'key': payload['key'],
            'upload_url': payload['upload_url'],
            'expires_in': payload['expires_in'],
            'method': 'PUT',
            'headers': {'Content-Type': content_type.split(';')[0]},
        },
        status.HTTP_200_OK,
    )
