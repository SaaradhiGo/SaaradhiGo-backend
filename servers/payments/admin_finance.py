"""The Finance read side of the operator console: /payments/admin/...

EVERYTHING HERE IS A GET, EXCEPT ONE
------------------------------------
Payments, settlements, payouts, a driver's ledger, the per-city overview,
reconciliation, the payout configuration and the CSV exports are all reads. The
single write is resolving a reconciliation finding with a note (`finance.adjust`):
it moves no money, is idempotent and is audited. Money writes (payout release,
adjustments, wallet credit, configuration edits) are NOT here and never go around
the maker-checker framework in `servers.approvals`.

THE RULES EVERY ENDPOINT FOLLOWS
--------------------------------
  * Scope comes from `servers.payments.scope.money_visible_to`, in every list,
    detail, aggregate and export. Out of scope is a 404; in scope without the
    capability at that place is a 403.
  * Amounts are minor units plus a currency. A total is ALWAYS grouped by
    currency; nothing sums across currencies, not even on the overview.
  * A day is a day in the CITY's timezone (`finance_common`).
  * Personal data is masked by the operator renderer by field name; gateway ids
    and payout references are shown by their last four characters only; raw
    webhook payloads (which can carry card or bank data) are never returned.
"""

import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES, get_country
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_access.masking import mask_last4, mask_payload, mask_upi, policy_for
from servers.admin_access.scoping import ScopeNotHeld, requested_node
from servers.admin_audit.services import record_admin_action
from servers.driver.models import Driver, WithdrawalRequest
from servers.ride.models import TripMoneyAction

from . import ledger, payout_rails
from .finance_common import (
    BadParam, FinancePagination, city_day_q, csv_safe, day_bounds, group_by_timezone,
    mask_tail, money, node_timezones, parse_currency, parse_int, parse_range,
)
from .models import (
    Payment, PayoutCountryConfig, ReconciliationItem, ReconciliationRun, TransactionHistory,
    TripSettlement, WebhookEvent,
)
from .reconciliation import STATEMENT_NOT_CONNECTED, get_statement_source, resolve_item
from .unsettled import unsettled_rows
from .scope import (
    country_codes_visible, fetch_one, money_visible_to, node_for, paths_for,
)

READ = 'finance.read'

OPEN_PAYOUT_STATUSES = ('pending', 'approved', 'processed', 'unresolved')
PAYOUT_STATUS_LABELS = {
    'pending': 'Pending approval',
    'approved': 'Approved',
    'processed': 'Processing',
    'completed': 'Paid',
    'failed': 'Failed',
    'rejected': 'Rejected',
    'unresolved': 'Unresolved',
}
#: The order the console shows the tabs in.
PAYOUT_TAB_ORDER = ['pending', 'approved', 'processed', 'completed', 'failed', 'unresolved', 'rejected']


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _bad(exc):
    return error_response(
        code='VALIDATION_ERROR', message=exc.message, field=exc.field,
        issue=exc.message, status=status.HTTP_400_BAD_REQUEST)


def _scope_node(request):
    """`(node, refusal_response)` for the optional `?scope=` narrowing."""
    try:
        return requested_node(request), None
    except ScopeNotHeld as exc:
        return None, error_response(
            code='SCOPE_NOT_HELD', message=str(exc), field='scope',
            issue='Requested scope is outside your assignments',
            status=status.HTTP_403_FORBIDDEN)


def _not_found(what):
    return error_response(
        code='NOT_FOUND', message=f'{what} not found', field='id',
        issue=f'No {what.lower()} matches', status=status.HTTP_404_NOT_FOUND)


def _forbidden():
    return error_response(
        code='PERMISSION_DENIED', message='You do not have permission to do that here.',
        field='permission', issue='Capability not held at this place',
        status=status.HTTP_403_FORBIDDEN)


def _refusal(kind, what):
    return _not_found(what) if kind == 'not_found' else _forbidden()


def _include_unplaced(user, node, permission):
    """Rows with no city are visible to unrestricted callers only (and not when
    they narrowed to a place)."""
    return node is None and paths_for(user, permission) is None


def _page(request, qs, build, extra=None):
    paginator = FinancePagination()
    page = paginator.paginate_queryset(qs, request)
    data = paginator.get_paginated_response([build(row) for row in page]).data
    if extra:
        data.update(extra)
    return data


def _city(node):
    if node is None:
        return None
    return {'code': node.code, 'name': node.name}


def _totals_by_currency(qs, **aggregates):
    """`[{currency, count, ...}]` — one entry per currency, never a cross sum."""
    rows = qs.order_by().values('currency').annotate(count=Count('id'), **aggregates).order_by('currency')
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Row builders (shared by the JSON endpoints and the CSV exports)
# ---------------------------------------------------------------------------

def payment_row(p):
    trip = p.trip_id
    zone = getattr(trip, 'zone', None)
    return {
        'id': p.id,
        'trip_id': trip.id,
        'city': _city(getattr(zone, 'geo_node', None)),
        'rider_name': getattr(p.user_id, 'full_name', '') or '',
        'amount_minor': p.amount_minor,
        'amount': money(p.amount_minor, p.currency),
        'currency': p.currency,
        'method': p.method,
        'status': p.status,
        'gateway': p.payment_gateway,
        'gateway_order_id': mask_tail(p.gateway_order_id or p.cashfree_order_id),
        'gateway_payment_id': mask_tail(p.gateway_payment_id or p.cashfree_payment_id),
        'switch_reason': p.switch_reason,
        'replaced_by_id': p.replaced_by_id,
        'created_at': p.created_at,
        'updated_at': p.updated_at,
    }


def settlement_row(s):
    zone = getattr(s.trip, 'zone', None)
    return {
        'id': s.id,
        'trip_id': s.trip_id,
        'driver_id': s.driver_id,
        'driver_name': getattr(s.driver.user_id, 'full_name', '') or '',
        'city': _city(getattr(zone, 'geo_node', None)),
        'gross_fare_minor': s.gross_fare_minor,
        'gross_fare': money(s.gross_fare_minor, s.currency),
        'commission_percent': str(s.commission_percent),
        'commission_amount_minor': s.commission_amount_minor,
        'commission_amount': money(s.commission_amount_minor, s.currency),
        'driver_net_minor': s.driver_net_minor,
        'driver_net': money(s.driver_net_minor, s.currency),
        # US pass-through (AUDIT 27.62 item 1): null = nothing passed through (India, and rows
        # written before the rule, whose gross still read as it did).
        'tax_collected_minor': s.tax_collected_minor,
        'regulatory_fee_collected_minor': s.regulatory_fee_collected_minor,
        'currency': s.currency,
        'payment_method': s.payment_method,
        'source': s.source,
        'version': s.version,
        'settled_at': s.settled_at,
    }


