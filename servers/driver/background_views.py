"""Background check, accessibility acknowledgement (14 Nov, USA from day 1).

    GET  /driver/background/disclosure/        the FCRA disclosure, on its own page, and the status
    POST /driver/background/disclosure/        the driver has read it
    POST /driver/background/authorise/         the driver authorises the check -> provider form URL
    POST /driver/accessibility/acknowledge/    non-discrimination and accessibility (Idaho 49-3713)
    POST /driver/admin/<id>/background/        staff: decide a `consider` (clear | failed); on QA
                                               the fake provider's statuses too
    POST /driver/webhooks/background/<provider>/   the provider's status webhook (signed)
"""
from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from base.utils import error_response as _error_response, success_response


def error_response(*, code, message, status, field=''):
    return _error_response(code=code, message=message, field=field, issue=message, status=status)
from servers.admin_access.bridge import ScopedPermission

from . import background

#: A stand-alone FCRA disclosure (15 U.S.C. 1681b(b)(2)): a document consisting solely of the
#: disclosure. Legal review pending; the provider may supply its own wording.
FCRA_DISCLOSURE = {
    'title': 'Disclosure regarding background investigation',
    'version': 'fcra-disclosure-v1',
    'paragraphs': [
        'DeepTrics LLC, which operates SaaradhiGo in the United States, may obtain information about you '
        'from a third-party consumer reporting agency for employment-related purposes, which here means '
        'deciding whether you may drive on the SaaradhiGo platform. We may obtain consumer reports and '
        'investigative consumer reports about you now and, if you drive with us, at any time while you do, '
        'including yearly re-checks.',
        'The reports may contain information about your character, general reputation, personal '
        'characteristics and mode of living, and may include criminal records (local and national, '
        'including the national sex offender registry), and your driving record.',
        'You have the right, on written request made within a reasonable time, to a complete and '
        'accurate disclosure of the nature and scope of any investigative consumer report, and to a copy '
        'of "A Summary of Your Rights Under the Fair Credit Reporting Act".',
    ],
}


def _driver(request):
    return getattr(request.user, 'driver', None)


def _state(d):
    return {'disclosure_read': bool(d.background_disclosure_at), 'authorised': bool(d.background_consent_at),
            'status': d.background_check_status, 'checked_at': d.background_checked_at.isoformat()
            if d.background_checked_at else None,
            'accessibility_acknowledged': bool(d.accessibility_ack_at)}


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def background_disclosure(request):
    d = _driver(request)
    if d is None:
        return error_response(code='NOT_A_DRIVER', message='Only drivers have a background check.',
                              status=status.HTTP_403_FORBIDDEN)
    if request.method == 'POST':
        background.record_disclosure(d)
    return success_response({'disclosure': FCRA_DISCLOSURE, **_state(d)}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def background_authorise(request):
    d = _driver(request)
    if d is None:
        return error_response(code='NOT_A_DRIVER', message='Only drivers have a background check.',
                              status=status.HTTP_403_FORBIDDEN)
    if request.data.get('authorise') is not True:
        return error_response(code='AUTHORISATION_REQUIRED', field='authorise',
                              message='Tick the box to authorise the background check.',
                              status=status.HTTP_400_BAD_REQUEST)
    try:
        started = background.authorise_and_start(d)
    except ValueError as exc:
        return error_response(code='BACKGROUND_CHECK_UNAVAILABLE', message=str(exc),
                              status=status.HTTP_409_CONFLICT)
    return success_response({'form_url': started.form_url, **_state(d)}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def accessibility_acknowledge(request):
    d = _driver(request)
    if d is None:
        return error_response(code='NOT_A_DRIVER', message='Only drivers acknowledge this.',
                              status=status.HTTP_403_FORBIDDEN)
    if request.data.get('acknowledge') is not True:
        return error_response(code='ACKNOWLEDGEMENT_REQUIRED', field='acknowledge',
                              message='Tick the box to agree.', status=status.HTTP_400_BAD_REQUEST)
    if not d.accessibility_ack_at:
        d.accessibility_ack_at = timezone.now()
        d.save(update_fields=['accessibility_ack_at'])
    return success_response(_state(d), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.write')])
def admin_background_decision(request, driver_id):
    """A reviewer decides a `consider` report (clear or failed). Outside production the fake
    provider's statuses may be set here too, to run the flow on QA."""
    from .admin_drivers import _driver_for_write

    driver, refusal = _driver_for_write(request, driver_id, 'driver.write')
    if refusal:
        return refusal
    decision = str(request.data.get('status') or '')
    note = str(request.data.get('note') or '').strip()[:500]
    fake = driver.background_check_provider == 'fake' and not getattr(settings, 'IS_PRODUCTION', True)
    allowed = background.REVIEW_DECISIONS + (background.STATUSES if fake else ())
    if decision not in allowed:
        return error_response(code='INVALID_VALUE', field='status',
                              message=f'status must be one of {", ".join(allowed)}.',
                              status=status.HTTP_400_BAD_REQUEST)
    if decision in background.REVIEW_DECISIONS and not fake and driver.background_check_status != 'consider':
        return error_response(code='NOT_IN_REVIEW', message='Only a report marked "consider" is decided here.',
                              status=status.HTTP_409_CONFLICT)
    if decision == 'failed' and not note:
        return error_response(code='NOTE_REQUIRED', field='note', message='Say why the check failed.',
                              status=status.HTTP_400_BAD_REQUEST)
    background.apply_status(driver, decision, actor=request.user, note=note)
    return success_response({'driver_id': driver.id, 'status': driver.background_check_status}, status.HTTP_200_OK)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def background_webhook(request, provider):
    code, message = background.handle_webhook(
        provider, request.body, request.headers.get('X-Signature', ''))
    if code != 200:
        return error_response(code='WEBHOOK_REFUSED', message=message, status=code)
    return success_response({'ok': True}, status.HTTP_200_OK)
