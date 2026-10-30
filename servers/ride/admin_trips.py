"""Trips module (operator console, module 2): list, detail extras, writes.

WHAT LIVES HERE
---------------
* The list's server-side filters, sort and stale rule (the route and its
  permission stay in `admin_views.admin_list_trips`, so the console's URL
  is unchanged).
* The detail's extra sections: timeline events, fare in minor units,
  payment and payout summary, tickets, flags, notes, money actions, the
  trip's audit trail and the actions the caller may take.
* The writes: cancel, refund, adjust-fare, flag, unflag, note, bulk.
* The maker-checker executors for `support.refund`, `finance.refund`,
  `finance.adjust` and `bulk.action`, registered with the approvals
  registry on import (`RideConfig.ready` imports this module).

RULES THIS FILE KEEPS
---------------------
* Every write is gated by `ScopedPermission`, re-checked at the trip's own
  place (a trip outside scope answers 404, like every object), audited with
  `record_admin_action`, and idempotent.
* Money is integer minor units of the trip's currency. Nothing here prices
  anything: refund and adjustment amounts are entered by the operator,
  checked against what the trip was paid, and gated by the approval
  thresholds. Operator cancellation never charges a fee.
* An in-progress trip cannot be cancelled here. What the rider owes for a
  ride that already started is an undecided money policy (the frozen
  `stale_rides` screen refused it for the same reason).
"""

import logging
from datetime import datetime, time, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from base.money import from_minor
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted, node_of
from servers.admin_access.scoping import scoped, scoped_or_none
from servers.admin_audit.services import record_admin_action
from servers.approvals import adjust, bulk, registry
from servers.approvals import service as approvals
from servers.approvals.actions import (
    BULK_ACTION, FINANCE_ADJUST, FINANCE_REFUND, SUPPORT_REFUND,
)
from servers.approvals.models import EXECUTED, FAILED, REQUESTED

from .models import Trip, TripFlag, TripMoneyAction, TripNote, TripStatus

logger = logging.getLogger(__name__)

TARGET = 'trip'

ACTIVE_STATUSES = ('requested', 'accepted', 'reached', 'in_progress')
#: Where an operator may cancel. Not in_progress: see the module docstring.
CANCELLABLE_STATUSES = ('requested', 'accepted', 'reached')

CANCEL_REASONS = {
    'rider_request': 'Rider asked support to cancel',
    'driver_request': 'Driver asked support to cancel',
    'no_driver': 'No driver is coming',
    'stale': 'Trip is stale (no progress)',
    'safety': 'Safety concern',
    'fraud_suspicion': 'Suspected fraud',
    'duplicate': 'Duplicate booking',
    'other': 'Other (note required)',
}
FLAG_REASONS = dict(TripFlag.REASON_CHOICES)
BULK_ACTIONS = ('cancel', 'flag')
BULK_MAX_IDS = 500

SORTS = {
    'requested_at': 'requested_at',
    'completed_at': 'completed_at',
    'fare': 'estimated_fare_minor',
    'status': 'status_id__status_code',
    'id': 'id',
}
DEFAULT_SORT = '-requested_at'
TRUE = {'1', 'true', 'yes', 'on'}
FALSE = {'0', 'false', 'no', 'off'}


