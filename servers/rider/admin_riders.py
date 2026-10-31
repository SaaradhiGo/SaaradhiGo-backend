"""Riders module (operator console, module 4): list, detail, writes.

WHAT LIVES HERE
---------------
* `GET /rider/admin/` - the rider list: server-side filters, sort, pages.
* `GET /rider/admin/<id>/` - the detail: profile, wallet (read-only), trips,
  tickets, SOS, ratings, saved-place and session counts, block / flags,
  status history, notes, the rider's audit trail and the caller's actions.
* The writes: block / unblock, internal note, flag / unflag, force sign-out
  and bulk block / unblock (over the `bulk.action` limit a checker approves).

THE RIDER'S ID IS THE ACCOUNT ID
--------------------------------
`<id>` is the rider's user (account) id, not `Rider.pk`: it is the id the
Trips module calls `rider_id`, the id the reveal endpoint takes for
`target_type='rider'`, and the `target_id` of this module's audit rows, so
one number names the rider everywhere in the console.

HOW A RIDER'S SCOPE IS DECIDED
-----------------------------
`rider.scope` holds the one rule (`riders_visible_to`, `rider_may`): a rider
belongs to every city they rode in within `RIDER_SCOPE_TRIP_MONTHS`, plus a
home city if known, else their phone's country. The list, the page, the
writes, rider search and the reveal endpoint all use it. The `city` shown
here is display only: the latest trip's city, else the home city, else the
country. Out of scope answers 404; in scope without the capability 403.

RULES THIS FILE KEEPS
---------------------
* Every write: its own capability (`rider.note`, `rider.flag`,
  `rider.signout`, `rider.block`), re-checked at the rider's anchors, audited with `record_admin_action`, idempotent (a repeat changes
  nothing and writes no second audit row).
* Nothing here moves money. The wallet is DISPLAYED (balance and recent
  ledger rows, integer minor units with their currency); credits and debits
  belong to Payments & Finance.
* Personal data leaves masked: the payload uses the field names the masking
  renderer knows (`full_name`, `phone_number`, `email`), and the console's
  `Revealable` goes through `/admin-access/reveal/` (logged).
* A block refuses ride REQUESTS (`rider.eligibility`) and signs the rider
  out, at trip end if one is under way; SOS always works. See `RiderBlock`.
"""

import logging
from datetime import datetime, time, timedelta

