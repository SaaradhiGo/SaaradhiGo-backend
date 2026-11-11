"""Support module (operator console, module 5): queue, ticket page, writes.

WHAT LIVES HERE (all under /support/admin/)
-------------------------------------------
* `GET tickets/` the queue: filters, sort, pages, per-tab counts.
* `GET tickets/<id>/` the ticket: thread (internal notes flagged), requester,
  trip and fare, SLA, assignee, refund state, allowed actions, audit trail.
* Writes: `reply`, `note`, `status`, `priority`, `category`, `assign`.
* `GET tickets/<id>/assignees/` operators who may be assigned the ticket.
* Macros (`macros/`) and the SLA policy (`sla/`).

RULES THIS FILE KEEPS
---------------------
* ONE scope rule (`scope.tickets_visible_to`) for the list, the page, every
  write, the assignee list and macro filtering. Out of scope is 404; in
  scope without the capability AT THE TICKET is 403.
* Every write is audited with before / after (`record_admin_action`,
  target `support_ticket`) and idempotent: a repeat changes nothing and
  writes no second audit row. Replies and notes also take an idempotency key.
* Internal notes are `SupportMessage(author_role='internal')`. They are
  returned here, flagged, and never by any rider or driver endpoint (the
  model's default manager leaves them out).
* Nothing here moves money. Fare review calls the EXISTING trip refund
  endpoint (support.refund, maker-checker); that endpoint records the
  ticket link and posts the system message (`service.post_system_message`).
* Contact details inside free text are masked in the payload
  (`text.mask_free_text`); the whole thread is available through the logged
  reveal (target `support_ticket`, field `thread`).
"""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES
from base.permissions import is_operator
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted, node_of
from servers.admin_access.models import COUNTRY, GeoNode
from servers.admin_access.scoping import ScopeNotHeld, requested_node
from servers.admin_audit.services import record_admin_action
from servers.ride.admin_trips import (
    BadInput, _date, _fare_minor, _iso, _payment_summary, _staff, _text, _csv,
    _idempotency_key, _int,
)

from . import macros as macro_lib
from .models import (
    SupportCountryConfig, SupportMacro, SupportMessage, SupportSlaPolicy, SupportTicket,
)
from .scope import profile, ticket_may, ticket_place, tickets_visible_to
from .sla import (
    DEFAULT_MINUTES, DUE_SOON_MINUTES, PRIORITIES, PRIORITY_RANK, apply_due_times,
    enter_status, sla_state,
)
from .text import mask_free_text

logger = logging.getLogger(__name__)

TARGET = 'support_ticket'
MACRO_TARGET = 'support_macro'
SLA_TARGET = 'support_sla'

STATUSES = dict(SupportTicket.STATUS_CHOICES)
ISSUES = dict(SupportTicket.ISSUE_CHOICES)
OPEN_STATUSES = ('OPEN', 'IN_PROGRESS', 'WAITING_USER')

SORTS = {
    'created': 'created_at',
    'updated': 'updated_at',
    'resolution_due': 'resolution_due_at',
    'priority': 'priority_rank',
}
DEFAULT_SORT = '-created'
MAX_BODY = 5000

User = get_user_model()

TICKET_QUERYSET = SupportTicket.objects.select_related(
    'user_id', 'user_id__driver', 'user_id__driver__home_city',
    'user_id__rider', 'user_id__rider__home_city',
    'trip_id', 'trip_id__zone', 'trip_id__zone__geo_node', 'assigned_to',
)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _not_found(ticket_id):
    """The one answer for "does not exist" AND "not in your scope"."""
    return error_response(
        code='NOT_FOUND', message='Ticket not found.', field='ticket_id',
        issue=f'Ticket {ticket_id}', status=status.HTTP_404_NOT_FOUND)


def _denied(permission):
    return error_response(
        code='PERMISSION_DENIED',
        message=f'You do not hold {permission} where this ticket sits.',
        field='ticket_id', issue=permission, status=status.HTTP_403_FORBIDDEN)


def _scope_refused(exc):
    return error_response(
        code='SCOPE_NOT_HELD', message=str(exc), field='scope',
        issue='Requested scope is outside your assignments',
        status=status.HTTP_403_FORBIDDEN)


def _ticket_for(request, ticket_id, permission=None, queryset=None):
    """(ticket, None) or (None, error response)."""
    qs = queryset if queryset is not None else TICKET_QUERYSET
    ticket = tickets_visible_to(request.user, None, qs).filter(id=ticket_id).first()
    if ticket is None:
        return None, _not_found(ticket_id)
    if permission and not ticket_may(request.user, permission, ticket):
        return None, _denied(permission)
    return ticket, None


def _locked(ticket_id):
    return SupportTicket.objects.select_for_update().get(pk=ticket_id)


def _snapshot(ticket):
    return {
        'status': ticket.status, 'priority': ticket.priority,
        'issue_type': ticket.issue_type, 'assigned_to': ticket.assigned_to_id,
        'first_responded_at': _iso(ticket.first_responded_at),
        'resolved_at': _iso(ticket.resolved_at),
        'resolution_due_at': _iso(ticket.resolution_due_at),
        'waiting_since': _iso(ticket.waiting_since),
        'resolution_paused_seconds': ticket.resolution_paused_seconds,
        'returned_at': _iso(ticket.returned_at),
    }


def _requester(ticket):
    user = ticket.user_id
    driver, rider = profile(user, 'driver'), profile(user, 'rider')
    if driver is not None:
        kind, page = 'driver', {'type': 'driver', 'id': driver.id}
    elif rider is not None:
        kind, page = 'rider', {'type': 'rider', 'id': user.id}
    else:
        kind, page = (user.role or 'user'), None
    return {
        'id': user.id, 'role': kind, 'page': page,
        'full_name': user.full_name, 'phone_number': user.phone_number,
        'email': user.email,
    }


def _author_name(message):
    if message.author_role != 'user':
        return message.author_role
    if not message.author_id:
        return 'user'
    return message.author.full_name or message.author.phone_number