class BadInput(Exception):
    def __init__(self, code, message, field):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field

    def response(self):
        return error_response(code=self.code, message=self.message, field=self.field,
                              issue=self.message, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if value else None


def _csv(value):
    return [part.strip() for part in str(value or '').split(',') if part.strip()]


def _int(value, field):
    try:
        if isinstance(value, bool):
            raise ValueError
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise BadInput('INVALID_FILTER', f'{field} must be an integer.', field)


def _bool(params, name):
    raw = params.get(name)
    if raw is None or raw == '':
        return None
    raw = str(raw).strip().lower()
    if raw in TRUE:
        return True
    if raw in FALSE:
        return False
    raise BadInput('INVALID_FILTER', f'{name} must be true or false.', name)


def _date(params, name):
    raw = params.get(name)
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        raise BadInput('INVALID_DATE', f'{name} must be YYYY-MM-DD.', name)


def _minor_amount(value, field='amount_minor'):
    """An integer of minor units from a JSON body; never a float or a string."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadInput('AMOUNT_NOT_INTEGER',
                       f'{field} must be an integer number of minor units.', field)
    return value


def _staff(user):
    if user is None:
        return None
    return {
        'id': user.id,
        'label': getattr(user, 'full_name', None) or getattr(user, 'email', None) or '',
        'role': getattr(user, 'role', '') or '',
    }


def trip_tz(trip):
    from base.market_time import trip_timezone

    return trip_timezone(trip)


def trip_country(trip):
    """ISO country of the trip's city: the scope tree first, the zone's column after."""
    zone = trip.zone
    if zone is None:
        return None
    node = zone.geo_node
    if node is not None:
        parts = [p for p in str(node.path).split('/') if p]
        if len(parts) > 1:
            return parts[1]
    return (zone.country or '').upper() or None


def charged_minor(trip):
    """What the trip charged: the promo fare when one was applied, else the quote.

    The same rule as `base.money.charged_fare()`, for one row.
    """
    if trip.final_fare_minor is not None:
        return trip.final_fare_minor
    return trip.estimated_fare_minor or 0


def stale_q(now=None):
    """Requested with no acceptance past TRIP_STALE_AFTER_SECONDS, accepted with
    no arrival past TRIP_OPERATOR_ATTENTION_AFTER_SECONDS, or an active trip
    the liveness detector has flagged. The detector's own settings."""
    from servers.ride.liveness import attention_after_seconds, stale_after_seconds

    now = now or timezone.now()
    stale_cutoff = now - timedelta(seconds=stale_after_seconds())
    attention_cutoff = now - timedelta(seconds=attention_after_seconds())
    return (
        Q(status_id__status_code='requested', accepted_at__isnull=True,
          requested_at__lt=stale_cutoff)
        | Q(status_id__status_code='accepted', reached_at__isnull=True,
            accepted_at__lt=attention_cutoff)
        | Q(status_id__status_code__in=ACTIVE_STATUSES, stale_flagged_at__isnull=False)
    )


def stale_reason(trip, now=None):
    """Why a trip is stale, or '' when it is not. Mirrors `stale_q`."""
    from servers.ride.liveness import attention_after_seconds, stale_after_seconds

    now = now or timezone.now()
    code = trip.status_id.status_code if trip.status_id else ''
    if (code == 'requested' and trip.accepted_at is None and trip.requested_at
            and (now - trip.requested_at).total_seconds() > stale_after_seconds()):
        return 'no_acceptance'
    if (code == 'accepted' and trip.reached_at is None and trip.accepted_at
            and (now - trip.accepted_at).total_seconds() > attention_after_seconds()):
        return 'no_arrival'
    if code in ACTIVE_STATUSES and trip.stale_flagged_at is not None:
        return trip.stale_reason or 'driver_silent'
    return ''


def _date_range_q(queryset, date_from, date_to):
    """requested_at within [date_from, date_to] in EACH trip's own city day.

    Zones are grouped by time zone, so a Hyderabad trip is cut at Indian
    midnight and an Austin trip at Central midnight in the same query. A
    trip with no zone is cut in the deployment's time zone.
    """
    from base.market_time import _zoneinfo, zone_timezone
    from servers.pricing.models import ServiceZone

    def bounds(tz):
        q = Q()
        if date_from:
            q &= Q(requested_at__gte=datetime.combine(date_from, time.min, tzinfo=tz))
        if date_to:
            q &= Q(requested_at__lt=datetime.combine(
                date_to + timedelta(days=1), time.min, tzinfo=tz))
        return q

    zone_ids = set(queryset.exclude(zone=None).values_list('zone_id', flat=True).distinct())
    by_tz = {}
    for zone in ServiceZone.objects.filter(id__in=zone_ids):
        tz = zone_timezone(zone)
        by_tz.setdefault(str(tz), (tz, []))[1].append(zone.id)
    combined = Q(zone__isnull=True) & bounds(_zoneinfo(None))
    for tz, ids in by_tz.values():
        combined |= Q(zone_id__in=ids) & bounds(tz)
    return combined


def annotate_markers(queryset):
    from servers.sos.models import SOSEvent
    from servers.support.models import SupportTicket

    return queryset.annotate(
        has_sos=Exists(SOSEvent.objects.filter(trip=OuterRef('pk'))),
        has_ticket=Exists(SupportTicket.objects.filter(trip_id=OuterRef('pk'))),
        flagged=Exists(TripFlag.objects.filter(trip=OuterRef('pk'), cleared_at__isnull=True)),
    )


def filter_trips(params, queryset, now=None):
    """Apply the list's filters and sort. Raises BadInput on a malformed value."""
    statuses = _csv(params.get('status'))
    if statuses:
        queryset = queryset.filter(status_id__status_code__in=statuses)

    trip_id = params.get('id') or params.get('trip_id')
    if trip_id:
        queryset = queryset.filter(id=_int(trip_id, 'id'))

    driver_id = params.get('driver_id')
    if driver_id:
        queryset = queryset.filter(driver_id=_int(driver_id, 'driver_id'))

    rider_id = params.get('rider_id') or params.get('user_id')
    if rider_id:
        queryset = queryset.filter(user_id=_int(rider_id, 'rider_id'))

    methods = _csv(params.get('payment_method'))
    if methods:
        queryset = queryset.filter(payment_method__in=methods)

    vehicle_types = _csv(params.get('vehicle_type'))
    if vehicle_types:
        queryset = queryset.filter(
            Q(requested_vehicle_type__type__in=vehicle_types)
            | Q(vehicle_id__vehicle_type_id__type__in=vehicle_types)
        )

    date_from, date_to = _date(params, 'date_from'), _date(params, 'date_to')
    if date_from and date_to and date_from > date_to:
        raise BadInput('INVALID_DATE', 'date_from is after date_to.', 'date_from')
    if date_from or date_to:
        queryset = queryset.filter(_date_range_q(queryset, date_from, date_to))

    for name in ('has_sos', 'has_ticket', 'flagged'):
        wanted = _bool(params, name)
        if wanted is not None:
            queryset = queryset.filter(**{name: wanted})

    stale = _bool(params, 'stale')
    if stale is True:
        queryset = queryset.filter(stale_q(now))
    elif stale is False:
        queryset = queryset.exclude(stale_q(now))

    ordering = (params.get('ordering') or DEFAULT_SORT).strip()
    key = ordering.lstrip('-')
    if key not in SORTS:
        raise BadInput(
            'INVALID_FILTER',
            f'ordering must be one of {", ".join(sorted(SORTS))} (prefix - for descending).',
            'ordering')
    prefix = '-' if ordering.startswith('-') else ''
    return queryset.order_by(f'{prefix}{SORTS[key]}', f'{prefix}id')


def _city(trip):
    zone = trip.zone
    if zone is None:
        return None
    node = zone.geo_node
    return {
        'code': node.code if node else zone.code,
        'name': (node.name if node else None) or zone.city or zone.name,
        'timezone': str(trip_tz(trip)),
        'country': trip_country(trip),
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class TripPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def list_response(request, queryset):
    """Body of GET /ride/admin/trips/ for an already SCOPED queryset (the view
    keeps the gate, the scope call and the URL the console uses)."""
    from .serializers import TripListSerializer

    try:
        queryset = filter_trips(request.query_params, queryset)
    except BadInput as exc:
        return exc.response()

    paginator = TripPagination()
    page = paginator.paginate_queryset(queryset, request)
    rows = TripListSerializer(page, many=True).data
    now = timezone.now()
    results = []
    for trip, row in zip(page, rows, strict=True):
        row = dict(row)
        reason = stale_reason(trip, now)
        row.update({
            'rider_id': trip.user_id_id,
            'driver_id': trip.driver_id_id,
            'estimated_fare_minor': trip.estimated_fare_minor,
            'final_fare_minor': trip.final_fare_minor,
            'charged_fare_minor': charged_minor(trip),
            'city': _city(trip),
            'has_sos': bool(getattr(trip, 'has_sos', False)),
            'has_ticket': bool(getattr(trip, 'has_ticket', False)),
            'flagged': bool(getattr(trip, 'flagged', False)),
            'stale': bool(reason),
            'stale_reason': reason or None,
            'cancelled_by': trip.cancelled_by or None,
        })
        results.append(row)

    django_page = paginator.page
    return success_response({
        'count': django_page.paginator.count,
        'next': paginator.get_next_link(),
        'previous': paginator.get_previous_link(),
        'page': django_page.number,
        'page_size': paginator.get_page_size(request),
        'total_pages': django_page.paginator.num_pages,
        'ordering': (request.query_params.get('ordering') or DEFAULT_SORT),
        'results': results,
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Money on a trip
# ---------------------------------------------------------------------------

def paid_minor(trip):
    """What the rider paid for this trip, in its currency.

    Completed Payment rows when there are any; otherwise, for a trip whose
    payment_status says it was paid (cash, wallet), what it charged.
    """
    from servers.payments.models import Payment

    total = (Payment.objects.filter(trip_id=trip, status='completed', currency=trip.currency)
             .aggregate(total=Sum('amount_minor'))['total'])
    if total:
        return int(total)
    if (trip.payment_status or '').lower() in ('completed', 'paid', 'success'):
        return int(charged_minor(trip))
    return 0


def refunded_minor(trip, exclude_request_id=None):
    qs = TripMoneyAction.objects.filter(
        trip=trip, kind=TripMoneyAction.REFUND, currency=trip.currency,
        status__in=(TripMoneyAction.COMPLETED, TripMoneyAction.PENDING_GATEWAY),
    )
    if exclude_request_id is not None:
        qs = qs.exclude(approval_request_id=exclude_request_id)
    return int(qs.aggregate(total=Sum('amount_minor'))['total'] or 0)


def refundable_minor(trip):
    return max(0, paid_minor(trip) - refunded_minor(trip))


def refund_method(trip):
    """Wallet credit for cash and wallet trips; the original method otherwise."""
    method = (trip.payment_method or '').lower()
    if method in ('cash', 'wallet', ''):
        return TripMoneyAction.WALLET_CREDIT
    return TripMoneyAction.GATEWAY


# ---------------------------------------------------------------------------
# Detail extras
# ---------------------------------------------------------------------------

def _timeline_events(trip, audit_rows):
    rider = {'type': 'rider', 'id': trip.user_id_id}
    driver = {'type': 'driver', 'id': trip.driver_id_id} if trip.driver_id_id else None
    events = [{'event': 'requested', 'at': _iso(trip.requested_at), 'actor': rider}]
    for name, at in (('accepted', trip.accepted_at), ('arrived', trip.reached_at),
                     ('started', trip.started_at), ('completed', trip.completed_at)):
        if at:
            events.append({'event': name, 'at': _iso(at), 'actor': driver})
    if trip.cancelled_at:
        by = trip.cancelled_by or 'unknown'
        actor = {'type': by, 'id': (trip.user_id_id if by == 'rider'
                                    else trip.driver_id_id if by == 'driver' else None)}
        events.append({'event': 'cancelled', 'at': _iso(trip.cancelled_at), 'actor': actor,
                       'detail': trip.cancellation_reason or None})
    try:
        from servers.driver.models import DriverCancellation

        for c in DriverCancellation.objects.filter(trip=trip):
            events.append({'event': 'driver_withdrew', 'at': _iso(c.created_at),
                           'actor': {'type': 'driver', 'id': c.driver_id}, 'detail': c.reason})
    except Exception:  # noqa: BLE001 - optional table on older deploys
        pass
    if trip.stale_flagged_at:
        events.append({'event': 'stale_flagged', 'at': _iso(trip.stale_flagged_at),
                       'actor': {'type': 'system', 'id': None},
                       'detail': trip.stale_reason or None})
    for row in audit_rows:
        events.append({'event': row['action'], 'at': row['created_at'],
                       'actor': {'type': 'operator', 'id': (row['created_by'] or {}).get('id')},
                       'created_by': row['created_by'], 'detail': row['reason'] or None})
    events.sort(key=lambda e: e['at'] or '')
    return events


def _fare_minor(trip):
    from .models import FarePricing, Receipt

    fp = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
    receipt = Receipt.objects.filter(trip_id=trip).order_by('-version').first()
    breakdown = None
    if fp is not None:
        breakdown = {
            'base_minor': fp.base_fare_minor,
            'distance_minor': fp.distance_fare_minor,
            'time_minor': fp.time_fare_minor,
            'surge_multiplier': str(fp.surge_multiplier),
            'night_surge_multiplier': (str(fp.night_surge_multiplier)
                                       if fp.night_surge_multiplier is not None else None),
            'gross_minor': fp.gross_fare_minor,
            'promo_discount_minor': fp.discount_amount_minor,
            'promo_code': fp.promo_code or None,
            'total_minor': fp.total_fare_minor,
            'rider_payable_minor': fp.rider_payable_minor,
            'currency': fp.currency,
        }
    return {
        'currency': trip.currency,
        'estimated_minor': trip.estimated_fare_minor,
        'final_minor': trip.final_fare_minor,
        'charged_minor': charged_minor(trip),
        'cancellation_fee_minor': trip.cancellation_fee_minor,
        # GST is stubbed platform-wide (AUDIT decision 6, TODO(CA)): this is
        # what the receipt recorded, not a rate applied here.
        'tax_minor': receipt.gst_amount_minor if receipt is not None else None,
        'tax_stub': True,
        'breakdown': breakdown,
    }


def _payment_summary(trip):
    from servers.payments.models import TripSettlement

    paid = paid_minor(trip)
    refunded = refunded_minor(trip)
    adjustments = int(TripMoneyAction.objects.filter(
        trip=trip, kind=TripMoneyAction.ADJUSTMENT,
    ).aggregate(total=Sum('amount_minor'))['total'] or 0)
    settlement = TripSettlement.objects.filter(trip=trip).order_by('-id').first()
    return {
        'currency': trip.currency,
        'method': trip.payment_method,
        'status': trip.payment_status,
        'paid_minor': paid,
        'refunded_minor': refunded,
        'refundable_minor': max(0, paid - refunded),
        'adjustments_minor': adjustments,
        'payout': {
            'status': 'settled' if settlement else 'unsettled',
            'gross_fare_minor': settlement.gross_fare_minor if settlement else None,
            'commission_minor': settlement.commission_amount_minor if settlement else None,
            'driver_net_minor': settlement.driver_net_minor if settlement else None,
            'settled_at': _iso(settlement.settled_at) if settlement else None,
        },
    }


def _audit_rows(trip):
    from servers.admin_audit.models import AdminAuditLog

    rows = []
    for row in (AdminAuditLog.objects.filter(target_type=TARGET, target_id=str(trip.id))
                .order_by('-created_at', '-id')[:100]):
        rows.append({
            'id': row.id,
            'action': row.action,
            'created_at': _iso(row.created_at),
            'reason': row.reason,
            # Staff attribution (masking.OPERATOR_IDENTITY_KEYS): left whole.
            'created_by': {'id': row.actor_id, 'label': row.actor_label,
                           'role': row.actor_role},
        })
    return rows


def allowed_actions(user, trip, refundable=None):
    """What this caller may do to this trip, so the console shows only that."""
    node = node_of(trip)
    code = trip.status_id.status_code if trip.status_id else ''

    def holds(permission):
        return granted(user, permission, node)[0]

    may_write = holds('trip.write')
    may_refund = holds('support.refund')
    if refundable is None:
        refundable = refundable_minor(trip)
    return {
        'cancel': may_write and code in CANCELLABLE_STATUSES,
        'flag': may_write,
        'note': may_write,
        'refund': may_refund and refundable > 0,
        'refund_action_key': ((FINANCE_REFUND if holds('finance.read') else SUPPORT_REFUND)
                              if may_refund else None),
        'adjust': holds('finance.adjust'),
        'reveal': holds('pii.view'),
        'cancellable_statuses': list(CANCELLABLE_STATUSES),
        'cancel_reasons': CANCEL_REASONS,
        'flag_reasons': FLAG_REASONS,
    }


def detail_extras(request, trip):
    """The sections the Trips module adds to the existing detail payload."""
    from servers.approvals.models import ApprovalRequest
    from servers.support.models import SupportTicket

    audit = _audit_rows(trip)
    payment = _payment_summary(trip)
    reason = stale_reason(trip)

    def dec(value):
        return str(value) if value is not None else None

    return {
        'currency': trip.currency,
        'city': _city(trip),
        'cancelled_by': trip.cancelled_by or None,
        'cancellation_reason': trip.cancellation_reason or None,
        'timeline_events': _timeline_events(trip, audit),
        'offers_recorded': False,  # dispatch offers live in Redis only, never persisted
        'fare_minor': _fare_minor(trip),
        'payment_summary': payment,
        'route': {
            'estimated_distance_km': dec(trip.estimated_distance_km),
            'actual_distance_km': dec(trip.actual_distance_km),
            'estimated_duration_min': dec(trip.estimated_duration_min),
            'actual_duration_min': dec(trip.actual_duration_min),
        },
        'support_tickets': [
            {'id': t.id, 'status': t.status, 'issue_type': t.issue_type,
             'created_at': _iso(t.created_at)}
            for t in SupportTicket.objects.filter(trip_id=trip).order_by('-created_at')
        ],
        'flags': [
            {'id': f.id, 'reason': f.reason, 'note': f.note, 'active': f.cleared_at is None,
             'created_at': _iso(f.created_at), 'created_by': _staff(f.created_by),
             'cleared_at': _iso(f.cleared_at)}
            for f in TripFlag.objects.filter(trip=trip).select_related('created_by')
            .order_by('-created_at')
        ],
        'notes': [
            {'id': n.id, 'body': n.body, 'created_at': _iso(n.created_at),
             'created_by': _staff(n.author)}
            for n in TripNote.objects.filter(trip=trip).select_related('author')
            .order_by('-created_at')
        ],
        'money_actions': [
            {'id': m.id, 'kind': m.kind, 'amount_minor': m.amount_minor,
             'currency': m.currency, 'method': m.method, 'status': m.status,
             'reason': m.reason, 'approval_request_id': m.approval_request_id,
             'created_at': _iso(m.created_at), 'created_by': _staff(m.created_by)}
            for m in TripMoneyAction.objects.filter(trip=trip).select_related('created_by')
            .order_by('-created_at')
        ],
        'approval_requests': [
            {'id': r.id, 'action_key': r.action_key, 'status': r.status,
             'amount': r.amount, 'currency': r.currency,
             'requested_at': _iso(r.requested_at)}
            for r in ApprovalRequest.objects.filter(target_type=TARGET, target_id=str(trip.id))
            .order_by('-id')[:50]
        ],
        'audit_trail': audit,
        'stale': {'is_stale': bool(reason), 'reason': reason or None},
        'actions': allowed_actions(request.user, trip, payment['refundable_minor']),
    }


# ---------------------------------------------------------------------------
# Write plumbing
# ---------------------------------------------------------------------------

def _not_found(trip_id):
    return error_response(code='NOT_FOUND', message='Trip not found.', field='trip_id',
                          issue=f'No trip {trip_id}', status=status.HTTP_404_NOT_FOUND)


def _trip_for_write(request, trip_id, permission):
    """(trip, None) or (None, error response). Out of scope is NOT_FOUND;
    in scope for reading but without this capability at its place is 403."""
    trip = scoped_or_none(request.user, Trip.objects.select_related(
        'status_id', 'zone', 'zone__geo_node', 'user_id', 'driver_id'), id=trip_id)
    if trip is None:
        return None, _not_found(trip_id)
    if not granted(request.user, permission, node_of(trip))[0]:
        return None, error_response(
            code='PERMISSION_DENIED',
            message=f'You do not hold {permission} where this trip happened.',
            field='trip_id', issue=permission, status=status.HTTP_403_FORBIDDEN)
    return trip, None


def _text(data, name, *, required=False, limit=2000):
    value = data.get(name)
    value = '' if value is None else str(value).strip()
    if required and not value:
        raise BadInput(f'{name.upper()}_REQUIRED', f'{name} is required.', name)
    if len(value) > limit:
        raise BadInput('TOO_LONG', f'{name} is longer than {limit} characters.', name)
    return value


def _idempotency_key(request, required=True):
    key = (request.headers.get('Idempotency-Key')
           or request.data.get('idempotency_key')
           or '')
    key = str(key).strip()[:120]
    if required and not key:
        raise BadInput('IDEMPOTENCY_KEY_REQUIRED', 'idempotency_key is required.',
                       'idempotency_key')
    return key


def _trip_snapshot(trip):
    return {
        'status': trip.status_id.status_code if trip.status_id else None,
        'cancelled_at': _iso(trip.cancelled_at),
        'cancelled_by': trip.cancelled_by,
        'cancellation_reason': trip.cancellation_reason,
        'cancellation_fee_minor': trip.cancellation_fee_minor,
    }


def _approval_answer(outcome, extra=None):
    """200 when it ran now, 202 when it waits for a checker, 409 when it failed."""
    row = outcome.request
    body = {'approval_request': row.as_dict(), 'duplicate': outcome.duplicate,
            **(extra or {})}
    if row.status == REQUESTED:
        body['state'] = 'pending_approval'
        return success_response(body, status.HTTP_202_ACCEPTED)
    if row.status == FAILED:
        return error_response(code=row.error_code or 'EXECUTION_FAILED',
                              message=row.error or 'The action could not be carried out.',
                              field='general', issue=row.error or row.error_code,
                              status=status.HTTP_409_CONFLICT)
    body['state'] = 'executed' if row.status == EXECUTED else row.status
    body['result'] = row.result
    return success_response(body, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

def operator_cancel(trip_id, *, reason_code, note='', waive_fee=False, http_request=None):
    """Cancel one trip as an operator. Returns (outcome, trip).

    outcome: 'cancelled' | 'already_cancelled' | 'not_cancellable' | 'not_found'.
    Idempotent: a trip an operator already cancelled answers
    already_cancelled and writes nothing. No fee is charged: a fee already
    on the row is zeroed when `waive_fee` is set, and the flag is audited.
    """
    with transaction.atomic():
        trip = (Trip.objects.select_for_update(of=('self',))
                .select_related('status_id').filter(id=trip_id).first())
        if trip is None:
            return 'not_found', None
        code = trip.status_id.status_code if trip.status_id else ''
        if code == 'cancelled' and trip.cancelled_by == 'admin':
            return 'already_cancelled', trip
        if code not in CANCELLABLE_STATUSES:
            return 'not_cancellable', trip
        before = _trip_snapshot(trip)
        cancelled, _ = TripStatus.objects.get_or_create(
            status_code='cancelled', defaults={'description': 'Trip cancelled'})
        trip.status_id = cancelled
        trip.cancelled_at = timezone.now()
        trip.cancelled_by = 'admin'
        trip.cancellation_reason = (f'{reason_code}: {note}' if note else reason_code)[:255]
        fields = ['status_id', 'cancelled_at', 'cancelled_by', 'cancellation_reason']
        if waive_fee and trip.cancellation_fee_minor:
            trip.cancellation_fee_minor = 0
            fields.append('cancellation_fee_minor')
        trip.save(update_fields=fields)
        record_admin_action(
            http_request, action='trip_cancelled', target_type=TARGET, target_id=trip.id,
            before=before,
            after={**_trip_snapshot(trip), 'reason_code': reason_code,
                   'fee_waived': bool(waive_fee)},
            reason=note or CANCEL_REASONS.get(reason_code, reason_code),
        )
        rider_id, driver_id, pk = trip.user_id_id, trip.driver_id_id, trip.id
        transaction.on_commit(lambda: _after_cancel(pk, rider_id, driver_id))
    return 'cancelled', trip


def _after_cancel(trip_id, rider_id, driver_id):
    """Release the driver and tell both sides. Best effort, outside the lock."""
    from django.contrib.auth import get_user_model

    from servers.rider.models import Notification

    try:
        from servers.redis_client import invalidate_trip
        from servers.ride.dispatch import dismiss_outstanding_offers

        dismiss_outstanding_offers(trip_id, reason='operator_cancelled')
        invalidate_trip(trip_id)
    except Exception:  # noqa: BLE001
        logger.exception('operator cancel: cache cleanup failed for trip %s', trip_id)
    users = [rider_id]
    if driver_id:
        try:
            from servers.redis_client import clear_driver_active_trip

            clear_driver_active_trip(driver_id)
        except Exception:  # noqa: BLE001
            logger.exception('operator cancel: driver release failed for %s', driver_id)
        from servers.driver.models import Driver

        driver_user = Driver.objects.filter(pk=driver_id).values_list('user_id', flat=True).first()
        if driver_user:
            users.append(driver_user)
    message = f'Trip #{trip_id} was cancelled by support.'
    for user in get_user_model().objects.filter(pk__in=users):
        try:
            Notification.objects.create(user_id=user, title='Ride Cancelled', message=message,
                                        notif_type='ride_event', trip_id=trip_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning('operator cancel notification failed: %s', exc)
        try:
            from servers.auth_user.services import send_push_notification

            send_push_notification(user, 'Ride Cancelled', message,
                                   {'trip_id': str(trip_id), 'type': 'ride_cancelled'})
        except Exception as exc:  # noqa: BLE001
            logger.warning('operator cancel push failed: %s', exc)


def _cancel_params(data):
    reason_code = _text(data, 'reason_code', required=True, limit=32)
    if reason_code not in CANCEL_REASONS:
        raise BadInput('INVALID_REASON',
                       f'reason_code must be one of {", ".join(CANCEL_REASONS)}.', 'reason_code')
    note = _text(data, 'note', required=(reason_code == 'other'))
    waive = data.get('waive_fee', False)
    if not isinstance(waive, bool):
        raise BadInput('INVALID_FIELD', 'waive_fee must be true or false.', 'waive_fee')
    return reason_code, note, waive


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('trip.write')])
def admin_cancel_trip(request, trip_id):
    """POST /ride/admin/trips/<id>/cancel/  {reason_code, note, waive_fee}"""
    trip, refusal = _trip_for_write(request, trip_id, 'trip.write')
    if refusal:
        return refusal
    try:
        reason_code, note, waive = _cancel_params(request.data)
    except BadInput as exc:
        return exc.response()
    outcome, trip = operator_cancel(trip.id, reason_code=reason_code, note=note,
                                    waive_fee=waive, http_request=request)
    if outcome == 'not_found':
        return _not_found(trip_id)
    if outcome == 'not_cancellable':
        current = trip.status_id.status_code if trip.status_id else ''
        return error_response(
            code='NOT_CANCELLABLE',
            message=f'A {current} trip cannot be cancelled here.',
            field='status', issue=f'Cancellable from: {", ".join(CANCELLABLE_STATUSES)}',
            status=status.HTTP_409_CONFLICT)
    return success_response({'trip_id': trip.id, 'status': 'cancelled',
                             'already_cancelled': outcome == 'already_cancelled',
                             'cancelled_at': _iso(trip.cancelled_at)}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Refund and adjustment
# ---------------------------------------------------------------------------

def _country_or_refusal(trip):
    country = trip_country(trip)
    if country is None:
        return None, error_response(code='COUNTRY_UNKNOWN', message='This trip has no city.',
                                    field='trip_id', issue='No zone on the trip',
                                    status=status.HTTP_409_CONFLICT)
    return country, None


def _linked_ticket(request, trip):
    """The support ticket id a refund is for, validated, or None.

    The ticket must be about THIS trip and visible to the caller; anything
    else is refused rather than silently unlinked."""
    raw = request.data.get('ticket_id')
    if raw in (None, ''):
        return None
    from servers.support.models import SupportTicket
    from servers.support.scope import tickets_visible_to

    ticket_id = _int(raw, 'ticket_id')
    ticket = tickets_visible_to(request.user, None, SupportTicket.objects.filter(
        id=ticket_id, trip_id=trip)).first()
    if ticket is None:
        raise BadInput('TICKET_TRIP_MISMATCH',
                       'That ticket does not exist for this trip.', 'ticket_id')
    return ticket.id


def _post_refund_review_message(ticket_id, request_id):
    """The ONE rider-facing line about a refund request, however it is being
    handled: neutral, with the country's review timeframe if one is
    configured. Nothing about approval, checking or any internal step."""
    from servers.support.models import SupportTicket
    from servers.support.service import post_system_message
    from servers.support.sla import refund_review_sentence

    country = SupportTicket.objects.filter(pk=ticket_id).values_list(
        'country', flat=True).first()
    post_system_message(ticket_id, refund_review_sentence(country or ''),
                        key=f'refund-pending:{request_id}')


def _note_refund_on_ticket(request, ticket_id, row, amount, currency):
    """Record the link: an audit row on the ticket and the neutral "we're
    reviewing" line for the rider (the same whether the refund ran at once
    or waits for a checker)."""
    record_admin_action(
        request, action='support_ticket_refund_requested', target_type='support_ticket',
        target_id=ticket_id,
        after={'trip_id': row.target_id, 'approval_request_id': row.id,
               'amount_minor': amount, 'currency': currency, 'state': row.status},
        reason=f'refund requested from ticket #{ticket_id}')
    _post_refund_review_message(ticket_id, row.id)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.refund')])
def admin_refund_trip(request, trip_id):
    """POST /ride/admin/trips/<id>/refund/  {amount_minor, reason, idempotency_key, ticket_id?}

    A caller holding `finance.read` here refunds under `finance.refund`;
    anyone else holding `support.refund` under `support.refund`. Within the
    maker's limit it runs now (200); above it waits for a checker (202).

    `ticket_id` (the Support module's fare review) links the refund to a
    support ticket ABOUT THIS TRIP: the id goes into the request's reason
    and payload, and the ticket gets a system message when the refund is
    requested and again when it is done. It changes no amount and no limit.
    """
    trip, refusal = _trip_for_write(request, trip_id, 'support.refund')
    if refusal:
        return refusal
    try:
        amount = _minor_amount(request.data.get('amount_minor'))
        if amount <= 0:
            raise BadInput('AMOUNT_NOT_POSITIVE', 'amount_minor must be above zero.',
                           'amount_minor')
        reason = _text(request.data, 'reason', required=True)
        key = _idempotency_key(request)
        ticket_id = _linked_ticket(request, trip)
    except BadInput as exc:
        return exc.response()
    if ticket_id:
        reason = f'{reason} [support ticket #{ticket_id}]'[:2000]
    country, refusal = _country_or_refusal(trip)
    if refusal:
        return refusal
    remaining = refundable_minor(trip)
    if amount > remaining:
        return error_response(
            code='REFUND_EXCEEDS_PAID',
            message='The refund is more than what is left of the amount paid.',
            field='amount_minor', issue=f'At most {remaining} {trip.currency} minor units',
            status=status.HTTP_400_BAD_REQUEST)

    node = node_of(trip)
    action_key = (FINANCE_REFUND if granted(request.user, 'finance.read', node)[0]
                  else SUPPORT_REFUND)
    try:
        outcome = approvals.require_approval_or_execute(
            action_key, actor=request.user, country=country, amount=amount,
            currency=trip.currency, scope_node=node, target_type=TARGET, target_id=trip.id,
            payload={'trip_id': trip.id, 'amount_minor': amount, 'currency': trip.currency,
                     'method': refund_method(trip),
                     **({'ticket_id': ticket_id} if ticket_id else {})},
            idempotency_key=f'trip_refund:{trip.id}:{key}', reason=reason,
            http_request=request,
        )
    except approvals.ApprovalError as exc:
        return exc.response()
    if ticket_id and not outcome.duplicate:
        _note_refund_on_ticket(request, ticket_id, outcome.request, amount, trip.currency)
    return _approval_answer(outcome, {'trip_id': trip.id})


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.adjust')])
def admin_adjust_fare(request, trip_id):
    """POST /ride/admin/trips/<id>/adjust-fare/  {amount_minor (signed), reason, idempotency_key}

    Records a signed manual adjustment in the trip's ledger. The threshold
    is on its size (`finance.adjust`); nothing is re-priced.
    """
    trip, refusal = _trip_for_write(request, trip_id, 'finance.adjust')
    if refusal:
        return refusal
    try:
        amount = _minor_amount(request.data.get('amount_minor'))
        if amount == 0:
            raise BadInput('AMOUNT_ZERO', 'amount_minor must not be zero.', 'amount_minor')
        reason = _text(request.data, 'reason', required=True)
        key = _idempotency_key(request)
    except BadInput as exc:
        return exc.response()
    if amount < 0 and -amount > charged_minor(trip):
        return error_response(
            code='ADJUSTMENT_EXCEEDS_FARE',
            message='A downward adjustment cannot exceed what the trip charged.',
            field='amount_minor', issue=f'At most {charged_minor(trip)}',
            status=status.HTTP_400_BAD_REQUEST)
    country, refusal = _country_or_refusal(trip)
    if refusal:
        return refusal
    try:
        outcome = approvals.require_approval_or_execute(
            FINANCE_ADJUST, actor=request.user, country=country, amount=abs(amount),
            currency=trip.currency, scope_node=node_of(trip), target_type=TARGET,
            target_id=trip.id,
            payload={'trip_id': trip.id, 'amount_minor': amount, 'currency': trip.currency},
            idempotency_key=f'trip_adjust:{trip.id}:{key}', reason=reason,
            http_request=request,
        )
    except approvals.ApprovalError as exc:
        return exc.response()
    return _approval_answer(outcome, {'trip_id': trip.id})


def _locked_trip(request_row):
    payload = request_row.payload or {}
    trip = (Trip.objects.select_for_update(of=('self',)).select_related('user_id')
            .filter(id=payload.get('trip_id')).first())
    if trip is None:
        raise registry.ExecutionRefused('TRIP_NOT_FOUND',
                                        f'Trip {payload.get("trip_id")} no longer exists.')
    if trip.currency != payload.get('currency'):
        raise registry.ExecutionRefused('CURRENCY_CHANGED', 'The trip currency changed.')
    return trip


def execute_refund(request_row):
    """Carry out an approved (or within-limit) trip refund; writes the ledger row.

    Cash and wallet trips: a wallet credit through `rider.credits`
    (idempotent on the request id). Online trips: TODO(integration) -- the
    gateway refund is RECORDED as pending_gateway and no provider is called;
    finance completes it out of band until the provider call is tested.
    """
    from servers.rider.credits import issue_refund_credit

    payload = request_row.payload or {}
    amount = int(payload.get('amount_minor') or 0)
    with transaction.atomic():
        trip = _locked_trip(request_row)
        existing = TripMoneyAction.objects.filter(approval_request=request_row).first()
        if existing is not None:
            return {'money_action_id': existing.id, 'status': existing.status}
        remaining = paid_minor(trip) - refunded_minor(trip, exclude_request_id=request_row.id)
        if amount <= 0 or amount > remaining:
            raise registry.ExecutionRefused(
                'REFUND_EXCEEDS_PAID',
                f'Refund of {amount} exceeds the {max(0, remaining)} left to refund.')
        method = payload.get('method') or refund_method(trip)
        wallet_txn = None
        if method == TripMoneyAction.WALLET_CREDIT:
            result = issue_refund_credit(
                trip.user_id, str(from_minor(amount, trip.currency)), trip.id,
                idempotency_key=f'trip_refund_request:{request_row.id}')
            if not result.ok:
                raise registry.ExecutionRefused(
                    f'WALLET_{(result.reason or "refused").upper()}',
                    f'The wallet credit was refused ({result.reason}).')
            wallet_txn = result.transaction_id
            state = TripMoneyAction.COMPLETED
        else:
            # TODO(integration): send this refund to the payment provider once
            # the provider call is tested end to end. Until then it is
            # recorded as owed and finance completes it manually.
            state = TripMoneyAction.PENDING_GATEWAY
        row = TripMoneyAction.objects.create(
            trip=trip, kind=TripMoneyAction.REFUND, amount_minor=amount,
            currency=trip.currency, method=method, status=state,
            reason=request_row.request_reason or '', approval_request=request_row,
            wallet_transaction_id=wallet_txn, created_by_id=request_row.maker_id,
        )
        record_admin_action(
            None, action='trip_refunded', target_type=TARGET, target_id=trip.id,
            after={'money_action_id': row.id, 'amount_minor': amount,
                   'currency': trip.currency, 'method': method, 'status': state,
                   'approval_request_id': request_row.id,
                   'action_key': request_row.action_key},
            reason=request_row.request_reason or '',
        )
    ticket_id = payload.get('ticket_id')
    if ticket_id:
        from servers.support.service import post_system_message

        # The "we're reviewing" line comes first (a refund that ran at once
        # has not posted it yet); the key makes a second post a no-op.
        _post_refund_review_message(ticket_id, request_row.id)
        done = ('Your refund of %s %s for this trip has been issued.'
                if state == TripMoneyAction.COMPLETED else
                'Your refund of %s %s for this trip has been processed and is on its way.')
        post_system_message(ticket_id, done % (from_minor(amount, trip.currency), trip.currency),
                            key=f'refund-done:{request_row.id}')
    return {'money_action_id': row.id, 'status': state, 'method': method,
            'amount_minor': amount, 'currency': trip.currency,
            'wallet_transaction_id': wallet_txn}


def execute_adjustment(request_row):
    """Record an approved (or within-limit) signed fare adjustment. Ledger only."""
    payload = request_row.payload or {}
    amount = int(payload.get('amount_minor') or 0)
    with transaction.atomic():
        trip = _locked_trip(request_row)
        existing = TripMoneyAction.objects.filter(approval_request=request_row).first()
        if existing is not None:
            return {'money_action_id': existing.id, 'status': existing.status}
        row = TripMoneyAction.objects.create(
            trip=trip, kind=TripMoneyAction.ADJUSTMENT, amount_minor=amount,
            currency=trip.currency, method=TripMoneyAction.LEDGER_ONLY,
            status=TripMoneyAction.RECORDED, reason=request_row.request_reason or '',
            approval_request=request_row, created_by_id=request_row.maker_id,
        )
        record_admin_action(
            None, action='trip_fare_adjusted', target_type=TARGET, target_id=trip.id,
            after={'money_action_id': row.id, 'amount_minor': amount,
                   'currency': trip.currency, 'approval_request_id': request_row.id},
            reason=request_row.request_reason or '',
        )
    return {'money_action_id': row.id, 'status': row.status, 'amount_minor': amount,
            'currency': trip.currency}


registry.register(SUPPORT_REFUND)(execute_refund)
registry.register(FINANCE_REFUND)(execute_refund)
adjust.register(TARGET)(execute_adjustment)


# A refund tied to a ticket that is rejected, cancelled or lets expire goes
# back to the agent who asked (one callback, no polling).
def _refund_closed(request_row, outcome):
    from servers.support.service import return_refund_to_agent

    return_refund_to_agent(request_row, outcome)


registry.on_closed(SUPPORT_REFUND)(_refund_closed)
registry.on_closed(FINANCE_REFUND)(_refund_closed)


# ---------------------------------------------------------------------------
# Flags and notes
# ---------------------------------------------------------------------------

def operator_flag(trip_id, *, reason, note='', actor=None, http_request=None):
    """Raise a flag. Returns (outcome, flag): 'flagged' | 'already_flagged' | 'not_found'."""
    trip = Trip.objects.filter(id=trip_id).first()
    if trip is None:
        return 'not_found', None
    existing = TripFlag.objects.filter(trip=trip, reason=reason, cleared_at__isnull=True).first()
    if existing is not None:
        return 'already_flagged', existing
    try:
        with transaction.atomic():
            flag = TripFlag.objects.create(trip=trip, reason=reason, note=note,
                                           created_by=actor)
    except IntegrityError:
        return 'already_flagged', TripFlag.objects.filter(
            trip=trip, reason=reason, cleared_at__isnull=True).first()
    record_admin_action(http_request, action='trip_flagged', target_type=TARGET,
                        target_id=trip.id, after={'flag_id': flag.id, 'reason': reason},
                        reason=note or FLAG_REASONS[reason])
    return 'flagged', flag


def _flag_reason(data, required=True):
    reason = _text(data, 'reason', required=required, limit=32)
    if reason and reason not in FLAG_REASONS:
        raise BadInput('INVALID_REASON',
                       f'reason must be one of {", ".join(FLAG_REASONS)}.', 'reason')
    return reason


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('trip.write')])
def admin_flag_trip(request, trip_id):
    """POST /ride/admin/trips/<id>/flag/  {reason: fraud_suspicion|safety|dispute, note}"""
    trip, refusal = _trip_for_write(request, trip_id, 'trip.write')
    if refusal:
        return refusal
    try:
        reason = _flag_reason(request.data)
        note = _text(request.data, 'note')
    except BadInput as exc:
        return exc.response()
    outcome, flag = operator_flag(trip.id, reason=reason, note=note, actor=request.user,
                                  http_request=request)
    return success_response(
        {'trip_id': trip.id, 'flag_id': flag.id, 'reason': flag.reason,
         'created': outcome == 'flagged'},
        status.HTTP_201_CREATED if outcome == 'flagged' else status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('trip.write')])
def admin_unflag_trip(request, trip_id):
    """POST /ride/admin/trips/<id>/unflag/  {reason (all when absent), note}"""
    trip, refusal = _trip_for_write(request, trip_id, 'trip.write')
    if refusal:
        return refusal
    try:
        reason = _flag_reason(request.data, required=False)
        note = _text(request.data, 'note')
    except BadInput as exc:
        return exc.response()
    with transaction.atomic():
        active = TripFlag.objects.select_for_update().filter(trip=trip, cleared_at__isnull=True)
        if reason:
            active = active.filter(reason=reason)
        ids = list(active.values_list('id', flat=True))
        if ids:
            TripFlag.objects.filter(id__in=ids).update(
                cleared_at=timezone.now(), cleared_by=request.user, clear_note=note)
            record_admin_action(request, action='trip_unflagged', target_type=TARGET,
                                target_id=trip.id, before={'flag_ids': ids},
                                after={'cleared': len(ids)}, reason=note or 'cleared')
    return success_response({'trip_id': trip.id, 'cleared': len(ids)}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('trip.write')])
def admin_note_trip(request, trip_id):
    """POST /ride/admin/trips/<id>/note/  {body, idempotency_key?}  Internal only."""
    trip, refusal = _trip_for_write(request, trip_id, 'trip.write')
    if refusal:
        return refusal
    try:
        body = _text(request.data, 'body', required=True, limit=5000)
        key = _idempotency_key(request, required=False)
    except BadInput as exc:
        return exc.response()
    scoped_key = f'trip_note:{trip.id}:{key}' if key else None
    existing = (TripNote.objects.filter(idempotency_key=scoped_key).first()
                if scoped_key else None)
    if existing is None:
        try:
            with transaction.atomic():
                note = TripNote.objects.create(trip=trip, body=body, author=request.user,
                                               idempotency_key=scoped_key)
        except IntegrityError:
            existing = TripNote.objects.filter(idempotency_key=scoped_key).first()
    if existing is not None:
        return success_response({'trip_id': trip.id, 'note_id': existing.id,
                                 'created': False}, status.HTTP_200_OK)
    record_admin_action(request, action='trip_note_added', target_type=TARGET,
                        target_id=trip.id, after={'note_id': note.id, 'body': body},
                        reason='internal note')
    return success_response({'trip_id': trip.id, 'note_id': note.id, 'created': True},
                            status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

def execute_bulk(request_row):
    """Run a bulk cancel or flag row by row; returns per-row results."""
    from django.contrib.auth import get_user_model

    p = request_row.payload or {}
    action = p.get('action')
    if action not in BULK_ACTIONS:
        raise registry.ExecutionRefused('UNKNOWN_BULK_ACTION', f'Unknown action {action!r}.')
    maker = get_user_model().objects.filter(pk=request_row.maker_id).first()
    results = []
    for trip_id in p.get('trip_ids') or []:
        if action == 'cancel':
            outcome, _trip = operator_cancel(trip_id, reason_code=p.get('reason_code'),
                                             note=p.get('note') or '',
                                             waive_fee=bool(p.get('waive_fee')))
        else:
            outcome, _flag = operator_flag(trip_id, reason=p.get('reason'),
                                           note=p.get('note') or '', actor=maker)
        results.append({'id': trip_id, 'result': outcome})
    for trip_id in p.get('not_found_ids') or []:
        results.append({'id': trip_id, 'result': 'not_found'})
    counts = {}
    for row in results:
        counts[row['result']] = counts.get(row['result'], 0) + 1
    return {'action': action, 'results': results, 'counts': counts}


# bulk.action is shared by every module; the dispatcher routes by target type.
bulk.register('trip_bulk')(execute_bulk)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('trip.write')])
def admin_bulk_trips(request):
    """POST /ride/admin/trips/bulk/
    {action: cancel|flag, trip_ids, reason_code|reason, note, waive_fee, idempotency_key}

    Up to the bulk.action limit (50 records in India) it runs now with
    per-row results; above it a checker must approve (202). Trips the caller
    cannot write are reported not_found, as a single write would answer.
    """
    data = request.data
    try:
        action = _text(data, 'action', required=True, limit=16)
        if action not in BULK_ACTIONS:
            raise BadInput('INVALID_ACTION', f'action must be one of {", ".join(BULK_ACTIONS)}.',
                           'action')
        raw_ids = data.get('trip_ids')
        if not isinstance(raw_ids, list) or not raw_ids:
            raise BadInput('TRIP_IDS_REQUIRED', 'trip_ids must be a non-empty list.', 'trip_ids')
        if len(raw_ids) > BULK_MAX_IDS:
            raise BadInput('TOO_MANY', f'At most {BULK_MAX_IDS} trips per bulk action.',
                           'trip_ids')
        ids = list(dict.fromkeys(_int(raw, 'trip_ids') for raw in raw_ids))
        params = {'action': action}
        if action == 'cancel':
            reason_code, note, waive = _cancel_params(data)
            params.update(reason_code=reason_code, note=note, waive_fee=waive)
        else:
            params.update(reason=_flag_reason(data), note=_text(data, 'note'))
        key = _idempotency_key(request)
    except BadInput as exc:
        return exc.response()

    visible = (scoped(request.user, Trip.objects.select_related('zone', 'zone__geo_node'))
               .filter(id__in=ids))
    writable = {t.id: t for t in visible if granted(request.user, 'trip.write', node_of(t))[0]}
    ordered = [i for i in ids if i in writable]
    not_found = [i for i in ids if i not in writable]
    if not ordered:
        return success_response({'state': 'executed', 'count': 0, 'result': {
            'action': action, 'results': [{'id': i, 'result': 'not_found'} for i in ids],
            'counts': {'not_found': len(ids)}}}, status.HTTP_200_OK)
    countries = {trip_country(t) for t in writable.values()}
    if len(countries) != 1 or None in countries:
        return error_response(
            code='MIXED_COUNTRIES', message='A bulk action covers one country at a time.',
            field='trip_ids', issue=', '.join(sorted(c or 'none' for c in countries)),
            status=status.HTTP_400_BAD_REQUEST)
    nodes = {node_of(t) for t in writable.values()}
    try:
        outcome = approvals.require_approval_or_execute(
            BULK_ACTION, actor=request.user, country=countries.pop(), amount=len(ordered),
            scope_node=nodes.pop() if len(nodes) == 1 else None,
            target_type='trip_bulk', target_id=f'{action}:{len(ordered)}',
            payload={**params, 'trip_ids': ordered, 'not_found_ids': not_found},
            idempotency_key=f'trip_bulk:{key}',
            reason=params.get('note') or params.get('reason_code') or params.get('reason') or '',
            http_request=request,
        )
    except approvals.ApprovalError as exc:
        return exc.response()
    return _approval_answer(outcome, {'count': len(ordered)})
