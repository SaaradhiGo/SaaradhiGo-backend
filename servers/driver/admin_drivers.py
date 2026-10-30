"""Drivers & KYC module (operator console, module 3): list, queue, detail, writes.

WHAT LIVES HERE
---------------
* The driver list's server-side filters, sort and pages (the route and its
  permission stay in `admin_views.list_drivers_admin`, so the console's URL
  is unchanged), and the KYC queue.
* The detail's extra sections: per-document review state, the approval
  blockers, ratings, SOS / ticket counts, suspension, status history, notes,
  the driver's audit trail and the actions the caller may take.
* The writes: per-document approve / reject, approve driver, suspend,
  reinstate, internal note, vehicle activate / deactivate, bulk, and the
  `driver.deactivate` executor that replaces the old hard delete.

RULES THIS FILE KEEPS
---------------------
* Every write is gated by `ScopedPermission`, re-checked at the driver's
  home city (out of scope or outside a KYC-only caller's queue answers 404),
  audited with `record_admin_action`, and idempotent: repeating a write
  changes nothing and writes no second audit row.
* Nothing here touches earnings, commissions, incentives or payouts.
* Suspension reuses the dispatch gate that already exists: it writes the
  two columns the licence-expiry sweeper writes (`approved=False`,
  `status='blocked'`), which `qa_eligibility.may_operate` and the socket's
  blocked check refuse at connect, on offers and on accept.
"""

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Exists, OuterRef, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted, node_of
from servers.admin_access.scoping import (
    ScopeNotHeld, scoped, scoped_or_none, scoped_to_request,
)
from servers.admin_audit.services import record_admin_action
from servers.approvals import bulk, registry
from servers.approvals import service as approvals
from servers.approvals.actions import BULK_ACTION, DRIVER_DEACTIVATE
from servers.ride.admin_trips import (
    BadInput, _approval_answer, _bool, _csv, _date, _idempotency_key, _int, _iso, _staff,
    _text,
)

from . import compliance
from .admin_views import for_kyc_view
from .models import (
    Driver, DriverDocumentReview, DriverNote, DriverSuspension, Vehicle,
)

logger = logging.getLogger(__name__)

TARGET = 'driver'

KYC_STATES = ('pending', 'in_review', 'approved', 'rejected', 'expired')
ONLINE_STATUSES = ('online', 'on ride')
VEHICLE_EXPIRIES = ('insurance_expiry', 'permit_expiry', 'fitness_expiry', 'puc_expiry')
SUSPEND_REASONS = {k: v for k, v in DriverSuspension.REASON_CHOICES if k != 'deactivated'}
BULK_ACTIONS = ('suspend', 'reinstate')
BULK_MAX_IDS = 500
REASON_LIMIT = 500

SORTS = {
    'id': 'id',
    'joined': 'user_id__created_at',
    'rating': 'ratings',
    'trips': 'total_trips',
    'waiting': 'waiting_since',
    'license_expiry': 'license_expiry',
}
DEFAULT_SORT = '-id'