def _message(message):
    internal = message.author_role == 'internal'
    return {
        'id': message.id,
        'ticket': message.ticket_id,
        'author': message.author_id,
        'author_role': message.author_role,
        'author_name': _author_name(message),
        'internal': internal,
        'body': mask_free_text(message.body),
        'created_at': _iso(message.created_at),
        'created_by': (_staff(message.author)
                       if message.author_role in ('support', 'internal') else None),
    }


def _row(ticket, now):
    return {
        'id': ticket.id,
        'user_id': ticket.user_id_id,
        'trip_id': ticket.trip_id_id,
        'issue_type': ticket.issue_type,
        'issue_label': ISSUES.get(ticket.issue_type, ticket.issue_type),
        'status': ticket.status,
        'priority': ticket.priority,
        'description': mask_free_text(ticket.description),
        'assigned_to': ticket.assigned_to_id,
        'assignee': _staff(ticket.assigned_to),
        'requester': _requester(ticket),
        'city': ticket_place(ticket),
        'country': ticket.country or None,
        'created_at': _iso(ticket.created_at),
        'updated_at': _iso(ticket.updated_at),
        'resolved_at': _iso(ticket.resolved_at),
        'first_responded_at': _iso(ticket.first_responded_at),
        'returned_at': _iso(ticket.returned_at),
        'returned_reason': (mask_free_text(ticket.returned_reason)
                            if ticket.returned_at else None),
        'sla': sla_state(ticket, now),
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class TicketPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


def _annotate(queryset):
    return queryset.annotate(priority_rank=Case(
        *[When(priority=name, then=Value(rank)) for name, rank in PRIORITY_RANK.items()],
        default=Value(2), output_field=IntegerField()))


def _breached_q(now):
    """Open and past a deadline that has not been met."""
    # A paused ticket (waiting on the user) does not breach resolution.
    return Q(status__in=OPEN_STATUSES) & (
        Q(first_responded_at__isnull=True, first_response_due_at__lt=now)
        | Q(resolution_due_at__lt=now, waiting_since__isnull=True))


def _due_soon_q(now):
    soon = now + timedelta(minutes=DUE_SOON_MINUTES)
    return Q(status__in=OPEN_STATUSES) & ~_breached_q(now) & (
        Q(first_responded_at__isnull=True, first_response_due_at__lte=soon)
        | Q(resolution_due_at__lte=soon, waiting_since__isnull=True))


def _day_range_q(date_from, date_to):
    """created_at within [date_from, date_to] in each ticket's own country day."""
    def bounds(tz):
        q = Q()
        if date_from:
            q &= Q(created_at__gte=datetime.combine(date_from, time.min, tzinfo=tz))
        if date_to:
            q &= Q(created_at__lt=datetime.combine(
                date_to + timedelta(days=1), time.min, tzinfo=tz))
        return q

    combined = ~Q(country__in=list(COUNTRIES)) & bounds(timezone.get_current_timezone())
    for code, country in COUNTRIES.items():
        combined |= Q(country=code) & bounds(ZoneInfo(country.default_timezone))
    return combined


def _q_filter(queryset, text):
    from base.db_indexes import phone_tail
    from servers.search.terms import TermError, parse_term

    text = text.strip().lstrip('#')
    cond = Q()
    if text.isdigit() and len(text) <= 9:
        cond |= Q(id=int(text))
    if text.isdigit() and len(text) == 4:
        # The tail the masked number shows: `+91••••••3210`.
        cond |= Q(user_id__phone_number__endswith=text)
    try:
        term = parse_term(text)
    except TermError:
        term = None
    if term is not None and term.searches_phones:
        queryset = queryset.annotate(phone_tail=phone_tail('user_id__phone_number'))
        if term.phone_e164:
            cond |= Q(user_id__phone_number=term.phone_e164)
        if term.phone_tail:
            cond |= Q(phone_tail=term.phone_tail)
        if term.phone_prefix:
            cond |= Q(phone_tail__startswith=term.phone_prefix)
    return queryset.filter(cond) if cond else queryset.none()


def filter_tickets(request, queryset, now):
    params = request.query_params
    statuses = _csv(params.get('status'))
    if statuses:
        wanted = set()
        for name in statuses:
            if name.lower() == 'open':
                wanted.update(OPEN_STATUSES)
            elif name.upper() in STATUSES:
                wanted.add(name.upper())
            else:
                raise BadInput('INVALID_FILTER', f'Unknown status {name!r}.', 'status')
        queryset = queryset.filter(status__in=wanted)

    priorities = _csv(params.get('priority'))
    if priorities:
        bad = [p for p in priorities if p not in PRIORITY_RANK]
        if bad:
            raise BadInput('INVALID_FILTER', f'Unknown priority {bad[0]!r}.', 'priority')
        queryset = queryset.filter(priority__in=priorities)

    issues = _csv(params.get('issue_type'))
    if issues:
        queryset = queryset.filter(issue_type__in=issues)

    assigned = (params.get('assigned') or '').strip().lower()
    if assigned:
        if assigned == 'me':
            queryset = queryset.filter(assigned_to=request.user)
        elif assigned == 'none':
            queryset = queryset.filter(assigned_to__isnull=True)
        else:
            queryset = queryset.filter(assigned_to_id=_int(assigned, 'assigned'))

    breach = (params.get('breach') or '').strip().lower()
    if breach == 'breached':
        queryset = queryset.filter(_breached_q(now))
    elif breach == 'due_soon':
        queryset = queryset.filter(_due_soon_q(now))
    elif breach:
        raise BadInput('INVALID_FILTER', 'breach must be breached or due_soon.', 'breach')

    if (params.get('returned') or '').strip().lower() in ('me', '1', 'true'):
        queryset = queryset.filter(
            assigned_to=request.user, returned_at__isnull=False, status__in=OPEN_STATUSES)

    country = (params.get('country') or '').strip().upper()
    if country:
        queryset = queryset.filter(country=country)

    date_from, date_to = _date(params, 'date_from'), _date(params, 'date_to')
    if date_from and date_to and date_from > date_to:
        raise BadInput('INVALID_DATE', 'date_from is after date_to.', 'date_from')
    if date_from or date_to:
        queryset = queryset.filter(_day_range_q(date_from, date_to))

    text = (params.get('q') or '').strip()
    if text:
        queryset = _q_filter(queryset, text)

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
    if key == 'priority':
        # Most urgent first, then the oldest: the order a queue is worked in.
        return queryset.order_by(column, 'created_at', 'id')
    return queryset.order_by(column, '-id' if ordering.startswith('-') else 'id')


def queue_counts(user, node, now):
    """Tab counts over the caller's scope (not narrowed by the other filters)."""
    base = tickets_visible_to(user, node, SupportTicket.objects.all())
    open_q = Q(status__in=OPEN_STATUSES)
    return base.aggregate(
        mine=Count('id', filter=open_q & Q(assigned_to=user)),
        unassigned=Count('id', filter=open_q & Q(assigned_to__isnull=True)),
        breaching=Count('id', filter=_breached_q(now)),
        open=Count('id', filter=open_q),
        closed=Count('id', filter=Q(status='CLOSED')),
        returned=Count('id', filter=open_q & Q(assigned_to=user, returned_at__isnull=False)),
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('support.read')])
def admin_list_tickets(request):
    """GET /support/admin/tickets/  The queue, in the caller's scope."""
    now = timezone.now()
    try:
        node = requested_node(request)
    except ScopeNotHeld as exc:
        return _scope_refused(exc)
    try:
        tickets = filter_tickets(
            request, tickets_visible_to(request.user, node, _annotate(TICKET_QUERYSET)), now)
    except BadInput as exc:
        return exc.response()

    paginator = TicketPagination()
    page = paginator.paginate_queryset(tickets, request)
    django_page = paginator.page
    return success_response({
        'count': django_page.paginator.count,
        'next': paginator.get_next_link(),
        'previous': paginator.get_previous_link(),
        'page': django_page.number,
        'page_size': paginator.get_page_size(request),
        'total_pages': django_page.paginator.num_pages,
        'ordering': request.query_params.get('ordering') or DEFAULT_SORT,
        'queues': queue_counts(request.user, node, now),
        'results': [_row(t, now) for t in page],
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def _audit_rows(ticket):
    from servers.admin_audit.models import AdminAuditLog

    return [{
        'id': row.id, 'action': row.action, 'created_at': _iso(row.created_at),
        'reason': row.reason, 'before': row.before, 'after': row.after,
        'created_by': {'id': row.actor_id, 'label': row.actor_label, 'role': row.actor_role},
    } for row in AdminAuditLog.objects.filter(
        target_type=TARGET, target_id=str(ticket.id)).order_by('-created_at', '-id')[:100]]


def _refund_section(user, ticket, node):
    """Fare review state for a ticket with a trip. Never moves money."""
    from servers.approvals.actions import FINANCE_REFUND, SUPPORT_REFUND
    from servers.approvals.models import ApprovalRequest
    from servers.approvals.service import active_policy
    from servers.ride.admin_trips import TARGET as TRIP_TARGET
    from servers.ride.admin_trips import refundable_minor, trip_country
    from servers.ride.models import TripMoneyAction

    trip = ticket.trip_id
    may = granted(user, 'support.refund', node)[0]
    refundable = refundable_minor(trip)
    action_key = (FINANCE_REFUND if granted(user, 'finance.read', node)[0]
                  else SUPPORT_REFUND) if may else None
    policy = None
    if action_key:
        row = active_policy(trip_country(trip) or '', action_key)
        if row is not None:
            policy = {'limit_kind': row.limit_kind, 'maker_limit': row.maker_limit,
                      'currency': row.currency, 'always_checker': row.always_checker}
    linked = [r for r in ApprovalRequest.objects.filter(
        target_type=TRIP_TARGET, target_id=str(trip.id)).order_by('-id')[:50]
        if (r.payload or {}).get('ticket_id') == ticket.id]
    money = {m.approval_request_id: m for m in TripMoneyAction.objects.filter(
        trip=trip, approval_request_id__in=[r.id for r in linked])}
    return {
        'may_refund': may and refundable > 0,
        'refundable_minor': refundable,
        'currency': trip.currency,
        'action_key': action_key,
        'policy': policy,
        'requests': [{
            'id': r.id, 'status': r.status, 'amount_minor': r.amount,
            'currency': r.currency, 'requested_at': _iso(r.requested_at),
            'money_action_status': money[r.id].status if r.id in money else None,
        } for r in linked],
    }


def allowed_actions(user, ticket, refund=None):
    """What this caller may do to this ticket, so the console shows only that."""
    def may(permission):
        return ticket_may(user, permission, ticket)

    replying = may('support.reply')
    return {
        'reply': replying,
        'note': replying,
        'status': replying,
        'priority': replying,
        'category': replying,
        'assign': replying,
        'macros': replying,
        'reveal': may('pii.view'),
        'refund': bool(refund and refund['may_refund']),
        'statuses': list(STATUSES),
        'priorities': list(PRIORITIES),
        'issue_types': ISSUES,
    }


def _trip_section(user, ticket):
    trip = ticket.trip_id
    if trip is None:
        return None, None
    node = node_of(trip)
    zone = trip.zone
    summary = {
        'id': trip.id,
        'status': trip.status_id.status_code if trip.status_id else None,
        'requested_at': _iso(trip.requested_at),
        'completed_at': _iso(trip.completed_at),
        'pickup_address': trip.pickup_address,
        'destination_address': trip.destination_address,
        'payment_method': trip.payment_method,
        'currency': trip.currency,
        'city': ({'code': node.code, 'name': node.name} if node else
                 ({'code': zone.code, 'name': zone.city} if zone else None)),
    }
    if granted(user, 'trip.read', node)[0]:
        summary['fare'] = _fare_minor(trip)
        summary['payment'] = _payment_summary(trip)
    else:
        summary['fare'] = None
        summary['payment'] = None
    return summary, node


def ticket_payload(request, ticket):
    now = timezone.now()
    data = _row(ticket, now)
    messages = (SupportMessage.everything.filter(ticket=ticket)
                .select_related('author').order_by('created_at', 'id'))
    trip, node = _trip_section(request.user, ticket)
    refund = _refund_section(request.user, ticket, node) if ticket.trip_id_id else None
    data.update({
        'messages': [_message(m) for m in messages],
        'trip': trip,
        'refund': refund,
        'audit_trail': _audit_rows(ticket),
        'allowed_actions': allowed_actions(request.user, ticket, refund),
    })
    return data


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('support.read')])
def admin_ticket_detail(request, ticket_id):
    """GET /support/admin/tickets/<id>/  One ticket, if it is in the caller's scope."""
    ticket, refusal = _ticket_for(request, ticket_id, 'support.read')
    if refusal:
        return refusal
    return success_response(ticket_payload(request, ticket), status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _body(data, *, required=True):
    return _text(data, 'body', required=required, limit=MAX_BODY)


def _new_status(data):
    value = data.get('status')
    if value in (None, ''):
        return None
    value = str(value).strip().upper()
    if value not in STATUSES:
        raise BadInput('INVALID_STATUS',
                       f'status must be one of {", ".join(STATUSES)}.', 'status')
    return value


STATUS_FIELDS = ['status', 'resolved_at']


def _set_status(ticket, new_status, now):
    """Move the ticket to `new_status`, keeping resolved_at and the
    waiting-on-user pause honest. Returns every field to save."""
    extra = enter_status(ticket, new_status, now)
    if new_status == 'CLOSED':
        if ticket.resolved_at is None:
            ticket.resolved_at = now
        if ticket.returned_at is not None:
            ticket.returned_at, ticket.returned_reason = None, ''
            extra += ['returned_at', 'returned_reason']
    else:
        ticket.resolved_at = None
    return STATUS_FIELDS + extra


def _clear_returned(ticket):
    """The agent has picked the returned ticket up again."""
    if ticket.returned_at is None:
        return []
    ticket.returned_at, ticket.returned_reason = None, ''
    return ['returned_at', 'returned_reason']


def _macro_for(ticket, macro_id):
    macro = SupportMacro.objects.filter(id=macro_id, active=True).first()
    if macro is None:
        raise BadInput('MACRO_NOT_FOUND', 'That macro does not exist or is inactive.',
                       'macro_id')
    if macro.country and ticket.country and macro.country != ticket.country:
        raise BadInput('MACRO_NOT_FOR_TICKET',
                       'That macro is for another country.', 'macro_id')
    if macro.issue_types and ticket.issue_type not in macro.issue_types:
        raise BadInput('MACRO_NOT_FOR_TICKET',
                       'That macro is not for this kind of ticket.', 'macro_id')
    return macro


def _existing_message(scoped_key):
    return SupportMessage.everything.filter(idempotency_key=scoped_key).first() \
        if scoped_key else None


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def admin_reply(request, ticket_id):
    """POST /support/admin/tickets/<id>/reply/
    {body, status?, macro_id?, idempotency_key?}

    The first reply stamps `first_responded_at` (stops the first-response
    clock). With `macro_id` and no body, the macro is rendered server-side.
    """
    ticket, refusal = _ticket_for(request, ticket_id, 'support.reply')
    if refusal:
        return refusal
    try:
        next_status = _new_status(request.data)
        key = _idempotency_key(request, required=False)
        macro = None
        macro_id = request.data.get('macro_id')
        if macro_id not in (None, ''):
            macro = _macro_for(ticket, _int(macro_id, 'macro_id'))
        body = _body(request.data, required=macro is None)
        if not body and macro is not None:
            body = macro_lib.render(macro.body, ticket)[0]
        if not body:
            raise BadInput('BODY_REQUIRED', 'body is required.', 'body')
    except BadInput as exc:
        return exc.response()

    scoped_key = f'reply:{ticket.id}:{key}' if key else None
    existing = _existing_message(scoped_key)
    if existing is not None:
        return success_response({**_message(existing), 'created': False,
                                 'status': ticket.status}, status.HTTP_200_OK)
    if ticket.status == 'CLOSED' and next_status in (None, 'CLOSED'):
        return error_response(
            code='TICKET_CLOSED',
            message='This ticket is closed. Choose a status to reopen it with the reply.',
            field='status', issue='status=CLOSED', status=status.HTTP_409_CONFLICT)

    now = timezone.now()
    try:
        with transaction.atomic():
            locked = _locked(ticket.id)
            before = _snapshot(locked)
            message = SupportMessage.everything.create(
                ticket=locked, author=request.user, author_role='support', body=body,
                idempotency_key=scoped_key)
            fields = ['updated_at']
            if locked.first_responded_at is None:
                locked.first_responded_at = now
                fields.append('first_responded_at')
            if next_status is not None:
                fields += _set_status(locked, next_status, now)
            elif locked.status == 'OPEN':
                fields += _set_status(locked, 'IN_PROGRESS', now)
            fields += _clear_returned(locked)
            fields = list(dict.fromkeys(fields))
            locked.save(update_fields=fields)
            record_admin_action(
                request, action='support_ticket_replied', target_type=TARGET,
                target_id=locked.id, before=before,
                after={**_snapshot(locked), 'message_id': message.id,
                       'macro_id': macro.id if macro else None,
                       'body_chars': len(body)},
                reason='reply' + (f' via macro {macro.id}' if macro else ''))
    except IntegrityError:
        existing = _existing_message(scoped_key)
        if existing is None:
            raise
        return success_response({**_message(existing), 'created': False,
                                 'status': ticket.status}, status.HTTP_200_OK)
    locked.refresh_from_db()
    return success_response({**_message(message), 'created': True, 'status': locked.status,
                             'sla': sla_state(locked, now)}, status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def admin_note(request, ticket_id):
    """POST /support/admin/tickets/<id>/note/  {body, idempotency_key?}  Internal only."""
    ticket, refusal = _ticket_for(request, ticket_id, 'support.reply')
    if refusal:
        return refusal
    try:
        body = _body(request.data)
        key = _idempotency_key(request, required=False)
    except BadInput as exc:
        return exc.response()
    scoped_key = f'note:{ticket.id}:{key}' if key else None
    existing = _existing_message(scoped_key)
    if existing is not None:
        return success_response({**_message(existing), 'created': False}, status.HTTP_200_OK)
    try:
        with transaction.atomic():
            note = SupportMessage.everything.create(
                ticket=ticket, author=request.user, author_role='internal', body=body,
                idempotency_key=scoped_key)
            SupportTicket.objects.filter(pk=ticket.pk).update(updated_at=timezone.now())
            record_admin_action(
                request, action='support_ticket_noted', target_type=TARGET,
                target_id=ticket.id, after={'message_id': note.id, 'body_chars': len(body)},
                reason='internal note')
    except IntegrityError:
        existing = _existing_message(scoped_key)
        if existing is None:
            raise
        return success_response({**_message(existing), 'created': False}, status.HTTP_200_OK)
    return success_response({**_message(note), 'created': True}, status.HTTP_201_CREATED)


def _change(request, ticket, field_name, apply, action):
    """Shared shape of status / priority / category: lock, compare, write once.

    `apply(locked, now)` mutates and returns the extra `update_fields`, or
    None when nothing needs to change (an idempotent repeat).
    """
    try:
        reason = _text(request.data, 'reason', limit=2000) or field_name
    except BadInput as exc:
        return exc.response()
    now = timezone.now()
    with transaction.atomic():
        locked = _locked(ticket.id)
        before = _snapshot(locked)
        fields = apply(locked, now)
        if fields is None:
            return success_response({'ticket_id': locked.id, 'changed': False,
                                     **_snapshot(locked)}, status.HTTP_200_OK)
        locked.save(update_fields=['updated_at', *fields])
        record_admin_action(
            request, action=action, target_type=TARGET, target_id=locked.id,
            before=before, after=_snapshot(locked), reason=reason)
    locked.refresh_from_db()
    return success_response({'ticket_id': locked.id, 'changed': True, **_snapshot(locked),
                             'sla': sla_state(locked, now)}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def admin_set_status(request, ticket_id):
    """POST /support/admin/tickets/<id>/status/  {status, reason?}"""
    try:
        wanted = _new_status(request.data)
        if wanted is None:
            raise BadInput('STATUS_REQUIRED', 'status is required.', 'status')
    except BadInput as exc:
        return exc.response()
    ticket, refusal = _ticket_for(request, ticket_id, 'support.reply')
    if refusal:
        return refusal

    def apply(ticket, now):
        if ticket.status == wanted:
            return None
        return _set_status(ticket, wanted, now)
    return _change(request, ticket, 'status', apply, 'support_ticket_status_changed')


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def admin_set_priority(request, ticket_id):
    """POST /support/admin/tickets/<id>/priority/  {priority, reason?}

    Recomputes both due times from the ticket's `created_at`."""
    wanted = str(request.data.get('priority') or '').strip().lower()
    if wanted not in PRIORITY_RANK:
        return BadInput('INVALID_PRIORITY',
                        f'priority must be one of {", ".join(PRIORITIES)}.',
                        'priority').response()
    ticket, refusal = _ticket_for(request, ticket_id, 'support.reply')
    if refusal:
        return refusal

    def apply(ticket, now):
        if ticket.priority == wanted:
            return None
        ticket.priority = wanted
        apply_due_times(ticket, save=False)
        return ['priority', 'first_response_due_at', 'resolution_due_at']
    return _change(request, ticket, 'priority', apply, 'support_ticket_priority_changed')


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def admin_set_category(request, ticket_id):
    """POST /support/admin/tickets/<id>/category/  {issue_type, reason?}"""
    wanted = str(request.data.get('issue_type') or '').strip()
    if wanted not in ISSUES:
        return BadInput('INVALID_ISSUE_TYPE',
                        f'issue_type must be one of {", ".join(ISSUES)}.',
                        'issue_type').response()
    ticket, refusal = _ticket_for(request, ticket_id, 'support.reply')
    if refusal:
        return refusal

    def apply(ticket, now):
        if ticket.issue_type == wanted:
            return None
        ticket.issue_type = wanted
        return ['issue_type']
    return _change(request, ticket, 'category', apply, 'support_ticket_category_changed')


def eligible_assignee(candidate, ticket):
    """An active operator who holds support.reply where the ticket sits."""
    return (candidate is not None and candidate.is_active and is_operator(candidate)
            and not getattr(candidate, 'is_break_glass', False)
            and ticket_may(candidate, 'support.reply', ticket))


def _assignee_target(request):
    """('me'|'none'|user_id) from {assigned_to | me | none}."""
    data = request.data
    if data.get('me') is True:
        return 'me'
    if data.get('none') is True:
        return 'none'
    value = data.get('assigned_to')
    if value in (None, '') or str(value).strip().lower() == 'none':
        if 'assigned_to' in data:
            return 'none'
        raise BadInput('MISSING_FIELDS', 'assigned_to is required.', 'assigned_to')
    if str(value).strip().lower() == 'me':
        return 'me'
    return _int(value, 'assigned_to')


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def admin_assign(request, ticket_id):
    """POST /support/admin/tickets/<id>/assign/  {assigned_to: <id> | "me" | "none"}

    Only an operator who holds support.reply at this ticket may be assigned.
    """
    ticket, refusal = _ticket_for(request, ticket_id, 'support.reply')
    if refusal:
        return refusal
    try:
        target = _assignee_target(request)
    except BadInput as exc:
        return exc.response()
    if target == 'none':
        assignee = None
    else:
        assignee = request.user if target == 'me' else User.objects.filter(id=target).first()
        if not eligible_assignee(assignee, ticket):
            return error_response(
                code='ASSIGNEE_NOT_ELIGIBLE',
                message='That person cannot be assigned: they must be an active operator '
                        'who can reply to tickets where this one sits.',
                field='assigned_to', issue=f'User {target}',
                status=status.HTTP_400_BAD_REQUEST)

    def apply(locked, now):
        if locked.assigned_to_id == (assignee.id if assignee else None):
            return None
        locked.assigned_to = assignee
        return ['assigned_to', *_clear_returned(locked)]
    return _change(request, ticket, 'assignee', apply, 'support_ticket_assigned')


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def admin_assignees(request, ticket_id):
    """GET /support/admin/tickets/<id>/assignees/  Operators eligible for this ticket."""
    ticket, refusal = _ticket_for(request, ticket_id, 'support.reply')
    if refusal:
        return refusal
    people = []
    candidates = User.objects.filter(is_active=True, is_staff=True).order_by('id')[:500]
    for candidate in candidates:
        if eligible_assignee(candidate, ticket):
            people.append({**_staff(candidate), 'me': candidate.id == request.user.id,
                           'assigned': candidate.id == ticket.assigned_to_id})
    return success_response({'ticket_id': ticket.id, 'results': people}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Macros
# ---------------------------------------------------------------------------

def _macro_dict(macro):
    return {
        'id': macro.id, 'title': macro.title, 'body': macro.body, 'locale': macro.locale,
        'country': macro.country, 'issue_types': list(macro.issue_types or []),
        'active': macro.active, 'created_by': _staff(macro.created_by),
        'updated_at': _iso(macro.updated_at),
        'version': (macro.versions.order_by('-version').values_list('version', flat=True)
                    .first() or 0) if macro.pk else 0,
    }


def _macro_scope_ok(user, country, permission):
    """May the caller manage a macro for `country` (None = every country)?"""
    from servers.rider.scope import unrestricted

    if country is None:
        return unrestricted(user) and granted(user, permission)[0]
    node = GeoNode.objects.filter(kind=COUNTRY, code=country).first()
    return granted(user, permission, node)[0] if node else granted(user, permission)[0]


def _macro_fields(data, current=None):
    """Validated fields for a create or a (partial) update."""
    out = {}

    def given(name):
        return current is None or name in data

    if given('title'):
        out['title'] = _text(data, 'title', required=True, limit=120)
    if given('body'):
        out['body'] = _text(data, 'body', required=True, limit=MAX_BODY)
        try:
            macro_lib.validate_body(out['body'])
        except macro_lib.PlaceholderError as exc:
            raise BadInput('UNKNOWN_PLACEHOLDER', str(exc), 'body')
    if given('locale'):
        locale = str(data.get('locale') or 'en').strip()
        if locale not in dict(SupportMacro.LOCALE_CHOICES):
            raise BadInput('INVALID_LOCALE', 'locale must be en, hi or te.', 'locale')
        out['locale'] = locale
    if given('country'):
        country = data.get('country')
        country = None if country in (None, '', 'all') else str(country).strip().upper()
        if country is not None and country not in COUNTRIES:
            raise BadInput('INVALID_COUNTRY', 'country must be IN, US or empty (all).',
                           'country')
        out['country'] = country
    if given('issue_types'):
        issue_types = data.get('issue_types') or []
        if not isinstance(issue_types, list) or any(i not in ISSUES for i in issue_types):
            raise BadInput('INVALID_ISSUE_TYPE',
                           'issue_types must be a list of known issue types.', 'issue_types')
        out['issue_types'] = list(dict.fromkeys(issue_types))
    if 'active' in data:
        if not isinstance(data.get('active'), bool):
            raise BadInput('INVALID_FIELD', 'active must be true or false.', 'active')
        out['active'] = data['active']
    return out


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def admin_macros(request):
    """GET /support/admin/macros/  Active macros (support.reply), filtered by
    `ticket` (its country, issue type), `locale`, `country`, `issue_type`.
    `include_inactive=1` is for support.macro holders.

    POST (support.macro) creates one."""
    if request.method == 'POST':
        return _create_macro(request)
    params = request.query_params
    macros = SupportMacro.objects.select_related('created_by')
    if params.get('include_inactive') in ('1', 'true') and granted(
            request.user, 'support.macro')[0]:
        pass
    else:
        macros = macros.filter(active=True)
    ticket_id = params.get('ticket')
    if ticket_id:
        try:
            ticket, refusal = _ticket_for(request, _int(ticket_id, 'ticket'), 'support.reply')
        except BadInput as exc:
            return exc.response()
        if refusal:
            return refusal
        macros = macros.filter(Q(country__isnull=True) | Q(country=ticket.country))
        issue = ticket.issue_type
    else:
        issue = (params.get('issue_type') or '').strip()
        country = (params.get('country') or '').strip().upper()
        if country:
            macros = macros.filter(Q(country__isnull=True) | Q(country=country))
    locale = (params.get('locale') or '').strip()
    if locale:
        macros = macros.filter(locale=locale)
    rows = [m for m in macros if not issue or not m.issue_types or issue in m.issue_types]
    return success_response({
        'results': [_macro_dict(m) for m in rows],
        'placeholders': list(macro_lib.ALLOWED_PLACEHOLDERS),
        'may_manage': granted(request.user, 'support.macro')[0],
    }, status.HTTP_200_OK)


def _create_macro(request):
    if not granted(request.user, 'support.macro')[0]:
        return error_response(
            code='PERMISSION_DENIED', message='You do not hold support.macro.',
            field='general', issue='support.macro', status=status.HTTP_403_FORBIDDEN)
    try:
        fields = _macro_fields(request.data)
    except BadInput as exc:
        return exc.response()
    if not _macro_scope_ok(request.user, fields.get('country'), 'support.macro'):
        return error_response(
            code='PERMISSION_DENIED',
            message='You do not hold support.macro for that country.',
            field='country', issue='support.macro', status=status.HTTP_403_FORBIDDEN)
    with transaction.atomic():
        macro = SupportMacro.objects.create(created_by=request.user, **fields)
        from .admin_followups import write_macro_version

        write_macro_version(macro, request.user)
        record_admin_action(
            request, action='support_macro_created', target_type=MACRO_TARGET,
            target_id=macro.id, after=_macro_dict(macro), reason='macro created')
    return success_response(_macro_dict(macro), status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAuthenticated, ScopedPermission('support.macro')])
def admin_macro_detail(request, macro_id):
    """PATCH /support/admin/macros/<id>/ edits; DELETE deactivates (soft).
    Both are idempotent: a repeat writes nothing."""
    with transaction.atomic():
        macro = SupportMacro.objects.select_for_update().filter(id=macro_id).first()
        if macro is None:
            return error_response(code='NOT_FOUND', message='Macro not found.',
                                  field='macro_id', issue=f'Macro {macro_id}',
                                  status=status.HTTP_404_NOT_FOUND)
        if not _macro_scope_ok(request.user, macro.country, 'support.macro'):
            return error_response(
                code='PERMISSION_DENIED',
                message='You do not hold support.macro for that country.',
                field='macro_id', issue='support.macro', status=status.HTTP_403_FORBIDDEN)
        before = _macro_dict(macro)
        if request.method == 'DELETE':
            fields, action = {'active': False}, 'support_macro_deactivated'
        else:
            try:
                fields = _macro_fields(request.data, current=macro)
            except BadInput as exc:
                return exc.response()
            if 'country' in fields and not _macro_scope_ok(
                    request.user, fields['country'], 'support.macro'):
                return error_response(
                    code='PERMISSION_DENIED',
                    message='You do not hold support.macro for that country.',
                    field='country', issue='support.macro',
                    status=status.HTTP_403_FORBIDDEN)
            action = 'support_macro_updated'
        changed = {k: v for k, v in fields.items() if getattr(macro, k) != v}
        if not changed:
            return success_response({**_macro_dict(macro), 'changed': False},
                                    status.HTTP_200_OK)
        for key, value in changed.items():
            setattr(macro, key, value)
        macro.save()
        if any(k in changed for k in ('title', 'body', 'locale', 'issue_types')):
            from .admin_followups import write_macro_version

            write_macro_version(macro, request.user)
        record_admin_action(
            request, action=action, target_type=MACRO_TARGET, target_id=macro.id,
            before=before, after=_macro_dict(macro), reason=action)
    return success_response({**_macro_dict(macro), 'changed': True}, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def admin_macro_preview(request, macro_id):
    """GET /support/admin/macros/<id>/preview/?ticket=<id>  The macro rendered for one ticket."""
    try:
        ticket, refusal = _ticket_for(
            request, _int(request.query_params.get('ticket'), 'ticket'), 'support.reply')
    except BadInput as exc:
        return exc.response()
    if refusal:
        return refusal
    macro = SupportMacro.objects.filter(id=macro_id).first()
    if macro is None:
        return error_response(code='NOT_FOUND', message='Macro not found.',
                              field='macro_id', issue=f'Macro {macro_id}',
                              status=status.HTTP_404_NOT_FOUND)
    text, missing = macro_lib.render(macro.body, ticket)
    return success_response({'macro_id': macro.id, 'ticket_id': ticket.id, 'body': text,
                             'missing': missing}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# SLA policy
# ---------------------------------------------------------------------------

def _sla_row(country, priority, row):
    first, resolution = (DEFAULT_MINUTES[priority] if row is None
                         else (row.first_response_minutes, row.resolution_minutes))
    return {
        'country': country, 'priority': priority,
        'first_response_minutes': first, 'resolution_minutes': resolution,
        'source': 'default' if row is None else 'policy',
        'updated_at': _iso(row.updated_at) if row else None,
        'updated_by': _staff(row.updated_by) if row else None,
    }


def _country_node(country):
    return GeoNode.objects.filter(kind=COUNTRY, code=country).first()


def _pending_changes(user):
    """Open `support.sla_change` requests in countries the caller may read."""
    from servers.approvals.actions import SUPPORT_SLA_CHANGE
    from servers.approvals.models import REQUESTED, ApprovalRequest

    rows = []
    for r in ApprovalRequest.objects.filter(
            action_key=SUPPORT_SLA_CHANGE, status=REQUESTED).order_by('-id')[:100]:
        node = _country_node(r.country)
        if not granted(user, 'support.read', node)[0]:
            continue
        p = r.payload or {}
        rows.append({
            'id': r.id, 'country': r.country, 'kind': p.get('kind'),
            'priority': p.get('priority'),
            'first_response_minutes': p.get('first_response_minutes'),
            'resolution_minutes': p.get('resolution_minutes'),
            'waiting_reminder_days': p.get('waiting_reminder_days'),
            'waiting_autoclose_days': p.get('waiting_autoclose_days'),
            'refund_review_hours': p.get('refund_review_hours'),
            'reason': r.request_reason, 'maker_label': r.maker_label,
            'maker_id': r.maker_id, 'requested_at': _iso(r.requested_at),
            'expires_at': _iso(r.expires_at),
        })
    return rows


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('support.read')])
def admin_sla(request):
    """GET /support/admin/sla/  The policy for every country and priority, each
    country's support settings (or "not configured"), and the changes still
    waiting for a checker."""
    from .sla_change import config_dict

    rows = {(r.country, r.priority): r
            for r in SupportSlaPolicy.objects.select_related('updated_by')}
    configs = {c.country: c for c in SupportCountryConfig.objects.select_related('updated_by')}
    editable = [c for c in COUNTRIES
                if granted(request.user, 'support.sla', _country_node(c))[0]]
    return success_response({
        'results': [_sla_row(c, p, rows.get((c, p))) for c in COUNTRIES for p in PRIORITIES],
        'config': [{**config_dict(c, configs.get(c)),
                    'updated_at': _iso(configs[c].updated_at) if c in configs else None,
                    'updated_by': _staff(configs[c].updated_by) if c in configs else None}
                   for c in COUNTRIES],
        'pending': _pending_changes(request.user),
        'may_edit': bool(editable),
        'editable_countries': editable,
        'note': 'Waiting on the user pauses the resolution clock only. A change '
                'needs a second person to approve it before it applies.',
    }, status.HTTP_200_OK)


def _propose_sla_change(request, country, kind, target_id, payload, key_tail):
    """Raise the maker-checker request for an SLA / config change (202)."""
    from servers.approvals import service as approvals
    from servers.approvals.actions import SUPPORT_SLA_CHANGE
    from servers.approvals.models import REQUESTED

    node = _country_node(country)
    reason = _text(request.data, 'reason', limit=2000) or 'Support SLA change'
    try:
        outcome = approvals.require_approval_or_execute(
            SUPPORT_SLA_CHANGE, actor=request.user, country=country, scope_node=node,
            target_type=SLA_TARGET if kind == 'sla_policy' else 'support_country_config',
            target_id=target_id, payload={'kind': kind, 'country': country, **payload},
            idempotency_key=f'sla_change:{target_id}:{key_tail}', reason=reason,
            http_request=request)
    except approvals.ApprovalError as exc:
        return exc.response()
    row = outcome.request
    body = {'approval_request': row.as_dict(), 'duplicate': outcome.duplicate}
    if row.status == REQUESTED:
        body['state'] = 'pending_approval'
        return success_response(body, status.HTTP_202_ACCEPTED)
    if row.status == 'failed':
        return error_response(code=row.error_code or 'EXECUTION_FAILED',
                              message=row.error or 'The change could not be applied.',
                              field='general', issue=row.error or row.error_code,
                              status=status.HTTP_409_CONFLICT)
    body['state'] = 'executed' if row.status == 'executed' else row.status
    body['result'] = row.result
    return success_response(body, status.HTTP_200_OK)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, ScopedPermission('support.sla')])
def admin_sla_update(request, country, priority):
    """PUT /support/admin/sla/<country>/<priority>/
    {first_response_minutes, resolution_minutes, reason?}

    Does NOT change the policy: it raises a maker-checker request
    (`support.sla_change`, always a checker) and answers 202. The change
    applies, audited, when a different `support.sla` holder approves it, and
    then only to tickets created (or re-prioritised) from that moment.
    A City Ops Manager can ask only for a country inside their scope."""
    from .sla_change import KIND_POLICY, check_policy, policy_dict

    country = country.upper()
    if country not in COUNTRIES or priority not in PRIORITY_RANK:
        return error_response(code='NOT_FOUND', message='No such SLA row.', field='country',
                              issue=f'{country}/{priority}', status=status.HTTP_404_NOT_FOUND)
    node = _country_node(country)
    if not granted(request.user, 'support.sla', node)[0]:
        return error_response(
            code='PERMISSION_DENIED', message=f'You do not hold support.sla for {country}.',
            field='country', issue='support.sla', status=status.HTTP_403_FORBIDDEN)
    try:
        first = _int(request.data.get('first_response_minutes'), 'first_response_minutes')
        resolution = _int(request.data.get('resolution_minutes'), 'resolution_minutes')
        error = check_policy(first, resolution)
        if error:
            raise BadInput('INVALID_MINUTES', error, 'resolution_minutes')
    except BadInput as exc:
        return exc.response()
    row = SupportSlaPolicy.objects.filter(country=country, priority=priority).first()
    if row is not None and (row.first_response_minutes, row.resolution_minutes) == (
            first, resolution):
        return success_response({**policy_dict(country, priority, row), 'changed': False},
                                status.HTTP_200_OK)
    stamp = _iso(row.updated_at) if row else 'new'
    return _propose_sla_change(
        request, country, KIND_POLICY, f'{country}:{priority}',
        {'priority': priority, 'first_response_minutes': first, 'resolution_minutes': resolution},
        f'{first}:{resolution}:{stamp}')


@api_view(['PUT'])
@permission_classes([IsAuthenticated, ScopedPermission('support.sla')])
def admin_country_config_update(request, country):
    """PUT /support/admin/sla/config/<country>/
    {waiting_reminder_days, waiting_autoclose_days, refund_review_hours, reason?}

    The country's waiting-on-user and refund-review numbers. Same rule as an
    SLA value: a request for a checker (202), never applied on the spot."""
    from .sla_change import KIND_CONFIG, check_config, config_dict

    country = country.upper()
    if country not in COUNTRIES:
        return error_response(code='NOT_FOUND', message='No such country.', field='country',
                              issue=country, status=status.HTTP_404_NOT_FOUND)
    node = _country_node(country)
    if not granted(request.user, 'support.sla', node)[0]:
        return error_response(
            code='PERMISSION_DENIED', message=f'You do not hold support.sla for {country}.',
            field='country', issue='support.sla', status=status.HTTP_403_FORBIDDEN)
    try:
        reminder = _int(request.data.get('waiting_reminder_days'), 'waiting_reminder_days')
        autoclose = _int(request.data.get('waiting_autoclose_days'), 'waiting_autoclose_days')
        hours = _int(request.data.get('refund_review_hours'), 'refund_review_hours')
        error = check_config(reminder, autoclose, hours)
        if error:
            raise BadInput('INVALID_CONFIG', error, 'waiting_autoclose_days')
    except BadInput as exc:
        return exc.response()
    row = SupportCountryConfig.objects.filter(country=country).first()
    if row is not None and (row.waiting_reminder_days, row.waiting_autoclose_days,
                            row.refund_review_hours) == (reminder, autoclose, hours):
        return success_response({**config_dict(country, row), 'changed': False},
                                status.HTTP_200_OK)
    return _propose_sla_change(
        request, country, KIND_CONFIG, f'config:{country}',
        {'waiting_reminder_days': reminder, 'waiting_autoclose_days': autoclose,
         'refund_review_hours': hours},
        f'{reminder}:{autoclose}:{hours}:v{row.version if row else 0}')