def _payout_handle(w):
    """The payee's handle, masked to its rail: UPI keeps the bank suffix, a
    bank account keeps its last four."""
    driver = w.driver
    if w.payout_method == 'bank':
        try:
            return {'bank_account': mask_last4(driver.bank_account.account_number)}
        except Exception:  # noqa: BLE001 - no bank account on file
            return {'bank_account': None}
    return {'upi_id': mask_upi(getattr(driver, 'upi_id', '') or '')}


def _approval_summary(r):
    return {
        'id': r.id,
        'status': r.status,
        'requested_at': r.requested_at,
        'expires_at': r.expires_at,
        'decided_at': r.decided_at,
        'maker': {'id': r.maker_id, 'label': r.maker_label, 'role': r.maker_role},
        'checker': ({'id': r.checker_id, 'label': r.checker_label, 'role': r.checker_role}
                    if r.checker_id else None),
        'batch': r.target_type == 'withdrawal_batch',
    }


def _approvals_for(withdrawal_ids):
    """`{withdrawal_id: latest payout ApprovalRequest summary}`.

    One query for single-payout requests, and one bounded query for the
    payout-run (batch) requests still open or recently decided, so a payout in
    a weekly run shows the run's request."""
    from servers.approvals.actions import FINANCE_PAYOUT
    from servers.approvals.models import ApprovalRequest

    if not withdrawal_ids:
        return {}
    latest = {}
    wanted = set(int(i) for i in withdrawal_ids)
    batches = (ApprovalRequest.objects
               .filter(action_key=FINANCE_PAYOUT, target_type='withdrawal_batch')
               .order_by('-id')[:200])
    for r in reversed(list(batches)):
        for wid in (r.payload or {}).get('withdrawal_ids', []):
            if wid in wanted:
                latest[wid] = _approval_summary(r)
    rows = ApprovalRequest.objects.filter(
        action_key=FINANCE_PAYOUT, target_type='withdrawal_request',
        target_id__in=[str(i) for i in withdrawal_ids]).order_by('id')
    for r in rows:
        latest[int(r.target_id)] = {
            'id': r.id,
            'status': r.status,
            'requested_at': r.requested_at,
            'expires_at': r.expires_at,
            'decided_at': r.decided_at,
            'maker': {'id': r.maker_id, 'label': r.maker_label, 'role': r.maker_role},
            'checker': ({'id': r.checker_id, 'label': r.checker_label, 'role': r.checker_role}
                        if r.checker_id else None),
        }
    return latest


NO_POLICY_MESSAGE = 'Blocked: no approval policy in this country.'


def payout_blocked_countries(withdrawals):
    """Country codes (among these payouts) with no active FINANCE_PAYOUT policy.

    One query. Such a payout cannot be approved from the console: the generic
    maker-checker refuses (THRESHOLD_NOT_CONFIGURED) and nothing is invented."""
    from servers.approvals.actions import FINANCE_PAYOUT
    from servers.approvals.executors import payout_country
    from servers.approvals.models import ApprovalPolicy

    countries = {payout_country(w) for w in withdrawals}
    countries.discard(None)
    if not countries:
        return set()
    have = set(ApprovalPolicy.objects.filter(
        action_key=FINANCE_PAYOUT, is_active=True, country__in=countries,
    ).values_list('country', flat=True))
    return countries - have


def payout_row(w, approvals=None, now=None, blocked=None):
    from servers.approvals.executors import payout_country

    now = now or timezone.now()
    open_ = w.status in OPEN_PAYOUT_STATUSES
    row = {
        'id': w.id,
        'driver_id': w.driver_id,
        'driver_name': getattr(w.driver.user_id, 'full_name', '') or '',
        'city': _city(getattr(w.driver, 'home_city', None)),
        'amount_minor': w.amount_minor,
        'amount': money(w.amount_minor, w.currency),
        'currency': w.currency,
        'status': w.status,
        'status_label': PAYOUT_STATUS_LABELS.get(w.status, w.status),
        'payout_method': w.payout_method,
        'payout_status': w.payout_status,
        'payout_reference': mask_tail(w.payout_reference_id),
        'failure_count': w.failure_count,
        'failure_reason': w.failure_reason,
        'last_failure_at': w.last_failure_at,
        'requested_at': w.requested_at,
        'processed_at': w.processed_at,
        'age_seconds': int((now - w.requested_at).total_seconds()) if open_ else None,
        'fee_minor': w.fee_minor,
        'origin': w.origin,
        'under_investigation': w.investigation_since is not None,
        'investigation_note': w.investigation_note,
        'investigation_since': w.investigation_since,
        'retryable': w.status == 'failed' and not w.payout_reference_id,
        # Approved in a currency with no payout rail: held, nothing sent.
        'awaiting_rail': w.payout_status == 'awaiting_rail',
        'rail_note': payout_rails.rail_note(w.currency)
        if w.payout_status == 'awaiting_rail' else None,
    }
    if blocked is not None:
        country = payout_country(w)
        row['country'] = country
        row['approval_blocked'] = bool(w.status == 'pending' and country in blocked)
        row['approval_blocked_reason'] = (
            NO_POLICY_MESSAGE if row['approval_blocked'] else None)
    row.update(_payout_handle(w))
    if approvals is not None:
        row['approval'] = approvals.get(w.id)
    return row


# ---------------------------------------------------------------------------
# Querysets (shared by the JSON endpoints and the CSV exports)
# ---------------------------------------------------------------------------