#: Audit actions that change a driver's standing, shown as status history.
STATUS_ACTIONS = (
    'kyc_update', 'driver_approved', 'driver_suspended', 'driver_reinstated',
    'driver_deactivated', 'driver_document_reviewed',
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_file(obj, column):
    field = getattr(obj, column, None) if obj is not None else None
    return bool(field and getattr(field, 'name', ''))


def _today():
    return timezone.now().date()


def _country(driver):
    """The approvals country: the home city's, else the phone number's."""
    node = getattr(driver, 'home_city', None)
    code = getattr(node, 'code', '') or ''
    if code and code != 'GLOBAL':
        return code.split('-')[0]
    return compliance.driver_country_code(driver)


def city_tz(node):
    """The time zone a home city keeps: its service zone's, else its country's."""
    from base.countries import get_country
    from base.market_time import _zoneinfo, zone_timezone
    from servers.pricing.models import ServiceZone

    if node is None:
        return _zoneinfo(None)
    zone = ServiceZone.objects.filter(geo_node=node).order_by('id').first()
    if zone is not None:
        return zone_timezone(zone)
    try:
        return _zoneinfo(get_country(node.code.split('-')[0]).default_timezone)
    except Exception:
        return _zoneinfo(None)


def _city(driver):
    node = getattr(driver, 'home_city', None)
    if node is None:
        return None
    return {'code': node.code, 'name': node.name, 'timezone': str(city_tz(node))}


def active_suspension_q():
    return Q(id__in=DriverSuspension.objects.filter(lifted_at__isnull=True)
             .values('driver_id'))


def expired_q(today=None):
    today = today or _today()
    q = Q(license_expiry__lt=today)
    for name in VEHICLE_EXPIRIES:
        q |= Q(**{f'active_vehicle__{name}__lt': today})
    return q


def expiring_q(days, today=None):
    today = today or _today()
    until = today + timedelta(days=days)
    q = Q(license_expiry__gte=today, license_expiry__lte=until)
    for name in VEHICLE_EXPIRIES:
        q |= Q(**{f'active_vehicle__{name}__gte': today,
                  f'active_vehicle__{name}__lte': until})
    return q


def _uploaded_q():
    return Q(license_doc__isnull=False) & ~Q(license_doc='')


def kyc_state_q(state, today=None):
    if state == 'approved':
        return Q(approved=True)
    if state == 'rejected':
        return Q(approved=False, doc_status='rejected')
    if state == 'in_review':
        # Documents on file, nobody has decided yet. `doc_status='approved'`
        # with `approved=False` is a driver the expiry sweeper took off the
        # road: they are back in review.
        return Q(approved=False) & ~Q(doc_status='rejected') & _uploaded_q()
    if state == 'pending':
        return Q(approved=False) & ~Q(doc_status='rejected') & ~_uploaded_q()
    if state == 'expired':
        return expired_q(today)
    raise BadInput('INVALID_FILTER', f'kyc_state must be one of {", ".join(KYC_STATES)}.',
                   'kyc_state')


def kyc_state(driver):
    if driver.approved:
        return 'approved'
    if driver.doc_status == 'rejected':
        return 'rejected'
    return 'in_review' if _has_file(driver, 'license_doc') else 'pending'


def documents_expired(driver, today=None):
    today = today or _today()
    if driver.license_expiry and driver.license_expiry < today:
        return True
    vehicle = driver.active_vehicle
    return bool(vehicle) and any(
        getattr(vehicle, n) and getattr(vehicle, n) < today for n in VEHICLE_EXPIRIES)


def annotate(queryset):
    from servers.ride.models import TripFlag

    return queryset.annotate(
        is_suspended=Exists(DriverSuspension.objects.filter(
            driver=OuterRef('pk'), lifted_at__isnull=True)),
        is_flagged=Exists(TripFlag.objects.filter(
            trip__driver_id=OuterRef('pk'), cleared_at__isnull=True)),
        waiting_since=Coalesce('uploaded_timestamp', 'user_id__created_at'),
    )


def _decimal(params, name):
    raw = params.get(name)
    if raw in (None, ''):
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        raise BadInput('INVALID_FILTER', f'{name} must be a number.', name)
    if value < 0 or value > 5:
        raise BadInput('INVALID_FILTER', f'{name} must be between 0 and 5.', name)
    return value


def _joined_q(queryset, date_from, date_to):
    """user joined within [date_from, date_to] in EACH driver's home-city day."""
    from base.market_time import _zoneinfo
    from servers.admin_access.models import GeoNode

    def bounds(tz):
        q = Q()
        if date_from:
            q &= Q(user_id__created_at__gte=datetime.combine(date_from, time.min, tzinfo=tz))
        if date_to:
            q &= Q(user_id__created_at__lt=datetime.combine(
                date_to + timedelta(days=1), time.min, tzinfo=tz))
        return q

    node_ids = set(queryset.exclude(home_city=None)
                   .values_list('home_city_id', flat=True).distinct())
    by_tz = {}
    for node in GeoNode.objects.filter(id__in=node_ids):
        tz = city_tz(node)
        by_tz.setdefault(str(tz), (tz, []))[1].append(node.id)
    combined = Q(home_city__isnull=True) & bounds(_zoneinfo(None))
    for tz, ids in by_tz.values():
        combined |= Q(home_city_id__in=ids) & bounds(tz)
    return combined


def filter_drivers(params, queryset, today=None):
    """Apply the list's filters and sort. Raises BadInput on a malformed value."""
    today = today or _today()
    queryset = annotate(queryset)

    states = _csv(params.get('kyc_state'))
    if states:
        q = Q()
        for state in states:
            q |= kyc_state_q(state, today)
        queryset = queryset.filter(q)

    driver_id = params.get('id') or params.get('driver_id')
    if driver_id:
        queryset = queryset.filter(id=_int(driver_id, 'id'))

    # The filters the list has always taken.
    approved = _bool(params, 'approved')
    if approved is not None:
        queryset = queryset.filter(approved=approved)
    statuses = _csv(params.get('status'))
    if statuses:
        queryset = queryset.filter(status__in=statuses)

    online = _bool(params, 'online')
    if online is True:
        queryset = queryset.filter(status__in=ONLINE_STATUSES)
    elif online is False:
        queryset = queryset.exclude(status__in=ONLINE_STATUSES)

    vehicle_types = _csv(params.get('vehicle_type'))
    if vehicle_types:
        queryset = queryset.filter(active_vehicle__vehicle_type_id__type__in=vehicle_types)

    within = params.get('expiring_within')
    if within not in (None, ''):
        days = _int(within, 'expiring_within')
        if days < 0 or days > 366:
            raise BadInput('INVALID_FILTER', 'expiring_within must be 0 to 366 days.',
                           'expiring_within')
        queryset = queryset.filter(expiring_q(days, today))

    low, high = _decimal(params, 'rating_min'), _decimal(params, 'rating_max')
    if low is not None and high is not None and low > high:
        raise BadInput('INVALID_FILTER', 'rating_min is above rating_max.', 'rating_min')
    if low is not None:
        queryset = queryset.filter(ratings__gte=low)
    if high is not None:
        queryset = queryset.filter(ratings__lte=high)

    joined_from, joined_to = _date(params, 'joined_from'), _date(params, 'joined_to')
    if joined_from and joined_to and joined_from > joined_to:
        raise BadInput('INVALID_DATE', 'joined_from is after joined_to.', 'joined_from')
    if joined_from or joined_to:
        queryset = queryset.filter(_joined_q(queryset, joined_from, joined_to))

    for name, column in (('suspended', 'is_suspended'), ('flagged', 'is_flagged')):
        wanted = _bool(params, name)
        if wanted is not None:
            queryset = queryset.filter(**{column: wanted})

    ordering = (params.get('ordering') or DEFAULT_SORT).strip()
    key = ordering.lstrip('-')
    if key not in SORTS:
        raise BadInput(
            'INVALID_FILTER',
            f'ordering must be one of {", ".join(sorted(SORTS))} (prefix - for descending).',
            'ordering')
    prefix = '-' if ordering.startswith('-') else ''
    return queryset.order_by(f'{prefix}{SORTS[key]}', f'{prefix}id')


# ---------------------------------------------------------------------------
# List and queue
# ---------------------------------------------------------------------------

class DriverPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def _base_row(driver):
    """The list row's own fields. Not `DriverAdminListSerializer`: that one
    builds a presigned licence URL per row and sends the user's FCM token,
    dob and street address, none of which a list needs."""
    user = driver.user_id
    return {
        'id': driver.id,
        'user_details': {
            'id': user.id, 'full_name': user.full_name, 'phone_number': user.phone_number,
            'email': user.email, 'role': user.role, 'is_active': user.is_active,
        },
        'docs_uploaded': _has_file(driver, 'license_doc'),
        'license_expiry': _iso(driver.license_expiry),
        'status': driver.status,
        'total_trips': driver.total_trips,
        'ratings': str(driver.ratings),
        'approved': driver.approved,
        'active_vehicle': driver.active_vehicle_id,
    }


def _row(driver, today):
    vehicle = driver.active_vehicle
    row = _base_row(driver)
    row.update({
        'kyc_state': kyc_state(driver),
        'doc_status': driver.doc_status,
        'suspended': bool(getattr(driver, 'is_suspended', False)),
        'flagged': bool(getattr(driver, 'is_flagged', False)),
        'documents_expired': documents_expired(driver, today),
        'online': driver.status in ONLINE_STATUSES,
        'home_city': _city(driver),
        'vehicle_type': (vehicle.vehicle_type_id.type
                         if vehicle and vehicle.vehicle_type_id else None),
        'vehicle_number': vehicle.vehicle_number if vehicle else None,
        'joined_at': _iso(getattr(driver.user_id, 'created_at', None)),
        'waiting_since': _iso(getattr(driver, 'waiting_since', None)),
    })
    return row


def _page(request, queryset, build):
    paginator = DriverPagination()
    page = paginator.paginate_queryset(queryset, request)
    django_page = paginator.page
    return success_response({
        'count': django_page.paginator.count,
        'next': paginator.get_next_link(),
        'previous': paginator.get_previous_link(),
        'page': django_page.number,
        'page_size': paginator.get_page_size(request),
        'total_pages': django_page.paginator.num_pages,
        'ordering': (request.query_params.get('ordering') or DEFAULT_SORT),
        'results': build(page),
    }, status.HTTP_200_OK)


def list_response(request, queryset):
    """Body of GET /driver/admin/ for an already SCOPED queryset."""
    try:
        queryset = filter_drivers(request.query_params, queryset.select_related(
            'user_id', 'home_city', 'active_vehicle', 'active_vehicle__vehicle_type_id'))
    except BadInput as exc:
        return exc.response()
    today = _today()

    def build(page):
        return [_row(d, today) for d in page]

    return _page(request, queryset, build)


def _document_rows(driver, vehicle, documents_visible):
    """Each required document of one owner (the driver, or one vehicle)."""
    owner = vehicle if vehicle is not None else driver
    if vehicle is None:
        checks = compliance.evaluate_driver(driver)
        columns = compliance.DRIVER_DOCUMENT_COLUMNS
    else:
        checks = compliance.evaluate_vehicle(vehicle, driver)
        columns = compliance.VEHICLE_DOCUMENT_COLUMNS
    latest = {}
    for review in (DriverDocumentReview.objects
                   .filter(driver=driver, vehicle=vehicle)
                   .select_related('reviewed_by').order_by('reviewed_at', 'id')):
        latest[review.document_key] = review
    rows = []
    for key, check in checks.items():
        column = columns.get(key)
        field = getattr(owner, column, None) if column else None
        name = getattr(field, 'name', '') if field else ''
        review = latest.get(key)
        current = review is not None and review.file_name == name
        url = None
        if documents_visible and name:
            from .admin_views import _document_url
            url = _document_url(field)
        rows.append({
            'key': key,
            'label': check['label'],
            'description': compliance.document_description(key),
            'owner': 'vehicle' if vehicle is not None else 'driver',
            'vehicle_id': vehicle.id if vehicle is not None else None,
            'present': check['present'],
            'recordable': check['applicable'],
            # Documents follow `pii.view` at the driver's city, as the full
            # detail always has; `url_withheld` says why there is no link.
            'url': url,
            'url_withheld': bool(name) and not documents_visible,
            'review_state': review.state if current else ('not_reviewed' if name else 'missing'),
            'review': ({
                'state': review.state, 'reason': review.reason,
                'reviewed_at': _iso(review.reviewed_at),
                'created_by': _staff(review.reviewed_by),
                'current': current,
            } if review else None),
        })
    return rows


def _documents_visible(user, driver):
    return granted(user, 'pii.view', node_of(driver))[0]


def documents_payload(user, driver):
    visible = _documents_visible(user, driver)
    rows = _document_rows(driver, None, visible)
    for vehicle in driver.vehicle_set.order_by('id'):
        rows.extend(_document_rows(driver, vehicle, visible))
    return rows


def queue_response(request):
    """GET /driver/admin/kyc-queue/: drivers awaiting a review, oldest first."""
    try:
        queryset = for_kyc_view(request.user, scoped_to_request(
            request, Driver.objects.select_related(
                'user_id', 'home_city', 'active_vehicle',
                'active_vehicle__vehicle_type_id')))
    except ScopeNotHeld as exc:
        return error_response(code='SCOPE_NOT_HELD', message=str(exc), field='scope',
                              issue='Requested scope is outside your assignments',
                              status=status.HTTP_403_FORBIDDEN)
    queryset = (annotate(queryset).filter(kyc_state_q('in_review'))
                .exclude(active_suspension_q()).order_by('waiting_since', 'id'))
    today = _today()
    now = timezone.now()

    def build(page):
        from .admin_views import _compliance_payload

        out = []
        for driver in page:
            row = _row(driver, today)
            since = getattr(driver, 'waiting_since', None)
            row['waiting_seconds'] = int((now - since).total_seconds()) if since else None
            row['checklist'] = _compliance_payload(driver)
            row['documents'] = documents_payload(request.user, driver)
            out.append(row)
        return out

    return _page(request, queryset, build)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
def admin_kyc_queue(request):
    """GET /driver/admin/kyc-queue/  The KYC Verifier's work list (scoped)."""
    return queue_response(request)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def approval_blockers(driver):
    """Why `approve` would refuse right now: [] when it would succeed."""
    from .serializers import KYCApprovalSerializer

    blockers = []
    if DriverSuspension.objects.filter(driver=driver, lifted_at__isnull=True).exists():
        blockers.append({'code': 'DRIVER_SUSPENDED',
                         'message': 'Suspended: reinstate before approving.'})
    rejected = rejected_documents(driver)
    if rejected:
        blockers.append({'code': 'DOCUMENTS_REJECTED',
                         'message': 'Rejected: ' + ', '.join(r['label'] for r in rejected)})
    vehicle = driver.active_vehicle
    missing = (compliance.missing_documents(vehicle, driver) if vehicle
               else [k for k, c in compliance.evaluate_driver(driver).items()
                     if not (c['present'] and c['applicable'])])
    if missing:
        blockers.append({'code': 'DOCUMENTS_MISSING',
                         'message': 'Missing: ' + ', '.join(
                             compliance.document_label(k) for k in missing)})
    serializer = KYCApprovalSerializer(driver, data={'approved': True}, partial=True)
    if not serializer.is_valid():
        for field, errors in serializer.errors.items():
            for error in (errors if isinstance(errors, list) else [errors]):
                blockers.append({'code': 'KYC_GATE', 'field': field, 'message': str(error)})
    return blockers


def rejected_documents(driver):
    """Documents whose current file was rejected (latest review per document)."""
    out = []
    for row in documents_payload_raw(driver):
        if row['review_state'] == DriverDocumentReview.REJECTED:
            out.append(row)
    return out


def documents_payload_raw(driver):
    rows = _document_rows(driver, None, False)
    for vehicle in driver.vehicle_set.order_by('id'):
        rows.extend(_document_rows(driver, vehicle, False))
    return rows


def _audit_rows(driver, actions=None):
    from servers.admin_audit.models import AdminAuditLog

    rows = AdminAuditLog.objects.filter(target_type=TARGET, target_id=str(driver.id))
    if actions:
        rows = rows.filter(action__in=actions)
    return [{
        'id': row.id, 'action': row.action, 'created_at': _iso(row.created_at),
        'reason': row.reason, 'after': row.after if actions else None,
        'created_by': {'id': row.actor_id, 'label': row.actor_label, 'role': row.actor_role},
    } for row in rows.order_by('-created_at', '-id')[:100]]


def _ratings(driver):
    from servers.ride.models import Rating

    given = Rating.objects.filter(trip_id__driver_id=driver).exclude(rater_id=driver.user_id)
    agg = given.aggregate(avg=Avg('score'), count=Count('id'))
    distribution = {str(i): 0 for i in range(1, 6)}
    for row in given.values('score').annotate(n=Count('id')):
        if 1 <= (row['score'] or 0) <= 5:
            distribution[str(row['score'])] = row['n']
    return {
        'average': str(driver.ratings),
        'computed_average': (round(float(agg['avg']), 2) if agg['avg'] is not None else None),
        'count': agg['count'],
        'distribution': distribution,
    }


def _counts(driver):
    from servers.sos.models import SOSEvent
    from servers.support.models import SupportTicket

    sos = SOSEvent.objects.filter(Q(user=driver.user_id) | Q(trip__driver_id=driver))
    tickets = SupportTicket.objects.filter(
        Q(user_id=driver.user_id) | Q(trip_id__driver_id=driver))
    return {
        'sos_total': sos.count(),
        'sos_open': sos.filter(status__in=('open', 'acknowledged')).count(),
        'tickets_total': tickets.count(),
        'tickets_open': tickets.exclude(status='CLOSED').count(),
    }


def _suspension(driver):
    row = (DriverSuspension.objects.filter(driver=driver, lifted_at__isnull=True)
           .select_related('created_by').first())
    if row is None:
        return {'active': False}
    return {'active': True, 'id': row.id, 'reason_code': row.reason_code,
            'reason_label': dict(DriverSuspension.REASON_CHOICES).get(row.reason_code),
            'note': row.note, 'created_at': _iso(row.created_at),
            'created_by': _staff(row.created_by),
            'permanent': row.reason_code == 'deactivated'}


def allowed_actions(user, driver, blockers=None):
    """What this caller may do to this driver, so the console shows only that."""
    node = node_of(driver)

    def holds(permission):
        return granted(user, permission, node)[0]

    may_kyc, may_write = holds('driver.kyc'), holds('driver.write')
    suspended = DriverSuspension.objects.filter(driver=driver, lifted_at__isnull=True).first()
    if blockers is None:
        blockers = approval_blockers(driver)
    return {
        'review_documents': may_kyc,
        'approve': (may_kyc or may_write) and not driver.approved,
        'approve_blocked': bool(blockers),
        'suspend': may_write and suspended is None,
        'reinstate': may_write and suspended is not None
        and suspended.reason_code != 'deactivated',
        'note': may_kyc or may_write,
        'vehicles': may_write,
        'deactivate': holds('driver.deactivate') and driver.user_id.is_active,
        'reveal': holds('pii.view'),
        'suspend_reasons': SUSPEND_REASONS,
    }


def detail_extras(request, driver):
    """The sections the Drivers module adds to the full detail payload."""
    from servers.approvals.models import ApprovalRequest

    blockers = approval_blockers(driver)
    return {
        'home_city': _city(driver),
        'kyc_state': kyc_state(driver),
        'documents_expired': documents_expired(driver),
        'joined_at': _iso(getattr(driver.user_id, 'created_at', None)),
        'documents': documents_payload(request.user, driver),
        'approval_blockers': blockers,
        'ratings_summary': _ratings(driver),
        'counts': _counts(driver),
        'suspension': _suspension(driver),
        'status_history': _audit_rows(driver, STATUS_ACTIONS),
        'notes': [
            {'id': n.id, 'body': n.body, 'created_at': _iso(n.created_at),
             'created_by': _staff(n.author)}
            for n in DriverNote.objects.filter(driver=driver).select_related('author')
            .order_by('-created_at')
        ],
        'approval_requests': [
            {'id': r.id, 'action_key': r.action_key, 'status': r.status,
             'requested_at': _iso(r.requested_at)}
            for r in ApprovalRequest.objects.filter(target_type=TARGET, target_id=str(driver.id))
            .order_by('-id')[:20]
        ],
        'audit_trail': _audit_rows(driver),
        'actions': allowed_actions(request.user, driver, blockers),
    }


# ---------------------------------------------------------------------------
# Write plumbing
# ---------------------------------------------------------------------------

def _not_found(driver_id):
    return error_response(code='NOT_FOUND', message='Driver not found.', field='driver_id',
                          issue=f'No driver {driver_id}', status=status.HTTP_404_NOT_FOUND)


def _conflict(code, message, field='general'):
    return error_response(code=code, message=message, field=field, issue=message,
                          status=status.HTTP_409_CONFLICT)


def _driver_for_write(request, driver_id, *permissions):
    """(driver, None) or (None, error response).

    Out of scope, or an approved driver to a KYC-only caller, is NOT_FOUND;
    in view but without any of `permissions` at the driver's city is 403.
    """
    driver = scoped_or_none(request.user, for_kyc_view(request.user, Driver.objects.select_related(
        'user_id', 'home_city', 'active_vehicle')), id=driver_id)
    if driver is None:
        return None, _not_found(driver_id)
    node = node_of(driver)
    if not any(granted(request.user, p, node)[0] for p in permissions):
        return None, error_response(
            code='PERMISSION_DENIED',
            message=f'You do not hold {" or ".join(permissions)} for this driver\'s city.',
            field='driver_id', issue=permissions[0], status=status.HTTP_403_FORBIDDEN)
    return driver, None


def _locked(driver_id):
    return Driver.objects.select_for_update().select_related('user_id').get(pk=driver_id)


def _snapshot(driver):
    return {'approved': driver.approved, 'status': driver.status,
            'doc_status': driver.doc_status}


def _take_off_the_road(driver_id):
    """Drop the driver from the dispatch geo index, as going offline does.

    The database gate (approved=False) is what refuses them; this only stops
    offers being computed for a socket that is still open. Best effort: Redis
    being down must not undo a suspension.
    """
    def drop():
        try:
            from servers.redis_client import remove_driver
            remove_driver(driver_id)
        except Exception:  # noqa: BLE001
            logger.warning('suspend: could not drop driver %s from the geo index', driver_id)
    transaction.on_commit(drop)


def _sync_driver_doc_state(driver):
    """Write the per-document outcome where the driver app reads it.

    `doc_status` / `doc_rejection_reason` are the driver's view: any rejected
    current document makes the driver `rejected` with every reason listed; a
    driver with none left is back to `pending` (approval is a separate step).
    """
    rejected = rejected_documents(driver)
    fields = {}
    if rejected:
        reason = '; '.join(f"{r['label']}: {r['review']['reason']}" for r in rejected)
        fields = {'doc_status': 'rejected', 'doc_rejection_reason': reason}
    elif driver.doc_status == 'rejected':
        fields = {'doc_status': 'pending', 'doc_rejection_reason': ''}
    if fields and any(getattr(driver, k) != v for k, v in fields.items()):
        for key, value in fields.items():
            setattr(driver, key, value)
        driver.doc_status_updated_at = timezone.now()
        driver.save(update_fields=[*fields, 'doc_status_updated_at'])


# ---------------------------------------------------------------------------
# Per-document review
# ---------------------------------------------------------------------------

def _document_target(driver, data):
    """(owner, column, label, vehicle) for the document named in `data`."""
    key = _text(data, 'document_key', required=True, limit=32)
    if key in compliance.DRIVER_DOCUMENT_COLUMNS:
        return driver, compliance.DRIVER_DOCUMENT_COLUMNS[key], key, None
    if key not in compliance.VEHICLE_DOCUMENT_COLUMNS:
        raise BadInput('INVALID_DOCUMENT', f'Unknown document {key!r}.', 'document_key')
    raw = data.get('vehicle_id')
    vehicle_id = _int(raw, 'vehicle_id') if raw not in (None, '') else driver.active_vehicle_id
    vehicle = Vehicle.objects.filter(id=vehicle_id, driver_id=driver).first() if vehicle_id else None
    if vehicle is None:
        raise BadInput('VEHICLE_REQUIRED', 'vehicle_id must name one of this driver\'s vehicles.',
                       'vehicle_id')
    return vehicle, compliance.VEHICLE_DOCUMENT_COLUMNS[key], key, vehicle


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.kyc')])
def admin_review_document(request, driver_id):
    """POST /driver/admin/<id>/documents/review/
    {document_key, vehicle_id?, decision: approve|reject, reason (required on reject)}

    The reason is what the driver reads. Repeating the same decision on the
    same file is a no-op (200, no audit row).
    """
    driver, refusal = _driver_for_write(request, driver_id, 'driver.kyc')
    if refusal:
        return refusal
    try:
        owner, column, key, vehicle = _document_target(driver, request.data)
        decision = _text(request.data, 'decision', required=True, limit=10)
        if decision not in ('approve', 'reject'):
            raise BadInput('INVALID_DECISION', 'decision must be approve or reject.', 'decision')
        reason = _text(request.data, 'reason', required=decision == 'reject', limit=REASON_LIMIT)
    except BadInput as exc:
        return exc.response()
    state = DriverDocumentReview.APPROVED if decision == 'approve' else DriverDocumentReview.REJECTED
    if decision == 'approve':
        reason = ''
    if not _has_file(owner, column):
        return _conflict('DOCUMENT_MISSING', 'There is no file on record to review.',
                         'document_key')
    file_name = getattr(owner, column).name

    with transaction.atomic():
        driver = _locked(driver.id)
        latest = (DriverDocumentReview.objects.filter(driver=driver, vehicle=vehicle,
                                                      document_key=key)
                  .order_by('-reviewed_at', '-id').first())
        if (latest is not None and latest.file_name == file_name and latest.state == state
                and latest.reason == reason):
            return success_response({'driver_id': driver.id, 'review_id': latest.id,
                                     'state': state, 'created': False}, status.HTTP_200_OK)
        before = _snapshot(driver)
        review = DriverDocumentReview.objects.create(
            driver=driver, vehicle=vehicle, document_key=key, file_name=file_name,
            state=state, reason=reason, reviewed_by=request.user)
        _sync_driver_doc_state(driver)
        record_admin_action(
            request, action='driver_document_reviewed', target_type=TARGET,
            target_id=driver.id, before={**before, 'document_review': latest.state if latest else None},
            after={**_snapshot(driver), 'document_key': key, 'vehicle_id': getattr(vehicle, 'id', None),
                   'state': state, 'review_id': review.id},
            reason=reason or f'{compliance.document_label(key)} approved')
    return success_response({'driver_id': driver.id, 'review_id': review.id, 'state': state,
                             'doc_status': driver.doc_status, 'created': True},
                            status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
def admin_approve_driver(request, driver_id):
    """POST /driver/admin/<id>/approve/  {note?}

    `driver.kyc` or `driver.write` at the driver's city. Refused while
    suspended, while a document is rejected, while the country's checklist
    is incomplete, and by `KYCApprovalSerializer` (name, licence, vehicle).
    Approving an approved driver is a no-op.
    """
    driver, refusal = _driver_for_write(request, driver_id, 'driver.kyc', 'driver.write')
    if refusal:
        return refusal
    try:
        note = _text(request.data, 'note', limit=2000)
    except BadInput as exc:
        return exc.response()
    from .serializers import KYCApprovalSerializer

    with transaction.atomic():
        driver = _locked(driver.id)
        if driver.approved and driver.doc_status == 'approved':
            return success_response({'driver_id': driver.id, 'approved': True,
                                     'changed': False}, status.HTTP_200_OK)
        blockers = approval_blockers(driver)
        hard = [b for b in blockers if b['code'] != 'KYC_GATE']
        if hard:
            code = hard[0]['code']
            return error_response(
                code=code, message=hard[0]['message'], field='general',
                issue='; '.join(b['message'] for b in blockers),
                status=status.HTTP_409_CONFLICT if code == 'DRIVER_SUSPENDED'
                else status.HTTP_400_BAD_REQUEST)
        serializer = KYCApprovalSerializer(driver, data={'approved': True}, partial=True)
        if not serializer.is_valid():
            return error_response(
                code='VALIDATION_ERROR', message='The driver cannot be approved yet.',
                field=list(serializer.errors.keys())[0], issue=str(serializer.errors),
                status=status.HTTP_400_BAD_REQUEST)
        before = _snapshot(driver)
        serializer.save()
        driver.doc_status = 'approved'
        driver.doc_rejection_reason = ''
        driver.doc_status_updated_at = timezone.now()
        driver.save(update_fields=['doc_status', 'doc_rejection_reason', 'doc_status_updated_at'])
        record_admin_action(request, action='driver_approved', target_type=TARGET,
                            target_id=driver.id, before=before, after=_snapshot(driver),
                            reason=note or 'KYC approved')
    return success_response({'driver_id': driver.id, 'approved': True, 'changed': True},
                            status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Suspend / reinstate
# ---------------------------------------------------------------------------

def operator_suspend(driver_id, *, reason_code, note='', actor=None, http_request=None):
    """Suspend one driver. Returns (outcome, suspension):
    'suspended' | 'already_suspended' | 'not_found'."""
    with transaction.atomic():
        try:
            driver = _locked(driver_id)
        except Driver.DoesNotExist:
            return 'not_found', None
        existing = DriverSuspension.objects.filter(driver=driver, lifted_at__isnull=True).first()
        if existing is not None:
            return 'already_suspended', existing
        before = _snapshot(driver)
        try:
            with transaction.atomic():
                row = DriverSuspension.objects.create(
                    driver=driver, reason_code=reason_code, note=note,
                    previous_approved=driver.approved, created_by=actor)
        except IntegrityError:
            return 'already_suspended', DriverSuspension.objects.filter(
                driver=driver, lifted_at__isnull=True).first()
        driver.approved = False
        driver.status = 'blocked'
        driver.save(update_fields=['approved', 'status'])
        _take_off_the_road(driver.id)
        record_admin_action(http_request, action='driver_suspended', target_type=TARGET,
                            target_id=driver.id, before=before,
                            after={**_snapshot(driver), 'suspension_id': row.id,
                                   'reason_code': reason_code},
                            reason=note or SUSPEND_REASONS.get(reason_code, reason_code))
    return 'suspended', row


def operator_reinstate(driver_id, *, note='', actor=None, http_request=None):
    """Lift a suspension. Returns (outcome, details):
    'reinstated' | 'not_suspended' | 'permanent' | 'not_found'.

    Approval comes back only if the driver was approved before AND still
    passes the KYC gate today; otherwise they return to the KYC queue.
    """
    from .serializers import KYCApprovalSerializer

    with transaction.atomic():
        try:
            driver = _locked(driver_id)
        except Driver.DoesNotExist:
            return 'not_found', None
        row = (DriverSuspension.objects.select_for_update()
               .filter(driver=driver, lifted_at__isnull=True).first())
        if row is None:
            return 'not_suspended', {'approved': driver.approved}
        if row.reason_code == 'deactivated':
            return 'permanent', {'approved': driver.approved}
        before = _snapshot(driver)
        row.lifted_at, row.lifted_by, row.lift_note = timezone.now(), actor, note
        row.save(update_fields=['lifted_at', 'lifted_by', 'lift_note'])
        restored = False
        if row.previous_approved:
            restored = KYCApprovalSerializer(driver, data={'approved': True},
                                             partial=True).is_valid()
        driver.approved = restored
        driver.status = 'off'
        driver.save(update_fields=['approved', 'status'])
        record_admin_action(http_request, action='driver_reinstated', target_type=TARGET,
                            target_id=driver.id, before=before,
                            after={**_snapshot(driver), 'suspension_id': row.id,
                                   'approval_restored': restored},
                            reason=note or 'reinstated')
    return 'reinstated', {'approved': restored, 'back_to_review': row.previous_approved
                          and not restored}


def _suspend_params(data):
    reason_code = _text(data, 'reason_code', required=True, limit=32)
    if reason_code not in SUSPEND_REASONS:
        raise BadInput('INVALID_REASON',
                       f'reason_code must be one of {", ".join(SUSPEND_REASONS)}.', 'reason_code')
    note = _text(data, 'note', required=reason_code == 'other', limit=2000)
    return reason_code, note


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.write')])
def admin_suspend_driver(request, driver_id):
    """POST /driver/admin/<id>/suspend/  {reason_code, note}"""
    driver, refusal = _driver_for_write(request, driver_id, 'driver.write')
    if refusal:
        return refusal
    try:
        reason_code, note = _suspend_params(request.data)
    except BadInput as exc:
        return exc.response()
    outcome, row = operator_suspend(driver.id, reason_code=reason_code, note=note,
                                    actor=request.user, http_request=request)
    return success_response(
        {'driver_id': driver.id, 'suspension_id': row.id, 'reason_code': row.reason_code,
         'created': outcome == 'suspended'},
        status.HTTP_201_CREATED if outcome == 'suspended' else status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.write')])
def admin_reinstate_driver(request, driver_id):
    """POST /driver/admin/<id>/reinstate/  {note}"""
    driver, refusal = _driver_for_write(request, driver_id, 'driver.write')
    if refusal:
        return refusal
    try:
        note = _text(request.data, 'note', required=True, limit=2000)
    except BadInput as exc:
        return exc.response()
    outcome, details = operator_reinstate(driver.id, note=note, actor=request.user,
                                          http_request=request)
    if outcome == 'permanent':
        return _conflict('DEACTIVATED', 'This driver was permanently deactivated.')
    return success_response({'driver_id': driver.id, 'changed': outcome == 'reinstated',
                             **(details or {})}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Note
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
def admin_note_driver(request, driver_id):
    """POST /driver/admin/<id>/note/  {body, idempotency_key?}  Internal only.
    `driver.kyc` or `driver.write` at the driver's city."""
    driver, refusal = _driver_for_write(request, driver_id, 'driver.kyc', 'driver.write')
    if refusal:
        return refusal
    try:
        body = _text(request.data, 'body', required=True, limit=5000)
        key = _idempotency_key(request, required=False)
    except BadInput as exc:
        return exc.response()
    scoped_key = f'driver_note:{driver.id}:{key}' if key else None
    existing = DriverNote.objects.filter(idempotency_key=scoped_key).first() if scoped_key else None
    if existing is None:
        try:
            with transaction.atomic():
                note = DriverNote.objects.create(driver=driver, body=body, author=request.user,
                                                 idempotency_key=scoped_key)
        except IntegrityError:
            existing = DriverNote.objects.filter(idempotency_key=scoped_key).first()
    if existing is not None:
        return success_response({'driver_id': driver.id, 'note_id': existing.id,
                                 'created': False}, status.HTTP_200_OK)
    record_admin_action(request, action='driver_note_added', target_type=TARGET,
                        target_id=driver.id, after={'note_id': note.id, 'body': body},
                        reason='internal note')
    return success_response({'driver_id': driver.id, 'note_id': note.id, 'created': True},
                            status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.write')])
def admin_vehicle_status(request, driver_id, vehicle_id):
    """POST /driver/admin/<id>/vehicles/<vehicle_id>/status/  {status: active|inactive, note}

    Deactivating the driver's active vehicle also clears it as active, so
    the accept path's "no active vehicle" check refuses rides on it.
    """
    driver, refusal = _driver_for_write(request, driver_id, 'driver.write')
    if refusal:
        return refusal
    try:
        wanted = _text(request.data, 'status', required=True, limit=16)
        if wanted not in ('active', 'inactive'):
            raise BadInput('INVALID_STATUS', 'status must be active or inactive.', 'status')
        note = _text(request.data, 'note', limit=2000)
    except BadInput as exc:
        return exc.response()
    with transaction.atomic():
        driver = _locked(driver.id)
        vehicle = Vehicle.objects.select_for_update().filter(id=vehicle_id,
                                                             driver_id=driver).first()
        if vehicle is None:
            return error_response(code='NOT_FOUND', message='Vehicle not found.',
                                  field='vehicle_id', issue=f'No vehicle {vehicle_id}',
                                  status=status.HTTP_404_NOT_FOUND)
        if vehicle.status == wanted:
            return success_response({'driver_id': driver.id, 'vehicle_id': vehicle.id,
                                     'status': wanted, 'changed': False}, status.HTTP_200_OK)
        before = {'vehicle_status': vehicle.status, 'active_vehicle_id': driver.active_vehicle_id}
        vehicle.status = wanted
        vehicle.save(update_fields=['status'])
        if wanted == 'inactive' and driver.active_vehicle_id == vehicle.id:
            driver.active_vehicle = None
            driver.save(update_fields=['active_vehicle'])
        elif wanted == 'active' and driver.active_vehicle_id is None:
            driver.active_vehicle = vehicle
            driver.save(update_fields=['active_vehicle'])
        record_admin_action(
            request, action='driver_vehicle_activated' if wanted == 'active'
            else 'driver_vehicle_deactivated', target_type=TARGET, target_id=driver.id,
            before={**before, 'vehicle_id': vehicle.id},
            after={'vehicle_id': vehicle.id, 'vehicle_status': wanted,
                   'active_vehicle_id': driver.active_vehicle_id},
            reason=note or f'vehicle {wanted}')

    def forget():
        try:
            from servers.redis_client import invalidate_driver_vehicle_type
            invalidate_driver_vehicle_type(driver.id)
        except Exception:  # noqa: BLE001
            logger.warning('vehicle status: could not refresh vehicle type cache %s', driver.id)
    transaction.on_commit(forget)
    return success_response({'driver_id': driver.id, 'vehicle_id': vehicle.id, 'status': wanted,
                             'active_vehicle_id': driver.active_vehicle_id, 'changed': True},
                            status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Permanent deactivation (replaces the hard delete)
# ---------------------------------------------------------------------------

def execute_deactivate(request_row):
    """Executor for `driver.deactivate`: suspend for good and lock the account.

    Keeps every row (trips, earnings, payouts and the audit trail all point
    at the driver), unlike the `driver.delete()` it replaces.
    """
    from django.contrib.auth import get_user_model

    payload = request_row.payload or {}
    driver_id = payload.get('driver_id')
    maker = get_user_model().objects.filter(pk=request_row.maker_id).first()
    with transaction.atomic():
        driver = Driver.objects.select_for_update().select_related('user_id').filter(
            pk=driver_id).first()
        if driver is None:
            raise registry.ExecutionRefused('NOT_FOUND', f'No driver {driver_id}.')
        user = driver.user_id
        current = DriverSuspension.objects.filter(driver=driver, lifted_at__isnull=True).first()
        if current is not None and current.reason_code == 'deactivated' and not user.is_active:
            raise registry.ExecutionRefused('ALREADY_DEACTIVATED', 'Already deactivated.')
        before = {**_snapshot(driver), 'is_active': user.is_active}
        previous = driver.approved
        if current is not None:
            previous = current.previous_approved
            current.lifted_at, current.lifted_by = timezone.now(), maker
            current.lift_note = 'superseded by permanent deactivation'
            current.save(update_fields=['lifted_at', 'lifted_by', 'lift_note'])
        DriverSuspension.objects.create(driver=driver, reason_code='deactivated',
                                        note=payload.get('note') or '',
                                        previous_approved=previous, created_by=maker)
        driver.approved, driver.status = False, 'blocked'
        driver.save(update_fields=['approved', 'status'])
        user.is_active = False
        user.save(update_fields=['is_active'])
        _take_off_the_road(driver.id)
        record_admin_action(None, action='driver_deactivated', target_type=TARGET,
                            target_id=driver.id, before=before,
                            after={**_snapshot(driver), 'is_active': False,
                                   'approval_request_id': request_row.id},
                            reason=payload.get('note') or 'permanent deactivation')
    return {'driver_id': driver.id, 'deactivated': True}


registry.register(DRIVER_DEACTIVATE)(execute_deactivate)


def deactivate_response(request, driver_id):
    """Body of DELETE /driver/admin/<id>/delete/: a `driver.deactivate`
    request (always a checker in India), never a hard delete."""
    driver, refusal = _driver_for_write(request, driver_id, 'driver.deactivate')
    if refusal:
        return refusal
    try:
        data = request.data if hasattr(request, 'data') else {}
        note = _text(data, 'reason', limit=2000) or _text(data, 'note', limit=2000)
    except BadInput as exc:
        return exc.response()
    if not driver.user_id.is_active:
        return _conflict('ALREADY_DEACTIVATED', 'This driver is already deactivated.')
    try:
        outcome = approvals.require_approval_or_execute(
            DRIVER_DEACTIVATE, actor=request.user, country=_country(driver),
            scope_node=node_of(driver), target_type=TARGET, target_id=str(driver.id),
            payload={'driver_id': driver.id, 'note': note},
            idempotency_key=f'driver_deactivate:{driver.id}',
            reason=note or 'permanent deactivation', http_request=request)
    except approvals.ApprovalError as exc:
        return exc.response()
    return _approval_answer(outcome, {'driver_id': driver.id})


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

def execute_bulk(request_row):
    """Run a bulk suspend or reinstate row by row; returns per-row results."""
    from django.contrib.auth import get_user_model

    p = request_row.payload or {}
    action = p.get('action')
    if action not in BULK_ACTIONS:
        raise registry.ExecutionRefused('UNKNOWN_BULK_ACTION', f'Unknown action {action!r}.')
    maker = get_user_model().objects.filter(pk=request_row.maker_id).first()
    results = []
    for driver_id in p.get('driver_ids') or []:
        if action == 'suspend':
            outcome, _row = operator_suspend(driver_id, reason_code=p.get('reason_code'),
                                             note=p.get('note') or '', actor=maker)
        else:
            outcome, _row = operator_reinstate(driver_id, note=p.get('note') or '', actor=maker)
        results.append({'id': driver_id, 'result': outcome})
    for driver_id in p.get('not_found_ids') or []:
        results.append({'id': driver_id, 'result': 'not_found'})
    counts = {}
    for row in results:
        counts[row['result']] = counts.get(row['result'], 0) + 1
    return {'action': action, 'results': results, 'counts': counts}


bulk.register('driver_bulk')(execute_bulk)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.write')])
def admin_bulk_drivers(request):
    """POST /driver/admin/bulk/
    {action: suspend|reinstate, driver_ids, reason_code, note, idempotency_key}

    Up to the bulk.action limit (50 records in India) it runs now with
    per-row results; above it a checker must approve (202). Drivers the
    caller cannot write are reported not_found, as a single write would.
    """
    data = request.data
    try:
        action = _text(data, 'action', required=True, limit=16)
        if action not in BULK_ACTIONS:
            raise BadInput('INVALID_ACTION', f'action must be one of {", ".join(BULK_ACTIONS)}.',
                           'action')
        raw_ids = data.get('driver_ids')
        if not isinstance(raw_ids, list) or not raw_ids:
            raise BadInput('DRIVER_IDS_REQUIRED', 'driver_ids must be a non-empty list.',
                           'driver_ids')
        if len(raw_ids) > BULK_MAX_IDS:
            raise BadInput('TOO_MANY', f'At most {BULK_MAX_IDS} drivers per bulk action.',
                           'driver_ids')
        ids = list(dict.fromkeys(_int(raw, 'driver_ids') for raw in raw_ids))
        params = {'action': action}
        if action == 'suspend':
            reason_code, note = _suspend_params(data)
            params.update(reason_code=reason_code, note=note)
        else:
            params.update(note=_text(data, 'note', required=True, limit=2000))
        key = _idempotency_key(request)
    except BadInput as exc:
        return exc.response()

    visible = (for_kyc_view(request.user, scoped(request.user, Driver.objects.select_related(
        'home_city', 'user_id'))).filter(id__in=ids))
    writable = {d.id: d for d in visible if granted(request.user, 'driver.write', node_of(d))[0]}
    ordered = [i for i in ids if i in writable]
    not_found = [i for i in ids if i not in writable]
    if not ordered:
        return success_response({'state': 'executed', 'count': 0, 'result': {
            'action': action, 'results': [{'id': i, 'result': 'not_found'} for i in ids],
            'counts': {'not_found': len(ids)}}}, status.HTTP_200_OK)
    countries = {_country(d) for d in writable.values()}
    if len(countries) != 1:
        return error_response(
            code='MIXED_COUNTRIES', message='A bulk action covers one country at a time.',
            field='driver_ids', issue=', '.join(sorted(countries)),
            status=status.HTTP_400_BAD_REQUEST)
    nodes = {node_of(d) for d in writable.values()}
    try:
        outcome = approvals.require_approval_or_execute(
            BULK_ACTION, actor=request.user, country=countries.pop(), amount=len(ordered),
            scope_node=nodes.pop() if len(nodes) == 1 else None,
            target_type='driver_bulk', target_id=f'{action}:{len(ordered)}',
            payload={**params, 'driver_ids': ordered, 'not_found_ids': not_found},
            idempotency_key=f'driver_bulk:{key}',
            reason=params.get('note') or params.get('reason_code') or '',
            http_request=request,
        )
    except approvals.ApprovalError as exc:
        return exc.response()
    return _approval_answer(outcome, {'count': len(ordered)})