from django.db import IntegrityError, transaction
from django.db.models import (
    Avg, Count, Exists, F, IntegerField, OuterRef, Q, Subquery, Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_access.scoping import ScopeNotHeld, requested_node
from servers.admin_audit.services import record_admin_action
from servers.approvals import bulk, registry
from servers.approvals import service as approvals
from servers.approvals.actions import BULK_ACTION
from servers.ride.admin_trips import (
    BadInput, _approval_answer, _bool, _date, _idempotency_key, _int, _iso, _staff, _text,
)
from servers.ride.models import Trip

from .scope import anchor_nodes, riders_visible_to, rider_may
from .models import Rider, RiderBlock, RiderFlag, RiderNote, Wallet, WalletTransaction

logger = logging.getLogger(__name__)

TARGET = 'rider'

BLOCK_REASONS = dict(RiderBlock.REASON_CHOICES)
FLAG_REASONS = dict(RiderFlag.REASON_CHOICES)
BULK_ACTIONS = ('block', 'unblock')
BULK_MAX_IDS = 500
WALLET_ROWS_PER_CURRENCY = 10
RECENT = 10

SORTS = {
    'id': 'user_id_id',
    'joined': 'user_id__created_at',
    'trips': 'trips_completed',
    'last_trip': 'last_trip_at',
    'rating': 'rating',
}
DEFAULT_SORT = '-id'

#: Audit actions that change a rider's standing, shown as status history.
STATUS_ACTIONS = (
    'rider_blocked', 'rider_unblocked', 'rider_flagged', 'rider_unflagged',
    'rider_signed_out', 'rider_cash_disabled', 'rider_cash_enabled',
)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def _latest_trip():
    return Trip.objects.filter(user_id=OuterRef('user_id')).order_by('-requested_at', '-id')


def annotate(queryset):
    """City (via the latest trip), counts and states the list filters on."""
    from servers.sos.models import SOSEvent
    from servers.support.models import SupportTicket

    latest = _latest_trip()

    def trip_count(**extra):
        rows = (Trip.objects.filter(user_id=OuterRef('user_id'), **extra).order_by()
                .values('user_id').annotate(n=Count('id')).values('n')[:1])
        return Coalesce(Subquery(rows, output_field=IntegerField()), Value(0))

    return queryset.annotate(
        latest_trip_id=Subquery(latest.values('id')[:1]),
        city_node_id=Coalesce(
            Subquery(latest.filter(zone__geo_node__isnull=False)
                     .values('zone__geo_node_id')[:1]),
            F('home_city_id'), output_field=IntegerField()),
        trip_city_id=Subquery(latest.filter(zone__geo_node__isnull=False)
                              .values('zone__geo_node_id')[:1]),
        last_trip_at=Subquery(latest.values('requested_at')[:1]),
        trips_total=trip_count(),
        trips_completed=trip_count(status_id__status_code='completed'),
        is_blocked=Exists(RiderBlock.objects.filter(rider=OuterRef('pk'),
                                                    lifted_at__isnull=True)),
        is_flagged=Exists(RiderFlag.objects.filter(rider=OuterRef('pk'),
                                                   cleared_at__isnull=True)),
        has_open_ticket=Exists(SupportTicket.objects.filter(
            user_id=OuterRef('user_id')).exclude(status='CLOSED')),
        had_sos=Exists(SOSEvent.objects.filter(user=OuterRef('user_id'))),
    )


def _nodes(ids):
    from servers.admin_access.models import GeoNode

    ids = {i for i in ids if i}
    return {n.id: n for n in GeoNode.objects.filter(id__in=ids)} if ids else {}


def node_of_rider(rider):
    """The node shown for the rider: latest trip city, else home city, else
    the country their phone belongs to (display only; see `rider.scope`)."""
    node_id = getattr(rider, 'city_node_id', None)
    if node_id:
        return _nodes([node_id]).get(node_id)
    anchors = anchor_nodes(rider)
    return anchors[0] if anchors else None


def _city(node):
    from servers.driver.admin_drivers import city_tz

    if node is None:
        return None
    return {'code': node.code, 'name': node.name, 'timezone': str(city_tz(node))}


def _country(rider, node):
    from base.countries import country_of_phone

    code = getattr(node, 'code', '') or ''
    if code and code != 'GLOBAL':
        return code.split('-')[0]
    country = country_of_phone(getattr(rider.user_id, 'phone_number', None))
    return country.code if country is not None else 'IN'


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _day_range_q(queryset, column, date_from, date_to):
    """`column` within [date_from, date_to] in EACH rider's city day (the
    deployment default zone for a rider with no city)."""
    from base.market_time import _zoneinfo
    from servers.driver.admin_drivers import city_tz

    def bounds(tz):
        q = Q()
        if date_from:
            q &= Q(**{f'{column}__gte': datetime.combine(date_from, time.min, tzinfo=tz)})
        if date_to:
            q &= Q(**{f'{column}__lt': datetime.combine(
                date_to + timedelta(days=1), time.min, tzinfo=tz)})
        return q

    node_ids = set(queryset.exclude(city_node_id=None)
                   .values_list('city_node_id', flat=True).distinct())
    by_tz = {}
    for node in _nodes(node_ids).values():
        tz = city_tz(node)
        by_tz.setdefault(str(tz), (tz, []))[1].append(node.id)
    combined = Q(city_node_id__isnull=True) & bounds(_zoneinfo(None))
    for tz, ids in by_tz.values():
        combined |= Q(city_node_id__in=ids) & bounds(tz)
    return combined


def _range(params, low_name, high_name, parse):
    low, high = parse(params, low_name), parse(params, high_name)
    if low is not None and high is not None and low > high:
        raise BadInput('INVALID_FILTER', f'{low_name} is after {high_name}.', low_name)
    return low, high


def _count(params, name):
    raw = params.get(name)
    if raw in (None, ''):
        return None
    value = _int(raw, name)
    if value < 0:
        raise BadInput('INVALID_FILTER', f'{name} must be 0 or more.', name)
    return value


def filter_riders(params, queryset):
    """Apply the list's filters and sort. Raises BadInput on a malformed value."""
    rider_id = params.get('id') or params.get('rider_id')
    if rider_id:
        queryset = queryset.filter(user_id_id=_int(rider_id, 'id'))

    joined_from, joined_to = _range(params, 'joined_from', 'joined_to', _date)
    if joined_from or joined_to:
        queryset = queryset.filter(
            _day_range_q(queryset, 'user_id__created_at', joined_from, joined_to))

    last_from, last_to = _range(params, 'last_trip_from', 'last_trip_to', _date)
    if last_from or last_to:
        queryset = queryset.filter(_day_range_q(queryset, 'last_trip_at', last_from, last_to))

    low, high = _range(params, 'trips_min', 'trips_max', _count)
    if low is not None:
        queryset = queryset.filter(trips_completed__gte=low)
    if high is not None:
        queryset = queryset.filter(trips_completed__lte=high)

    for name, column in (('blocked', 'is_blocked'), ('flagged', 'is_flagged'),
                         ('has_open_ticket', 'has_open_ticket'), ('had_sos', 'had_sos'),
                         ('low_rating', 'flagged_for_review')):
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
    column = F(SORTS[key])
    column = column.desc(nulls_last=True) if ordering.startswith('-') else column.asc(
        nulls_last=True)
    tie = '-user_id_id' if ordering.startswith('-') else 'user_id_id'
    return queryset.order_by(column, tie)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class RiderPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def _soft_blocked(rider):
    from .rating_decay import RIDER_SOFT_BLOCK_THRESHOLD

    return rider.rating is not None and rider.rating < RIDER_SOFT_BLOCK_THRESHOLD


def _row(rider, nodes):
    user = rider.user_id
    return {
        'id': user.id,
        'profile_id': rider.id,
        'user_details': {
            'id': user.id, 'full_name': user.full_name, 'phone_number': user.phone_number,
            'email': user.email, 'is_active': user.is_active,
        },
        'city': _city(nodes.get(rider.city_node_id)),
        'joined_at': _iso(getattr(user, 'created_at', None)),
        'trips_total': rider.trips_total,
        'trips_completed': rider.trips_completed,
        'last_trip_at': _iso(rider.last_trip_at),
        'last_trip_id': rider.latest_trip_id,
        'rating': str(rider.rating),
        'low_rating': rider.flagged_for_review,
        'soft_blocked': _soft_blocked(rider),
        'blocked': bool(rider.is_blocked),
        'flagged': bool(rider.is_flagged),
        'has_open_ticket': bool(rider.has_open_ticket),
        'had_sos': bool(rider.had_sos),
    }


def _scope_refused(exc):
    return error_response(code='SCOPE_NOT_HELD', message=str(exc), field='scope',
                          issue='Requested scope is outside your assignments',
                          status=status.HTTP_403_FORBIDDEN)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.read')])
def admin_list_riders(request):
    """GET /rider/admin/  Riders in the caller's scope (see the module
    docstring for how a rider's city is decided), filtered, sorted, paged."""
    try:
        node = requested_node(request)
    except ScopeNotHeld as exc:
        return _scope_refused(exc)
    try:
        riders = filter_riders(request.query_params, riders_visible_to(
            request.user, node, annotate(Rider.objects.select_related('user_id'))))
    except BadInput as exc:
        return exc.response()

    paginator = RiderPagination()
    page = paginator.paginate_queryset(riders, request)
    nodes = _nodes(r.city_node_id for r in page)
    django_page = paginator.page
    return success_response({
        'count': django_page.paginator.count,
        'next': paginator.get_next_link(),
        'previous': paginator.get_previous_link(),
        'page': django_page.number,
        'page_size': paginator.get_page_size(request),
        'total_pages': django_page.paginator.num_pages,
        'ordering': request.query_params.get('ordering') or DEFAULT_SORT,
        'results': [_row(r, nodes) for r in page],
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def _not_found(rider_id):
    return error_response(code='NOT_FOUND', message='Rider not found.', field='rider_id',
                          issue=f'No rider {rider_id}', status=status.HTTP_404_NOT_FOUND)


def _rider_for(request, rider_id, *permissions):
    """(rider, node, None) or (None, None, error response).

    Not in scope (or no such rider) is NOT_FOUND, identically; in scope
    without any of `permissions` at the rider's city is 403.
    """
    rider = riders_visible_to(
        request.user, None, annotate(Rider.objects.select_related('user_id'))
    ).filter(user_id_id=rider_id).first()
    if rider is None:
        return None, None, _not_found(rider_id)
    node = node_of_rider(rider)
    if permissions and not any(rider_may(request.user, p, rider) for p in permissions):
        return None, None, error_response(
            code='PERMISSION_DENIED',
            message=f'You do not hold {" or ".join(permissions)} for this rider\'s city.',
            field='rider_id', issue=permissions[0], status=status.HTTP_403_FORBIDDEN)
    return rider, node, None


def _city_source(rider, node):
    if node is None:
        return None
    if getattr(rider, 'trip_city_id', None):
        return 'latest_trip'
    return 'home_city' if rider.home_city_id else 'country'


def _wallet(user):
    """Balances and the latest ledger rows per currency. Read-only."""
    wallets = Wallet.objects.filter(user_id=user, scope=Wallet.SCOPE_RIDER)
    balances = [{'currency': w.currency, 'balance_minor': w.balance_minor} for w in wallets]
    currencies = sorted(set(WalletTransaction.objects.filter(user_id=user)
                            .values_list('currency', flat=True).distinct())
                        | {b['currency'] for b in balances})
    transactions = {}
    for currency in currencies:
        rows = (WalletTransaction.objects.filter(user_id=user, currency=currency)
                .order_by('-created_at', '-id')[:WALLET_ROWS_PER_CURRENCY])
        transactions[currency] = [{
            'id': t.id, 'txn_type': t.txn_type, 'amount_minor': t.amount_minor,
            'currency': t.currency, 'status': t.status, 'purpose': t.purpose or '',
            'reference_id': t.reference_id or '', 'created_at': _iso(t.created_at),
        } for t in rows]
    return {'balances': balances, 'transactions': transactions,
            'rows_per_currency': WALLET_ROWS_PER_CURRENCY}


def _trips(user):
    trips = Trip.objects.filter(user_id=user)
    by_status = {row['status_id__status_code'] or 'unknown': row['n'] for row in
                 trips.order_by().values('status_id__status_code').annotate(n=Count('id'))}
    recent = (trips.select_related('status_id', 'zone__geo_node')
              .order_by('-requested_at', '-id')[:RECENT])
    return {
        'total': sum(by_status.values()),
        'by_status': by_status,
        'recent': [{
            'id': t.id,
            'status': t.status_id.status_code if t.status_id else None,
            'requested_at': _iso(t.requested_at),
            'completed_at': _iso(t.completed_at),
            'fare_minor': (t.final_fare_minor if t.final_fare_minor is not None
                           else t.estimated_fare_minor),
            'currency': t.currency,
            'city_code': (t.zone.geo_node.code if t.zone_id and t.zone.geo_node_id else None),
        } for t in recent],
    }


def _tickets(user):
    from servers.support.models import SupportTicket

    tickets = SupportTicket.objects.filter(user_id=user)
    return {
        'total': tickets.count(),
        'open': tickets.exclude(status='CLOSED').count(),
        'recent': [{'id': t.id, 'status': t.status, 'issue_type': t.issue_type,
                    'trip_id': t.trip_id_id, 'created_at': _iso(t.created_at)}
                   for t in tickets.order_by('-created_at', '-id')[:RECENT]],
    }


def _sos(user):
    from servers.sos.models import SOSEvent

    events = SOSEvent.objects.filter(user=user)
    return {
        'total': events.count(),
        'open': events.filter(status__in=('open', 'acknowledged')).count(),
        'recent': [{'id': e.id, 'status': e.status, 'event_type': e.event_type,
                    'trip_id': e.trip_id, 'created_at': _iso(e.created_at)}
                   for e in events.order_by('-created_at', '-id')[:RECENT]],
    }


def _ratings(rider):
    from servers.ride.models import Rating

    user = rider.user_id
    given = Rating.objects.filter(rater_id=user)
    received = Rating.objects.filter(trip_id__user_id=user).exclude(rater_id=user)
    g = given.aggregate(avg=Avg('score'), count=Count('id'))
    r = received.aggregate(avg=Avg('score'), count=Count('id'))
    distribution = {str(i): 0 for i in range(1, 6)}
    for row in received.values('score').annotate(n=Count('id')):
        if 1 <= (row['score'] or 0) <= 5:
            distribution[str(row['score'])] = row['n']

    def avg(value):
        return round(float(value), 2) if value is not None else None

    return {
        'rating': str(rider.rating),
        'rating_count': rider.rating_count,
        'low_rating': rider.flagged_for_review,
        'soft_blocked': _soft_blocked(rider),
        'given': {'count': g['count'], 'average': avg(g['avg'])},
        'received': {'count': r['count'], 'average': avg(r['avg']),
                     'distribution': distribution},
    }


def _sessions(user):
    """Counts only: live refresh tokens and whether a push device is set."""
    active = 0
    try:
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        active = (OutstandingToken.objects.filter(user=user, expires_at__gt=timezone.now())
                  .filter(blacklistedtoken__isnull=True).count())
    except Exception:  # noqa: BLE001 - a count must not 500 the page
        logger.warning('riders: could not count sessions for user %s', user.id)
    return {'active_sessions': active, 'push_device': bool(getattr(user, 'fcm_token', None))}


def _block(rider):
    row = (RiderBlock.objects.filter(rider=rider, lifted_at__isnull=True)
           .select_related('created_by').first())
    if row is None:
        return {'active': False}
    return {'active': True, 'id': row.id, 'reason_code': row.reason_code,
            'reason_label': BLOCK_REASONS.get(row.reason_code), 'note': row.note,
            'created_at': _iso(row.created_at), 'created_by': _staff(row.created_by)}


def _flags(rider):
    return [{'id': f.id, 'reason': f.reason, 'reason_label': FLAG_REASONS.get(f.reason),
             'note': f.note, 'created_at': _iso(f.created_at), 'created_by': _staff(f.created_by)}
            for f in RiderFlag.objects.filter(rider=rider, cleared_at__isnull=True)
            .select_related('created_by').order_by('-created_at')]


def _restrictions(rider):
    """Active operator restrictions (today: online payment only), with who set it and when."""
    from .models import RiderRestriction

    return [{'id': r.id, 'kind': r.kind, 'label': r.get_kind_display(), 'reason': r.reason,
             'set_at': _iso(r.set_at), 'set_by': _staff(r.set_by), 'flag_id': r.flag_id}
            for r in RiderRestriction.objects.filter(rider=rider, cleared_at__isnull=True)
            .select_related('set_by').order_by('-set_at')]


def _audit_rows(rider, actions=None):
    from servers.admin_audit.models import AdminAuditLog

    rows = AdminAuditLog.objects.filter(target_type=TARGET, target_id=str(rider.user_id_id))
    if actions:
        rows = rows.filter(action__in=actions)
    return [{
        'id': row.id, 'action': row.action, 'created_at': _iso(row.created_at),
        'reason': row.reason, 'after': row.after if actions else None,
        'created_by': {'id': row.actor_id, 'label': row.actor_label, 'role': row.actor_role},
    } for row in rows.order_by('-created_at', '-id')[:100]]


def _has_restriction(rider):
    from .models import RiderRestriction

    return RiderRestriction.objects.filter(rider=rider, cleared_at__isnull=True).exists()


def allowed_actions(user, rider, node):
    """What this caller may do to this rider, so the console shows only that."""
    anchors = anchor_nodes(rider)

    def may(permission):
        return rider_may(user, permission, rider, anchors)

    may_block, may_flag = may('rider.block'), may('rider.flag')
    blocked = RiderBlock.objects.filter(rider=rider, lifted_at__isnull=True).exists()
    return {
        'block': may_block and not blocked,
        'unblock': may_block and blocked,
        'note': may('rider.note'),
        'flag': may_flag,
        'unflag': may_flag and RiderFlag.objects.filter(
            rider=rider, cleared_at__isnull=True).exists(),
        # Clear the online-payment-only restriction (set from a payment-abuse review).
        'clear_restriction': may_block and _has_restriction(rider),
        'sign_out': may('rider.signout'),
        # Closed-loop wallet credit through a checker (Finance module, Part B).
        'wallet_credit': may('finance.adjust'),
        'reveal': may('pii.view'),
        'block_reasons': BLOCK_REASONS,
        'flag_reasons': FLAG_REASONS,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.read')])
def admin_rider_detail(request, rider_id):
    """GET /rider/admin/<id>/  One rider, if in scope (404 otherwise)."""
    rider, node, refusal = _rider_for(request, rider_id, 'rider.read')
    if refusal:
        return refusal
    user = rider.user_id
    return success_response({
        'id': user.id,
        'profile_id': rider.id,
        'user_details': {
            'id': user.id, 'full_name': user.full_name, 'phone_number': user.phone_number,
            'email': user.email, 'is_active': user.is_active,
        },
        'city': _city(node),
        'city_source': _city_source(rider, node),
        'anchors': [{'code': n.code, 'name': n.name, 'kind': n.kind}
                    for n in anchor_nodes(rider)],
        'joined_at': _iso(getattr(user, 'created_at', None)),
        'last_trip_at': _iso(rider.last_trip_at),
        'blocked': bool(rider.is_blocked),
        'flagged': bool(rider.is_flagged),
        'block': _block(rider),
        'flags': _flags(rider),
        'restrictions': _restrictions(rider),
        'wallet': _wallet(user),
        'trips': _trips(user),
        'tickets': _tickets(user),
        'sos': _sos(user),
        'ratings_summary': _ratings(rider),
        'saved_places_count': user.favorite_places.count(),
        'sessions': _sessions(user),
        'status_history': _audit_rows(rider, STATUS_ACTIONS),
        'notes': [
            {'id': n.id, 'body': n.body, 'created_at': _iso(n.created_at),
             'created_by': _staff(n.author)}
            for n in RiderNote.objects.filter(rider=rider).select_related('author')
            .order_by('-created_at')
        ],
        'approval_requests': _approval_requests(user.id),
        'audit_trail': _audit_rows(rider),
        'actions': allowed_actions(request.user, rider, node),
    }, status.HTTP_200_OK)


def _approval_requests(rider_id):
    """Bulk requests naming this rider (newest first). Filtered in Python:
    the payload is JSON and SQLite has no JSON containment lookup."""
    from servers.approvals.models import ApprovalRequest

    rows = ApprovalRequest.objects.filter(target_type='rider_bulk').order_by('-id')[:200]
    return [{'id': r.id, 'action_key': r.action_key, 'status': r.status,
             'requested_at': _iso(r.requested_at)}
            for r in rows if rider_id in ((r.payload or {}).get('rider_ids') or [])][:20]


# ---------------------------------------------------------------------------
# Block / unblock
# ---------------------------------------------------------------------------

def _locked(rider_pk):
    return Rider.objects.select_for_update().get(pk=rider_pk)


def operator_block(rider_pk, *, reason_code, note='', actor=None, http_request=None):
    """Block one rider. Returns (outcome, block): 'blocked' | 'already_blocked'
    | 'not_found'."""
    with transaction.atomic():
        try:
            rider = _locked(rider_pk)
        except Rider.DoesNotExist:
            return 'not_found', None
        existing = RiderBlock.objects.filter(rider=rider, lifted_at__isnull=True).first()
        if existing is not None:
            return 'already_blocked', existing
        try:
            with transaction.atomic():
                row = RiderBlock.objects.create(rider=rider, reason_code=reason_code, note=note,
                                                created_by=actor)
        except IntegrityError:
            return 'already_blocked', RiderBlock.objects.filter(
                rider=rider, lifted_at__isnull=True).first()
        record_admin_action(http_request, action='rider_blocked', target_type=TARGET,
                            target_id=rider.user_id_id, before={'blocked': False},
                            after={'blocked': True, 'block_id': row.id,
                                   'reason_code': reason_code},
                            reason=note or BLOCK_REASONS.get(reason_code, reason_code))
        # A block also ends the rider's sessions, but never under a trip.
        from .block_signout import sign_out_or_defer

        sign_out_or_defer(rider, row, http_request=http_request)
    return 'blocked', row


def operator_unblock(rider_pk, *, note='', actor=None, http_request=None):
    """Lift a block. Returns 'unblocked' | 'not_blocked' | 'not_found'."""
    with transaction.atomic():
        try:
            rider = _locked(rider_pk)
        except Rider.DoesNotExist:
            return 'not_found'
        row = (RiderBlock.objects.select_for_update()
               .filter(rider=rider, lifted_at__isnull=True).first())
        if row is None:
            return 'not_blocked'
        row.lifted_at, row.lifted_by, row.lift_note = timezone.now(), actor, note
        row.save(update_fields=['lifted_at', 'lifted_by', 'lift_note'])
        record_admin_action(http_request, action='rider_unblocked', target_type=TARGET,
                            target_id=rider.user_id_id, before={'blocked': True},
                            after={'blocked': False, 'block_id': row.id},
                            reason=note or 'unblocked')
    return 'unblocked'


def _block_params(data):
    reason_code = _text(data, 'reason_code', required=True, limit=32)
    if reason_code not in BLOCK_REASONS:
        raise BadInput('INVALID_REASON',
                       f'reason_code must be one of {", ".join(BLOCK_REASONS)}.', 'reason_code')
    note = _text(data, 'note', required=reason_code == 'other', limit=2000)
    return reason_code, note


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.block')])
def admin_block_rider(request, rider_id):
    """POST /rider/admin/<id>/block/  {reason_code, note}"""
    rider, _node, refusal = _rider_for(request, rider_id, 'rider.block')
    if refusal:
        return refusal
    try:
        reason_code, note = _block_params(request.data)
    except BadInput as exc:
        return exc.response()
    outcome, row = operator_block(rider.id, reason_code=reason_code, note=note,
                                  actor=request.user, http_request=request)
    return success_response(
        {'rider_id': rider.user_id_id, 'block_id': row.id, 'reason_code': row.reason_code,
         'created': outcome == 'blocked'},
        status.HTTP_201_CREATED if outcome == 'blocked' else status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.block')])
def admin_unblock_rider(request, rider_id):
    """POST /rider/admin/<id>/unblock/  {note}"""
    rider, _node, refusal = _rider_for(request, rider_id, 'rider.block')
    if refusal:
        return refusal
    try:
        note = _text(request.data, 'note', required=True, limit=2000)
    except BadInput as exc:
        return exc.response()
    outcome = operator_unblock(rider.id, note=note, actor=request.user, http_request=request)
    return success_response({'rider_id': rider.user_id_id, 'changed': outcome == 'unblocked'},
                            status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Note, flag / unflag, force sign-out
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.note')])
def admin_note_rider(request, rider_id):
    """POST /rider/admin/<id>/note/  {body, idempotency_key?}  Internal only."""
    rider, _node, refusal = _rider_for(request, rider_id, 'rider.note')
    if refusal:
        return refusal
    try:
        body = _text(request.data, 'body', required=True, limit=5000)
        key = _idempotency_key(request, required=False)
    except BadInput as exc:
        return exc.response()
    scoped_key = f'rider_note:{rider.id}:{key}' if key else None
    existing = RiderNote.objects.filter(idempotency_key=scoped_key).first() if scoped_key else None
    if existing is None:
        try:
            with transaction.atomic():
                note = RiderNote.objects.create(rider=rider, body=body, author=request.user,
                                                idempotency_key=scoped_key)
        except IntegrityError:
            existing = RiderNote.objects.filter(idempotency_key=scoped_key).first()
    if existing is not None:
        return success_response({'rider_id': rider.user_id_id, 'note_id': existing.id,
                                 'created': False}, status.HTTP_200_OK)
    record_admin_action(request, action='rider_note_added', target_type=TARGET,
                        target_id=rider.user_id_id, after={'note_id': note.id, 'body': body},
                        reason='internal note')
    return success_response({'rider_id': rider.user_id_id, 'note_id': note.id, 'created': True},
                            status.HTTP_201_CREATED)


def _flag_reason(data, required=True):
    reason = _text(data, 'reason', required=required, limit=32)
    if reason and reason not in FLAG_REASONS:
        raise BadInput('INVALID_REASON', f'reason must be one of {", ".join(FLAG_REASONS)}.',
                       'reason')
    return reason


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.flag')])
def admin_flag_rider(request, rider_id):
    """POST /rider/admin/<id>/flag/  {reason, note}"""
    rider, _node, refusal = _rider_for(request, rider_id, 'rider.flag')
    if refusal:
        return refusal
    try:
        reason = _flag_reason(request.data)
        note = _text(request.data, 'note', limit=2000)
    except BadInput as exc:
        return exc.response()
    created = False
    with transaction.atomic():
        _locked(rider.id)
        flag = RiderFlag.objects.filter(rider=rider, reason=reason, cleared_at__isnull=True).first()
        if flag is None:
            flag = RiderFlag.objects.create(rider=rider, reason=reason, note=note,
                                            created_by=request.user)
            created = True
            record_admin_action(request, action='rider_flagged', target_type=TARGET,
                                target_id=rider.user_id_id, after={'flag_id': flag.id,
                                                                   'reason': reason},
                                reason=note or FLAG_REASONS[reason])
    return success_response(
        {'rider_id': rider.user_id_id, 'flag_id': flag.id, 'reason': flag.reason,
         'created': created},
        status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.flag')])
def admin_unflag_rider(request, rider_id):
    """POST /rider/admin/<id>/unflag/  {reason (all when absent), note}"""
    rider, _node, refusal = _rider_for(request, rider_id, 'rider.flag')
    if refusal:
        return refusal
    try:
        reason = _flag_reason(request.data, required=False)
        note = _text(request.data, 'note', limit=2000)
    except BadInput as exc:
        return exc.response()
    with transaction.atomic():
        active = RiderFlag.objects.select_for_update().filter(rider=rider, cleared_at__isnull=True)
        if reason:
            active = active.filter(reason=reason)
        ids = list(active.values_list('id', flat=True))
        if ids:
            RiderFlag.objects.filter(id__in=ids).update(
                cleared_at=timezone.now(), cleared_by=request.user, clear_note=note)
            record_admin_action(request, action='rider_unflagged', target_type=TARGET,
                                target_id=rider.user_id_id, before={'flag_ids': ids},
                                after={'cleared': len(ids)}, reason=note or 'cleared')
    return success_response({'rider_id': rider.user_id_id, 'cleared': len(ids)},
                            status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.signout')])
def admin_sign_out_rider(request, rider_id):
    """POST /rider/admin/<id>/sign-out/  {reason}

    Blacklists every refresh token the account holds (the primitive a
    password change uses). Access tokens already issued stay valid until
    they expire (15 minutes; see `auth_user.session_revocation`). A repeat
    with nothing left to revoke changes nothing and writes no audit row.
    """
    from servers.auth_user.session_revocation import revoke_all_refresh_tokens

    rider, _node, refusal = _rider_for(request, rider_id, 'rider.signout')
    if refusal:
        return refusal
    try:
        reason = _text(request.data, 'reason', required=True, limit=2000)
    except BadInput as exc:
        return exc.response()
    revoked = revoke_all_refresh_tokens(rider.user_id)
    if revoked:
        record_admin_action(request, action='rider_signed_out', target_type=TARGET,
                            target_id=rider.user_id_id,
                            after={'refresh_tokens_revoked': revoked}, reason=reason)
    return success_response({'rider_id': rider.user_id_id, 'revoked': revoked,
                             'changed': bool(revoked)}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

def execute_bulk(request_row):
    """Run a bulk block or unblock row by row; returns per-row results."""
    from django.contrib.auth import get_user_model

    p = request_row.payload or {}
    action = p.get('action')
    if action not in BULK_ACTIONS:
        raise registry.ExecutionRefused('UNKNOWN_BULK_ACTION', f'Unknown action {action!r}.')
    maker = get_user_model().objects.filter(pk=request_row.maker_id).first()
    profiles = dict(Rider.objects.filter(user_id_id__in=p.get('rider_ids') or [])
                    .values_list('user_id_id', 'id'))
    results = []
    for rider_id in p.get('rider_ids') or []:
        pk = profiles.get(rider_id)
        if pk is None:
            outcome = 'not_found'
        elif action == 'block':
            outcome, _row = operator_block(pk, reason_code=p.get('reason_code'),
                                           note=p.get('note') or '', actor=maker)
        else:
            outcome = operator_unblock(pk, note=p.get('note') or '', actor=maker)
        results.append({'id': rider_id, 'result': outcome})
    for rider_id in p.get('not_found_ids') or []:
        results.append({'id': rider_id, 'result': 'not_found'})
    counts = {}
    for row in results:
        counts[row['result']] = counts.get(row['result'], 0) + 1
    return {'action': action, 'results': results, 'counts': counts}


bulk.register('rider_bulk')(execute_bulk)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.block')])
def admin_bulk_riders(request):
    """POST /rider/admin/bulk/
    {action: block|unblock, rider_ids, reason_code, note, idempotency_key}

    Up to the bulk.action limit (50 records in India) it runs now with
    per-row results; above it a checker must approve (202). Riders the
    caller cannot write are reported not_found, as a single write would.
    """
    data = request.data
    try:
        action = _text(data, 'action', required=True, limit=16)
        if action not in BULK_ACTIONS:
            raise BadInput('INVALID_ACTION', f'action must be one of {", ".join(BULK_ACTIONS)}.',
                           'action')
        raw_ids = data.get('rider_ids')
        if not isinstance(raw_ids, list) or not raw_ids:
            raise BadInput('RIDER_IDS_REQUIRED', 'rider_ids must be a non-empty list.',
                           'rider_ids')
        if len(raw_ids) > BULK_MAX_IDS:
            raise BadInput('TOO_MANY', f'At most {BULK_MAX_IDS} riders per bulk action.',
                           'rider_ids')
        ids = list(dict.fromkeys(_int(raw, 'rider_ids') for raw in raw_ids))
        params = {'action': action}
        if action == 'block':
            reason_code, note = _block_params(data)
            params.update(reason_code=reason_code, note=note)
        else:
            params.update(note=_text(data, 'note', required=True, limit=2000))
        key = _idempotency_key(request)
    except BadInput as exc:
        return exc.response()

    visible = list(riders_visible_to(
        request.user, None, annotate(Rider.objects.select_related('user_id'))
    ).filter(user_id_id__in=ids))
    writable = {r.user_id_id: r for r in visible
                if rider_may(request.user, 'rider.block', r)}
    nodes = _nodes(r.city_node_id for r in writable.values())
    ordered = [i for i in ids if i in writable]
    not_found = [i for i in ids if i not in writable]
    if not ordered:
        return success_response({'state': 'executed', 'count': 0, 'result': {
            'action': action, 'results': [{'id': i, 'result': 'not_found'} for i in ids],
            'counts': {'not_found': len(ids)}}}, status.HTTP_200_OK)
    countries = {_country(r, node_of_rider(r)) for r in writable.values()}
    if len(countries) != 1:
        return error_response(
            code='MIXED_COUNTRIES', message='A bulk action covers one country at a time.',
            field='rider_ids', issue=', '.join(sorted(countries)),
            status=status.HTTP_400_BAD_REQUEST)
    city_ids = {r.city_node_id for r in writable.values()}
    try:
        outcome = approvals.require_approval_or_execute(
            BULK_ACTION, actor=request.user, country=countries.pop(), amount=len(ordered),
            scope_node=nodes.get(city_ids.pop()) if len(city_ids) == 1 else None,
            target_type='rider_bulk', target_id=f'{action}:{len(ordered)}',
            payload={**params, 'rider_ids': ordered, 'not_found_ids': not_found},
            idempotency_key=f'rider_bulk:{key}',
            reason=params.get('note') or params.get('reason_code') or '',
            http_request=request,
        )
    except approvals.ApprovalError as exc:
        return exc.response()
    return _approval_answer(outcome, {'count': len(ordered)})


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.block')])
def admin_clear_restriction(request, rider_id):
    """POST /rider/admin/<id>/restriction/clear/  {note}

    Clears the online-payment-only restriction (`cash_disabled`). `rider.block` is the capability
    that sets and clears every restriction on this page."""
    from .models import RiderRestriction

    rider, _node, refusal = _rider_for(request, rider_id, 'rider.block')
    if refusal:
        return refusal
    try:
        note = _text(request.data, 'note', required=True, limit=2000)
    except BadInput as exc:
        return exc.response()
    with transaction.atomic():
        _locked(rider.id)
        rows = list(RiderRestriction.objects.select_for_update().filter(
            rider=rider, kind=RiderRestriction.CASH_DISABLED, cleared_at__isnull=True))
        for row in rows:
            row.cleared_at, row.cleared_by, row.clear_note = timezone.now(), request.user, note
            row.save(update_fields=['cleared_at', 'cleared_by', 'clear_note'])
        if rows:
            record_admin_action(request, action='rider_cash_enabled', target_type=TARGET,
                                target_id=rider.user_id_id,
                                before={'cash_disabled': True}, after={'cash_disabled': False},
                                reason=note)
    return success_response({'rider_id': rider.user_id_id, 'cleared': len(rows)},
                            status.HTTP_200_OK)