def payments_queryset(request, permission=READ):
    node, refusal = _scope_node(request)
    if refusal:
        return None, None, refusal
    d_from, d_to = parse_range(request)
    qs = money_visible_to(
        request.user, Payment, node=node, permission=permission,
        queryset=Payment.objects.select_related('trip_id__zone__geo_node', 'user_id'))
    filters = {}
    for name, field in (('status', 'status'), ('method', 'method'), ('gateway', 'payment_gateway')):
        value = (request.query_params.get(name) or '').strip()
        if value:
            qs = qs.filter(**{field: value})
            filters[name] = value
    currency = parse_currency(request)
    if currency:
        qs = qs.filter(currency=currency)
        filters['currency'] = currency
    trip = parse_int(request, 'trip')
    if trip:
        qs = qs.filter(trip_id_id=trip)
        filters['trip'] = trip
    needle = (request.query_params.get('q') or '').strip()
    if needle:
        qs = qs.filter(Q(gateway_order_id=needle) | Q(cashfree_order_id=needle)
                       | Q(gateway_payment_id=needle) | Q(cashfree_payment_id=needle))
        filters['q'] = needle
    if d_from or d_to:
        qs = qs.filter(city_day_q(
            'trip_id__zone__geo_node_id', 'created_at', d_from, d_to,
            include_unplaced=_include_unplaced(request.user, node, permission)))
        filters['from'], filters['to'] = str(d_from or ''), str(d_to or '')
    return qs.order_by('-created_at', '-id'), filters, None


def settlements_queryset(request, permission=READ):
    node, refusal = _scope_node(request)
    if refusal:
        return None, None, refusal
    d_from, d_to = parse_range(request)
    qs = money_visible_to(
        request.user, TripSettlement, node=node, permission=permission,
        queryset=TripSettlement.objects.select_related(
            'trip__zone__geo_node', 'driver__user_id'))
    filters = {}
    method = (request.query_params.get('method') or '').strip()
    if method:
        qs = qs.filter(payment_method=method)
        filters['method'] = method
    source = (request.query_params.get('source') or '').strip()
    if source:
        qs = qs.filter(source=source)
        filters['source'] = source
    currency = parse_currency(request)
    if currency:
        qs = qs.filter(currency=currency)
        filters['currency'] = currency
    driver = parse_int(request, 'driver')
    if driver:
        qs = qs.filter(driver_id=driver)
        filters['driver'] = driver
    if d_from or d_to:
        qs = qs.filter(city_day_q(
            'trip__zone__geo_node_id', 'settled_at', d_from, d_to,
            include_unplaced=_include_unplaced(request.user, node, permission)))
        filters['from'], filters['to'] = str(d_from or ''), str(d_to or '')
    return qs.order_by('-settled_at', '-id'), filters, None


def payouts_queryset(request, permission=READ, *, apply_status=True):
    node, refusal = _scope_node(request)
    if refusal:
        return None, None, refusal, None
    d_from, d_to = parse_range(request)
    qs = money_visible_to(
        request.user, WithdrawalRequest, node=node, permission=permission,
        queryset=WithdrawalRequest.objects.select_related(
            'driver__user_id', 'driver__home_city'))
    filters = {}
    currency = parse_currency(request)
    if currency:
        qs = qs.filter(currency=currency)
        filters['currency'] = currency
    method = (request.query_params.get('method') or '').strip()
    if method:
        qs = qs.filter(payout_method=method)
        filters['method'] = method
    driver = parse_int(request, 'driver')
    if driver:
        qs = qs.filter(driver_id=driver)
        filters['driver'] = driver
    if d_from or d_to:
        qs = qs.filter(city_day_q(
            'driver__home_city_id', 'requested_at', d_from, d_to,
            include_unplaced=_include_unplaced(request.user, node, permission)))
        filters['from'], filters['to'] = str(d_from or ''), str(d_to or '')
    counted = qs
    state = (request.query_params.get('status') or '').strip()
    if state and apply_status:
        qs = qs.filter(status=state)
        filters['status'] = state
    return qs.order_by('-requested_at', '-id'), filters, None, counted


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_payments(request):
    """GET /payments/admin/payments/ — rider charges, newest first."""
    try:
        qs, _filters, refusal = payments_queryset(request)
    except BadParam as exc:
        return _bad(exc)
    if refusal:
        return refusal
    totals = _totals_by_currency(qs, amount_minor=Sum('amount_minor'))
    return success_response(
        _page(request, qs, payment_row, {'totals': totals}), status.HTTP_200_OK)


def _chain(payment):
    """The switch/retry chain this payment belongs to, oldest first."""
    chain = [payment]
    seen = {payment.id}
    cursor = payment
    for _ in range(10):
        previous = Payment.objects.filter(replaced_by=cursor).first()
        if previous is None or previous.id in seen:
            break
        chain.insert(0, previous)
        seen.add(previous.id)
        cursor = previous
    cursor = payment
    for _ in range(10):
        nxt = cursor.replaced_by
        if nxt is None or nxt.id in seen:
            break
        chain.append(nxt)
        seen.add(nxt.id)
        cursor = nxt
    return [{'id': p.id, 'status': p.status, 'method': p.method,
             'amount_minor': p.amount_minor, 'currency': p.currency,
             'switch_reason': p.switch_reason, 'created_at': p.created_at,
             'current': p.id == payment.id} for p in chain]


def _webhook_summaries(payment):
    """The webhook deliveries for this payment's order: type, when, outcome.
    Never the payload, which can hold bank or card data."""
    oids = [o for o in (payment.gateway_order_id, payment.cashfree_order_id) if o]
    if not oids:
        return []
    q = Q()
    for oid in oids:
        q |= Q(raw_payload__data__order__order_id=oid) | Q(raw_payload__orderId=oid)
    events = (WebhookEvent.objects
              .filter(q, gateway=payment.payment_gateway,
                      received_at__gte=payment.created_at - timedelta(days=1))
              .only('id', 'event_type', 'received_at', 'processed_at', 'result')
              .order_by('-received_at')[:20])
    outcomes = {'ok': 'processed', 'error': 'error', '': 'not processed'}
    return [{'id': e.id, 'event_type': e.event_type, 'received_at': e.received_at,
             'processed_at': e.processed_at, 'result': outcomes.get(e.result, e.result)}
            for e in events]


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_payment_detail(request, payment_id):
    """GET /payments/admin/payments/<id>/"""
    payment, refusal = fetch_one(
        request.user, Payment, READ, pk=payment_id,
        queryset=Payment.objects.select_related('trip_id__zone__geo_node', 'user_id', 'replaced_by'))
    if refusal:
        return _refusal(refusal, 'Payment')
    refunds = [{
        'id': a.id, 'amount_minor': a.amount_minor, 'currency': a.currency,
        'method': a.method, 'status': a.status, 'created_at': a.created_at,
        'approval_request_id': a.approval_request_id,
    } for a in TripMoneyAction.objects.filter(
        trip_id=payment.trip_id_id, kind=TripMoneyAction.REFUND).order_by('created_at', 'id')]
    data = payment_row(payment)
    data.update({
        'chain': _chain(payment),
        'refunds': refunds,
        'webhooks': _webhook_summaries(payment),
    })
    return success_response(data, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Settlements
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_settlements(request):
    """GET /payments/admin/settlements/ — what each settled trip paid out."""
    try:
        qs, _filters, refusal = settlements_queryset(request)
    except BadParam as exc:
        return _bad(exc)
    if refusal:
        return refusal
    totals = _totals_by_currency(
        qs, gross_minor=Sum('gross_fare_minor'), commission_minor=Sum('commission_amount_minor'),
        driver_net_minor=Sum('driver_net_minor'),
        tax_collected_minor=Sum('tax_collected_minor'),
        regulatory_fee_collected_minor=Sum('regulatory_fee_collected_minor'))
    return success_response(
        _page(request, qs, settlement_row, {'totals': totals}), status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_payouts(request):
    """GET /payments/admin/payouts/ — withdrawals of every status, plus the
    queue counts per tab (counts and amounts, per currency)."""
    try:
        qs, _filters, refusal, counted = payouts_queryset(request)
    except BadParam as exc:
        return _bad(exc)
    if refusal:
        return refusal
    now = timezone.now()
    tabs = {s: {'status': s, 'label': PAYOUT_STATUS_LABELS[s], 'count': 0, 'amounts': []}
            for s in PAYOUT_TAB_ORDER}
    for row in (counted.order_by().values('status', 'currency')
                .annotate(count=Count('id'), amount_minor=Sum('amount_minor'))):
        tab = tabs.get(row['status'])
        if tab is None:
            continue
        tab['count'] += row['count']
        tab['amounts'].append({'currency': row['currency'], 'amount_minor': row['amount_minor']})

    paginator = FinancePagination()
    page = paginator.paginate_queryset(qs, request)
    approvals = _approvals_for([w.id for w in page])
    blocked = payout_blocked_countries(page)
    data = paginator.get_paginated_response(
        [payout_row(w, approvals, now, blocked) for w in page]).data
    data['tabs'] = [tabs[s] for s in PAYOUT_TAB_ORDER]
    # Currencies in this list with no payout rail: the console says approved payouts wait.
    data['rail_notices'] = [
        {'currency': c, 'note': payout_rails.rail_note(c)}
        for c in sorted({w.currency for w in page}) if not payout_rails.has_rail(c)]
    return success_response(data, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_payout_detail(request, payout_id):
    """GET /payments/admin/payouts/<id>/"""
    from servers.admin_audit.models import AdminAuditLog

    withdrawal, refusal = fetch_one(
        request.user, WithdrawalRequest, READ, pk=payout_id,
        queryset=WithdrawalRequest.objects.select_related('driver__user_id', 'driver__home_city'))
    if refusal:
        return _refusal(refusal, 'Payout')
    approvals = _approvals_for([withdrawal.id])
    data = payout_row(withdrawal, approvals, blocked=payout_blocked_countries([withdrawal]))
    data['admin_notes'] = withdrawal.admin_notes
    # Masked by the renderer; the console offers the logged reveal beside it.
    data['driver_phone'] = getattr(withdrawal.driver.user_id, 'phone_number', '') or ''
    data['payout_mode'] = withdrawal.payout_mode
    data['transactions'] = [{
        'id': t.id, 'status': t.status, 'txn_type': t.txn_type, 'created_at': t.created_at,
        'amount_minor': t.amount_minor, 'currency': t.currency, 'gateway': t.payment_gateway,
        'gateway_transaction_id': mask_tail(t.gateway_transaction_id or t.gateway_payment_id),
    } for t in TransactionHistory.objects.filter(withdrawal_request=withdrawal).order_by('created_at', 'id')]
    data['history'] = [{
        'id': a.id, 'action': a.action, 'at': a.created_at, 'by': a.actor_label,
        'reason': a.reason,
    } for a in AdminAuditLog.objects.filter(
        target_type='withdrawal_request', target_id=str(withdrawal.id)).order_by('created_at', 'id')[:50]]
    return success_response(data, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# A driver's ledger
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_driver_ledger(request, driver_id):
    """GET /payments/admin/drivers/<id>/ledger/?from=&to=&page=&page_size=

    The statement: opening balance, settlements, withdrawals (requested, paid,
    rejected or returned), closing balance. Dates are the driver's home city's
    own days; with no dates, the last 30 days.
    """
    driver, refusal = fetch_one(
        request.user, Driver, READ, pk=driver_id,
        queryset=Driver.objects.select_related('user_id', 'home_city'))
    if refusal:
        return _refusal(refusal, 'Driver')
    tzmap = node_timezones()
    tz = tzmap.get(driver.home_city_id)
    if tz is None:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(settings.TIME_ZONE)
    try:
        d_from, d_to = parse_range(request, default_days=30, tz=tz)
        page = max(1, parse_int(request, 'page') or 1)
        size = min(200, max(1, parse_int(request, 'page_size') or 50))
    except BadParam as exc:
        return _bad(exc)
    if d_from is None or d_to is None:
        d_to = d_to or d_from
        d_from = d_from or d_to
    start, end = day_bounds(d_from, d_to, tz)

    base = {
        'driver': {
            'id': driver.id,
            'driver_name': getattr(driver.user_id, 'full_name', '') or '',
            'home_city': _city(driver.home_city),
        },
        'range': {'from': d_from, 'to': d_to, 'timezone': getattr(tz, 'key', str(tz))},
    }
    statement = ledger.build_statement(driver, start, end)
    if statement is None:
        base.update({'has_wallet': False, 'currency': None, 'entries': {
            'count': 0, 'page': page, 'page_size': size, 'results': []}, 'memo': []})
        return success_response(base, status.HTTP_200_OK)

    rows = statement.pop('rows')
    count = rows.count()
    entries = ledger.entry_dicts(driver, rows, statement['opening_minor'], (page - 1) * size, size)
    memo = ledger.memo_lines(driver, statement['currency'], start, end)[:200]
    base.update(statement)
    from servers.fraud.money import ledger_extras

    base.update({
        'has_wallet': True,
        'entries': {'count': count, 'page': page, 'page_size': size, 'results': entries},
        'memo': memo,
        **ledger_extras(driver),
    })
    return success_response(base, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# The per-city, per-currency, per-day overview
# ---------------------------------------------------------------------------

def overview_rows(user, node, d_from, d_to, permission=READ):
    """`{(city_id, day, currency): {column: minor}}` computed per city timezone."""
    families = [
        (TripSettlement, 'trip__zone__geo_node_id', 'settled_at', Q(),
         {'gross_minor': Sum('gross_fare_minor'), 'commission_minor': Sum('commission_amount_minor'),
          'driver_net_minor': Sum('driver_net_minor'),
          # US pass-through (AUDIT 27.62 item 1), not part of gross, commission or net.
          'tax_collected_minor': Sum('tax_collected_minor'),
          'regulatory_fee_collected_minor': Sum('regulatory_fee_collected_minor')}),
        (TripMoneyAction, 'trip__zone__geo_node_id', 'created_at', Q(kind=TripMoneyAction.REFUND),
         {'refunds_minor': Sum('amount_minor')}),
        (TransactionHistory, 'driver_id__home_city_id', 'created_at',
         Q(withdrawal_request__isnull=False, status='completed'),
         {'payouts_paid_minor': Sum('amount_minor')}),
        (WithdrawalRequest, 'driver__home_city_id', 'requested_at',
         Q(status__in=OPEN_PAYOUT_STATUSES), {'payouts_pending_minor': Sum('amount_minor')}),
        (WithdrawalRequest, 'driver__home_city_id', 'requested_at', Q(status='failed'),
         {'payouts_failed_minor': Sum('amount_minor')}),
    ]
    from zoneinfo import ZoneInfo

    out = defaultdict(lambda: defaultdict(int))
    groups = group_by_timezone(node_timezones())
    unplaced = _include_unplaced(user, node, permission)
    for model, anchor, time_field, extra, sums in families:
        base = money_visible_to(user, model, node=node, permission=permission).filter(extra)
        buckets = [(tz, Q(**{f'{anchor}__in': ids})) for tz, ids in groups.values()]
        if unplaced:
            buckets.append((ZoneInfo(settings.TIME_ZONE), Q(**{f'{anchor}__isnull': True})))
        for tz, where in buckets:
            start, end = day_bounds(d_from, d_to, tz)
            qs = (base.filter(where, **{f'{time_field}__gte': start, f'{time_field}__lt': end})
                  .annotate(_day=TruncDate(time_field, tzinfo=tz))
                  .values(anchor, '_day', 'currency').order_by().annotate(**sums))
            for row in qs:
                key = (row[anchor], row['_day'], row['currency'])
                for column in sums:
                    out[key][column] += row[column] or 0
    return out


def pass_through_section(out, currency=None):
    """"Tax and fees collected (pass-through)": per currency, what the platform collected from
    riders for remittance, by kind. `out` is `overview_rows`'s result, so it covers exactly the cities,
    days and scope of the overview it sits in. A currency with nothing collected is left out."""
    sums = defaultdict(lambda: {'tax': 0, 'fee': 0})
    for (_city_id, _day, cur), values in out.items():
        if currency and cur != currency:
            continue
        sums[cur]['tax'] += values.get('tax_collected_minor', 0)
        sums[cur]['fee'] += values.get('regulatory_fee_collected_minor', 0)
    return {
        'title': 'Tax and fees collected (pass-through)',
        'note': 'Collected from riders and remitted by SaaradhiGo. Not driver earnings and not '
                'platform revenue; commission is worked out on the fare before tax and fees.',
        'by_currency': [
            {'currency': cur, 'collected_minor': v['tax'] + v['fee'],
             'by_kind': [{'kind': 'tax_collected', 'label': 'Sales tax', 'amount_minor': v['tax']},
                         {'kind': 'regulatory_fee_collected', 'label': 'Regulatory fees',
                          'amount_minor': v['fee']}]}
            for cur, v in sorted(sums.items()) if v['tax'] or v['fee']],
    }


def platform_funded_ends(user, node, limit=100):
    """Trips an operator ended under the platform-funded rule (safety incident, platform or
    app fault) THIS WEEK (Monday on, in the server's time zone): the rider paid nothing and
    the platform paid the driver. Scoped by `money_visible_to`; trip id, city, reason code
    and amount only, nothing about the rider or the driver.

    Returns `{week_start, count, totals: [{currency, count, driver_earnings_minor}], rows}`.
    Totals are per currency, never a cross sum."""
    from zoneinfo import ZoneInfo


    from servers.ride.models import Trip

    from .unsettled import city_node_of
    from .weekly_payout import week_start_of

    tz = ZoneInfo(settings.TIME_ZONE)
    monday = week_start_of(timezone.now(), tz)
    start = datetime.combine(monday, datetime.min.time(), tzinfo=tz)
    qs = money_visible_to(
        user, Trip, node, permission=READ,
        queryset=Trip.objects.filter(end_rule='platform_funded', completed_at__gte=start))
    totals = [{'currency': r['currency'], 'count': r['count'],
               'driver_earnings_minor': r['earnings'] or 0}
              for r in (qs.order_by().values('currency')
                        .annotate(count=Count('id'), earnings=Sum('platform_funded_minor'))
                        .order_by('currency'))]
    rows = []
    for trip in (qs.select_related('zone', 'zone__geo_node')
                 .order_by('-completed_at', '-id')[:limit]):
        city = city_node_of(trip)
        rows.append({
            'trip_id': trip.id, 'city': _city(city), 'completed_at': trip.completed_at,
            'reason_code': trip.end_reason_code,
            'driver_earnings_minor': trip.platform_funded_minor or 0,
            'currency': trip.currency or '',
        })
    # The per-trip regulatory fee the platform owes on these trips (the rider was charged 0): a
    # platform expense on no wallet. US accountant to confirm (go-live blocker #15).
    from .models import PlatformExpense

    from collections import defaultdict as _dd

    trip_ids = list(qs.order_by().values_list('id', flat=True))
    fee_rows = list(PlatformExpense.objects.filter(
        kind=PlatformExpense.REGULATORY_FEE, trip_id__in=trip_ids).order_by('-created_at', '-id'))
    fee_totals = _dd(lambda: {'count': 0, 'amount_minor': 0})
    for e in fee_rows:
        fee_totals[e.currency]['count'] += 1
        fee_totals[e.currency]['amount_minor'] += e.amount_minor
    regulatory = {
        'kind': PlatformExpense.REGULATORY_FEE,
        'totals': [{'currency': c, **v} for c, v in sorted(fee_totals.items())],
        'rows': [{'trip_id': e.trip_id, 'amount_minor': e.amount_minor, 'currency': e.currency}
                 for e in fee_rows[:limit]],
        'note': 'Booked as a platform expense; none of it is charged to the rider or taken '
                'from a wallet. US accountant to confirm (go-live blocker 15).',
    }
    return {'week_start': monday, 'count': sum(t['count'] for t in totals),
            'totals': totals, 'rows': rows, 'regulatory_fee_expenses': regulatory}


def skipped_in_latest_runs(user, node):
    """Per country: how many drivers the LATEST weekly run skipped (no payout handle),
    counting only drivers inside the viewer's scope (money_visible_to on the driver's
    home city). A count only: the ids never leave this function."""
    from .models import PayoutRun

    out = []
    seen = set()
    for run in PayoutRun.objects.order_by('-week_start', '-id'):
        if run.country in seen:
            continue
        seen.add(run.country)
        ids = [s['driver_id'] for s in (run.skipped or [])]
        if not ids:
            continue
        visible = money_visible_to(user, Driver, node, queryset=Driver.objects.filter(id__in=ids))
        count = visible.count()
        if count:
            visible_ids = set(visible.values_list('id', flat=True))
            held = sum(1 for s in (run.skipped or [])
                       if s.get('reason') == 'held' and s['driver_id'] in visible_ids)
            row = {'country': run.country, 'week_start': run.week_start, 'count': count}
            if held:
                row.update(held_count=held, no_handle_count=count - held)
            out.append(row)
    return sorted(out, key=lambda r: r['country'])


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_overview(request):
    """GET /payments/admin/overview/?from=&to= — per city, currency and day.

    Columns: gross, commission and driver net (settlements by settled day),
    refunds (by refund day), payouts paid (by paid day), payouts pending (open
    requests, by request day: the liability) and failed payouts (by request
    day). Totals are per currency only.
    """
    from servers.admin_access.models import GeoNode

    node, refusal = _scope_node(request)
    if refusal:
        return refusal
    try:
        d_from, d_to = parse_range(request, default_days=14)
        currency = parse_currency(request)
    except BadParam as exc:
        return _bad(exc)
    if d_from is None or d_to is None:
        return _bad(BadParam('from', 'from and to are both required'))
    out = overview_rows(request.user, node, d_from, d_to)
    skipped = skipped_in_latest_runs(request.user, node)
    unsettled, unsettled_count = unsettled_rows(request.user, node)
    platform_funded = platform_funded_ends(request.user, node)
    names = {n.id: n for n in GeoNode.objects.filter(id__in={k[0] for k in out if k[0]})}
    columns = ['gross_minor', 'commission_minor', 'driver_net_minor', 'refunds_minor',
               'payouts_paid_minor', 'payouts_pending_minor', 'payouts_failed_minor']
    rows, totals = [], defaultdict(lambda: defaultdict(int))
    for (city_id, day, cur), values in out.items():
        if currency and cur != currency:
            continue
        if not any(values.get(c) for c in columns):
            continue
        row = {'city': _city(names.get(city_id)), 'day': day, 'currency': cur}
        for c in columns:
            row[c] = values.get(c, 0)
        rows.append(row)
        for c in columns:
            totals[cur][c] += row[c]
    rows.sort(key=lambda r: (r['day'], r['city']['name'] if r['city'] else '', r['currency']),
              reverse=False)
    rows.sort(key=lambda r: r['day'], reverse=True)
    return success_response({
        'range': {'from': d_from, 'to': d_to},
        'columns': columns,
        'rows': rows,
        'totals': [{'currency': cur, **dict(vals)} for cur, vals in sorted(totals.items())],
        # US pass-through (AUDIT 27.62 item 1): tax and fees collected from riders, owed to the
        # authorities, never earnings or revenue. Per currency, by kind. US accountant to confirm.
        'tax_and_fees_collected': pass_through_section(out, currency),
        # A COUNT per country, in the viewer's scope: never a name or a phone number.
        'skipped_in_latest_run': skipped,
        # Completed trips left unsettled because no commission rule governs them
        # (module 9 owner item). Scoped by money_visible_to; no rider or driver detail.
        'unsettled_trips': unsettled,
        'unsettled_trips_count': unsettled_count,
        # Started trips an operator ended as a safety incident or platform fault this week:
        # the rider paid 0 and the platform paid the driver. Scoped; no rider or driver detail.
        'platform_funded_trip_ends': platform_funded,
        'note': 'Totals are grouped by currency. Nothing is summed across currencies.',
    }, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.adjust')])
def finance_unsettled_retry(request, trip_id):
    """POST /payments/admin/unsettled/<trip_id>/retry/ {note?}

    "Retry now" for a trip left unsettled because no commission rule governed it. Needs
    `finance.adjust` at the trip's city (the finance write capability: there is no
    separate `finance.write`). It is NOT a money decision, so it needs no approval: it
    applies the rule that governs the trip now (or its booking-time snapshot) through the
    same idempotent settlement path, and it is audited either way. A trip outside the
    caller's money scope is a 404.
    """
    from servers.ride.models import Trip

    from .unsettled import REASON, open_alerts, retry_trip

    trip, refusal = fetch_one(request.user, Trip, 'finance.adjust', pk=trip_id)
    if refusal:
        return _refusal(refusal, 'Trip')
    if not open_alerts().filter(source_type='trip_settlement', source_id=str(trip.id)).exists():
        return error_response(
            code='NOT_UNSETTLED', message='That trip is not waiting on a commission rule.',
            field='trip_id', issue='No open unsettled-trip alert',
            status=status.HTTP_409_CONFLICT)
    note = str((request.data or {}).get('note', '') if hasattr(request, 'data') else '')[:2000]
    settled = retry_trip(trip)
    record_admin_action(
        request, action='unsettled_trip_retried', target_type='trip', target_id=trip.id,
        before={'settled': False, 'reason': REASON},
        after={'settled': settled}, reason=note or 'Retry now')
    return success_response({'trip_id': trip.id, 'settled': settled,
                             'still_unsettled_reason': None if settled else REASON},
                            status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _kind_labels():
    return [{'key': key, 'label': label} for key, label in ReconciliationItem.KIND_CHOICES]


def item_row(item):
    return {
        'id': item.id,
        'run_id': item.run_id,
        'kind': item.kind,
        'kind_label': item.get_kind_display(),
        'country': item.country,
        'currency': item.currency,
        'city': _city(item.anchor_node),
        'payment_id': item.payment_id,
        'withdrawal_id': item.withdrawal_id,
        'trip_id': item.trip_id,
        'reference': mask_tail(item.reference),
        'expected': item.expected,
        'seen': item.seen,
        'created_at': item.created_at,
        'resolved_at': item.resolved_at,
        'resolved_by_label': item.resolved_by_label,
        'resolution_note': item.resolution_note,
    }


def _items_queryset(request, permission=READ):
    node, refusal = _scope_node(request)
    if refusal:
        return None, refusal
    qs = money_visible_to(request.user, ReconciliationItem, node=node, permission=permission,
                          queryset=ReconciliationItem.objects.select_related('anchor_node'))
    kind = (request.query_params.get('kind') or '').strip()
    if kind:
        qs = qs.filter(kind=kind)
    country = (request.query_params.get('country') or '').strip().upper()
    if country:
        qs = qs.filter(country=country)
    run = parse_int(request, 'run')
    if run:
        qs = qs.filter(run_id=run)
    state = (request.query_params.get('state') or 'open').strip()
    if state == 'open':
        qs = qs.filter(resolved_at__isnull=True)
    elif state == 'resolved':
        qs = qs.filter(resolved_at__isnull=False)
    return qs, None


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_reconciliation(request):
    """GET /payments/admin/reconciliation/ — recent runs, open counts per kind,
    and what is and is not connected or configured."""
    node, refusal = _scope_node(request)
    if refusal:
        return refusal
    runs = money_visible_to(request.user, ReconciliationRun, node=node, permission=READ)
    country = (request.query_params.get('country') or '').strip().upper()
    if country:
        runs = runs.filter(country=country)
    items = money_visible_to(request.user, ReconciliationItem, node=node, permission=READ)
    open_items = items.filter(resolved_at__isnull=True)
    open_by_kind = {r['kind']: r['n'] for r in open_items.order_by().values('kind').annotate(n=Count('id'))}
    open_by_run = {r['run_id']: r['n'] for r in open_items.order_by().values('run_id').annotate(n=Count('id'))}

    def run_row(run):
        return {
            'id': run.id, 'date': run.date, 'country': run.country, 'currency': run.currency,
            'gateway': run.gateway, 'status': run.status, 'new_findings': run.counts,
            'open_items': open_by_run.get(run.id, 0), 'checks': run.checks,
            'started_at': run.started_at, 'finished_at': run.finished_at, 'error': run.error,
        }

    source = get_statement_source()
    configs = {c.country: c for c in PayoutCountryConfig.objects.filter(is_active=True)}
    stuck = []
    for code in country_codes_visible(request.user, node, READ):
        cfg = configs.get(code)
        hours = cfg.stuck_payout_hours if cfg else None
        stuck.append({'country': code, 'configured': bool(hours), 'stuck_payout_hours': hours})
    paginator = FinancePagination()
    page = paginator.paginate_queryset(runs.order_by('-date', 'country', 'currency'), request)
    data = paginator.get_paginated_response([run_row(r) for r in page]).data
    data.update({
        'statement_import': {'connected': source.connected, 'source': source.name,
                             'message': None if source.connected else STATEMENT_NOT_CONNECTED},
        'kinds': _kind_labels(),
        'open_by_kind': open_by_kind,
        'open_total': sum(open_by_kind.values()),
        'stuck_payout_check': stuck,
    })
    return success_response(data, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_reconciliation_items(request):
    """GET /payments/admin/reconciliation/items/?state=open|resolved|all&kind=&country=&run="""
    try:
        qs, refusal = _items_queryset(request)
    except BadParam as exc:
        return _bad(exc)
    if refusal:
        return refusal
    return success_response(_page(request, qs.order_by('-created_at', '-id'), item_row),
                            status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.adjust')])
def finance_reconciliation_resolve(request, item_id):
    """POST /payments/admin/reconciliation/items/<id>/resolve/ {note}

    The one write of the Finance read side. Needs `finance.adjust` at the
    item's place. Idempotent: resolving a resolved item changes nothing and
    says so. Moves no money.
    """
    item, refusal = fetch_one(request.user, ReconciliationItem, 'finance.adjust', pk=item_id)
    if refusal:
        return _refusal(refusal, 'Reconciliation item')
    note = str((request.data or {}).get('note', '') if hasattr(request, 'data') else '').strip()
    if len(note) < 3:
        return error_response(
            code='NOTE_REQUIRED', message='A note is required to resolve a finding.',
            field='note', issue='Say what you checked or did', status=status.HTTP_400_BAD_REQUEST)
    if len(note) > 2000:
        return error_response(
            code='NOTE_TOO_LONG', message='The note is limited to 2000 characters.',
            field='note', issue='Too long', status=status.HTTP_400_BAD_REQUEST)
    item, changed = resolve_item(item.id, user=request.user, note=note, request=request)
    data = item_row(item)
    data['already_resolved'] = not changed
    return success_response(data, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Payout configuration (read here; edits are finance.payout_config_change requests)
# ---------------------------------------------------------------------------

HISTORY_LIMIT = 20


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def finance_payout_config(request):
    """GET /payments/admin/payout-config/ — each visible country's payout rules,
    their version history, the pending change requests, the recent scheduled
    runs, and the plain statement of whether the country can be edited or is
    blocked for want of an approval policy."""
    from servers.admin_access.bridge import granted
    from servers.approvals.actions import FINANCE_PAYOUT, FINANCE_PAYOUT_CONFIG
    from servers.approvals.models import REQUESTED, ApprovalPolicy, ApprovalRequest

    from .models import PayoutRun

    node, refusal = _scope_node(request)
    if refusal:
        return refusal
    active = {c.country: c for c in PayoutCountryConfig.objects.filter(is_active=True)}
    editable_in = set(country_codes_visible(request.user, node, 'finance.config'))
    policies = {(p.country, p.action_key) for p in ApprovalPolicy.objects.filter(
        is_active=True, action_key__in=[FINANCE_PAYOUT, FINANCE_PAYOUT_CONFIG])}
    rows = []
    for code in country_codes_visible(request.user, node, READ):
        country = COUNTRIES[code]
        cfg = active.get(code)
        history = [c.as_dict() for c in
                   PayoutCountryConfig.objects.filter(country=code).order_by('-version')[:HISTORY_LIMIT]]
        pending = [r.as_dict() for r in ApprovalRequest.objects.filter(
            action_key=FINANCE_PAYOUT_CONFIG, target_id=code, status=REQUESTED).order_by('-id')[:5]]
        runs = [{'id': r.id, 'week_start': r.week_start, 'withdrawal_count': r.withdrawal_count,
                 'amount_minor': r.amount_minor, 'currency': r.currency, 'outcome': r.outcome,
                 'approval_request_id': r.approval_request_id, 'truncated': r.truncated,
                 'skipped': r.skipped, 'skipped_count': len(r.skipped or []),
                 'created_at': r.created_at}
                for r in PayoutRun.objects.filter(country=code).order_by('-week_start')[:8]]
        can_edit = code in editable_in and cfg is not None
        rows.append({
            'country': code, 'name': country.name, 'currency': country.currency,
            'configured': cfg is not None,
            'config': cfg.as_dict() if cfg else None,
            'message': None if cfg else (
                'Not configured. Drivers here cannot withdraw until the owner sets the '
                'fee and the minimum.'),
            'history': history,
            'pending_changes': pending,
            'recent_runs': runs,
            'editable': can_edit,
            'change_blocked': (can_edit and (code, FINANCE_PAYOUT_CONFIG) not in policies),
            'change_blocked_reason': (
                'Blocked: no approval policy in this country.'
                if can_edit and (code, FINANCE_PAYOUT_CONFIG) not in policies else None),
            'payout_approval_blocked': (code, FINANCE_PAYOUT) not in policies,
            'payout_approval_blocked_reason': (
                'Blocked: no approval policy in this country. Payouts here cannot be '
                'approved until one is configured.'
                if (code, FINANCE_PAYOUT) not in policies else None),
        })
    return success_response({
        'countries': rows,
        'editable': granted(request.user, 'finance.config')[0],
        'fee_warning': ('Any fee above zero is shown to drivers exactly as it is charged: '
                        'the same value feeds the driver app and the withdrawal.'),
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------

EXPORT_KINDS = ('settlements', 'payouts', 'payments')
DEFAULT_EXPORT_MAX_ROWS = 50_000


def export_max_rows():
    return int(getattr(settings, 'FINANCE_EXPORT_MAX_ROWS', DEFAULT_EXPORT_MAX_ROWS))


def _flat(row):
    """Flatten nested cells for CSV: a city is its code, an approval its status."""
    flat = {}
    for key, value in row.items():
        if key == 'city':
            flat['city'] = value['code'] if value else ''
        elif key == 'approval':
            flat['approval_status'] = value['status'] if value else ''
        elif isinstance(value, dict):
            continue
        else:
            flat[key] = '' if value is None else value
    return flat


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read'),
                     ScopedPermission('finance.export')])
def finance_export(request, kind):
    """GET /payments/admin/export/<settlements|payouts|payments>/ — CSV.

    The same filters, scope and masking as the JSON endpoint of the same name;
    never unmasked (there is no reveal for an export of money rows). Every
    export is audited with its filters and row count; above the row cap it is
    refused with 400 rather than truncated.
    """
    if kind not in EXPORT_KINDS:
        return error_response(
            code='UNKNOWN_EXPORT', message='Unknown export.', field='kind',
            issue=f'kind must be one of {", ".join(EXPORT_KINDS)}', status=status.HTTP_404_NOT_FOUND)
    permission = 'finance.export'
    try:
        if kind == 'settlements':
            qs, filters, refusal = settlements_queryset(request, permission)
            build = settlement_row
        elif kind == 'payments':
            qs, filters, refusal = payments_queryset(request, permission)
            build = payment_row
        else:
            qs, filters, refusal, _counted = payouts_queryset(request, permission)
            build = None
    except BadParam as exc:
        return _bad(exc)
    if refusal:
        return refusal

    cap = export_max_rows()
    count = qs.count()
    if count > cap:
        return error_response(
            code='EXPORT_TOO_LARGE',
            message=f'This export has {count} rows; the limit is {cap}. Narrow the date range or scope.',
            field='filters', issue=f'{count} rows exceeds {cap}', status=status.HTTP_400_BAD_REQUEST)

    now = timezone.now()
    approvals = None
    policy = policy_for(request.user)
    buffer = io.StringIO()
    writer = None
    written = 0
    ordered = qs
    for start in range(0, count, 2000):
        chunk = list(ordered[start:start + 2000])
        if kind == 'payouts':
            approvals = _approvals_for([w.id for w in chunk])
        for obj in chunk:
            row = payout_row(obj, approvals, now) if kind == 'payouts' else build(obj)
            row = _flat(mask_payload(row, policy))
            if writer is None:
                writer = csv.DictWriter(buffer, fieldnames=list(row), extrasaction='ignore')
                writer.writeheader()
            writer.writerow({k: csv_safe(v) for k, v in row.items()})
            written += 1
    if writer is None:
        buffer.write('id\n')

    audit = record_admin_action(
        request, action='finance_export', target_type='finance_export', target_id=kind,
        after={'kind': kind, 'filters': filters, 'row_count': written, 'masked': True,
               'scope': request.query_params.get('scope') or ''},
        reason='')
    if audit is None:
        # No log, no export: a bulk hand-over of money rows must leave a trace.
        return error_response(
            code='AUDIT_UNAVAILABLE', message='The export could not be logged, so it was not produced.',
            field='general', issue='audit write failed', status=status.HTTP_503_SERVICE_UNAVAILABLE)

    response = HttpResponse(buffer.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = (
        f'attachment; filename="finance-{kind}-{now:%Y%m%d-%H%M}.csv"')
    response['Cache-Control'] = 'no-store'
    return response
