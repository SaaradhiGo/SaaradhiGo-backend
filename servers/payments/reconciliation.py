"""Daily reconciliation, from LOCAL data only.

WHAT IT COMPARES
----------------
Our own tables against each other: payments against the webhook events we
stored, trips against their settlements, settlements against the trip's final
fare, payouts against the payout webhooks and against how long they have sat in
processing. It calls no gateway, imports no statement and spends no money. A
gateway's own statement would be the real third party; that arrives through
`GatewayStatementSource`, and the only implementation shipped is `NullSource`,
so the console says plainly that statement import is not connected.

It WRITES only `ReconciliationRun` and `ReconciliationItem` rows. It never
changes a payment, a trip, a settlement, a withdrawal or a balance.

SEVEN KINDS
-----------
  payment_no_webhook         a completed online payment with no confirming webhook
  webhook_no_payment         a confirming webhook with no completed payment
  amount_mismatch            the webhook's amount or currency is not the payment's
  trip_no_settlement         a completed trip with a driver and no settlement
  settlement_gross_mismatch  the current settlement's gross is not the trip's final fare
  payout_webhook_not_paid    a payout success webhook while the withdrawal is not paid
  payout_stuck               processing for longer than the country's stuck_payout_hours

The last one needs `PayoutCountryConfig.stuck_payout_hours`. A country with no
value has NO such check: it is skipped (recorded in `run.checks` as
`not_configured`) and the console says so. There is no default number of hours.

BOUNDED
-------
One run is one (date, country, currency, gateway) and every query is limited to
that day's window (plus a few days for a webhook to arrive), uses the indexes
on `created_at` / `completed_at` / `settled_at` / `received_at`, and raises at
most `MAX_ITEMS_PER_CHECK` items per check (a truncated check is flagged).

IDEMPOTENT
----------
A finding has a `dedupe_key` per subject and kind. Running the same day twice
raises nothing new, and a RESOLVED finding is not reopened by a later run.
"""

import logging
from collections import Counter
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from base.countries import COUNTRIES, get_country
from base.money import to_minor

from .finance_common import day_bounds
from .models import (
    Payment, PaymentGateway, PayoutCountryConfig, ReconciliationItem, ReconciliationRun,
    TripSettlement, WebhookEvent,
)

logger = logging.getLogger(__name__)

#: A payment or trip younger than this may still have its webhook / settlement
#: on the way; it is not a finding yet.
GRACE = timedelta(minutes=60)
#: A webhook may arrive after the day it confirms; look this far past the window.
WEBHOOK_LOOKAHEAD = timedelta(days=3)
#: ... and (for a payment made late the previous evening) this far before it.
WEBHOOK_LOOKBEHIND = timedelta(days=1)
#: How far back the stuck-payout check looks. Older than this is not "stuck",
#: it is history and belongs to a person.
STUCK_LOOKBACK = timedelta(days=30)
MAX_ITEMS_PER_CHECK = 2000

# Cashfree event names, then Razorpay / RazorpayX (their deliveries are stored raw in the inbox).
PAYMENT_SUCCESS_EVENTS = ('PAYMENT_SUCCESS_WEBHOOK', 'PAYMENT_SUCCESS', 'payment.captured', 'order.paid')
PAYOUT_SUCCESS_EVENTS = ('TRANSFER_SUCCESS', 'PAYOUT_SUCCESS', 'payout.processed')
#: Inbox gateway names that belong to one payment-gateway name (RazorpayX payouts are 'razorpayx').
INBOX_GATEWAYS = {'razorpay': ('razorpay', 'razorpayx')}

K = ReconciliationItem


# ---------------------------------------------------------------------------
# The gateway statement interface (not connected)
# ---------------------------------------------------------------------------

class GatewayStatementSource:
    """Where a gateway's own settlement statement would come from.

    `fetch` returns the statement lines for one (date, country, currency,
    gateway), or None if the source cannot supply them. Nothing in this module
    requires a source to be connected: the local checks stand on their own.
    """

    name = 'none'
    connected = False

    def fetch(self, *, date, country, currency, gateway):  # pragma: no cover - interface
        raise NotImplementedError


class NullSource(GatewayStatementSource):
    """The only source shipped. No gateway call, no import, no cost."""

    name = 'null'
    connected = False

    def fetch(self, *, date, country, currency, gateway):
        return None


def get_statement_source():
    return NullSource()


STATEMENT_NOT_CONNECTED = 'Gateway statement import is not connected. Reconciliation compares local records only.'


# ---------------------------------------------------------------------------
# Reading a stored webhook
# ---------------------------------------------------------------------------

def _dig(payload, *path):
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def parse_payment_webhook(payload):
    """`{order_id, amount_minor, currency}` from either Cashfree payload shape."""
    if not isinstance(payload, dict):
        return {'order_id': None, 'amount_minor': None, 'currency': None}
    rz = _dig(payload, 'payload', 'payment', 'entity')
    if isinstance(rz, dict) and rz.get('order_id'):
        # Razorpay: amounts are already minor units (paise).
        amount = rz.get('amount')
        return {'order_id': str(rz['order_id']),
                'amount_minor': amount if isinstance(amount, int) else None,
                'currency': str(rz.get('currency') or 'INR').upper()}
    order_id = _dig(payload, 'data', 'order', 'order_id') or payload.get('orderId')
    amount = (_dig(payload, 'data', 'payment', 'payment_amount')
              or _dig(payload, 'data', 'order', 'order_amount') or payload.get('orderAmount'))
    currency = (_dig(payload, 'data', 'payment', 'payment_currency')
                or _dig(payload, 'data', 'order', 'order_currency') or payload.get('currency'))
    currency = str(currency).upper() if currency else None
    amount_minor = None
    if amount is not None and currency:
        try:
            amount_minor = to_minor(Decimal(str(amount)), currency)
        except (InvalidOperation, ValueError, TypeError):
            amount_minor = None
    return {'order_id': str(order_id) if order_id else None,
            'amount_minor': amount_minor, 'currency': currency}


def parse_payout_webhook(payload):
    """The identifiers a payout webhook can be matched on."""
    if not isinstance(payload, dict):
        return []
    entity = _dig(payload, 'payload', 'payout', 'entity')
    if isinstance(entity, dict):
        return [str(v) for v in (entity.get('reference_id'), entity.get('id')) if v]
    found = [payload.get('transfer_id'), payload.get('payoutId'),
             payload.get('cf_transfer_id'), payload.get('referenceId')]
    return [str(v) for v in found if v]


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def country_timezone(country):
    return ZoneInfo(get_country(country).default_timezone)


def _node_country_path(country):
    from servers.admin_access.models import COUNTRY, GeoNode

    node = GeoNode.objects.filter(kind=COUNTRY, code=country).first()
    return node.path if node else None


class _Run:
    def __init__(self, run, now):
        self.run = run
        self.now = now
        self.counts = Counter()
        self.checks = {}

    def raise_item(self, kind, key, *, payment=None, withdrawal=None, trip=None,
                   reference='', expected='', seen='', anchor=None):
        """Create the finding unless one already exists for this subject."""
        dedupe = f'{kind}:{key}'[:120]
        if ReconciliationItem.objects.filter(dedupe_key=dedupe).exists():
            return None
        try:
            with transaction.atomic():
                item = ReconciliationItem.objects.create(
                    run=self.run, kind=kind, dedupe_key=dedupe,
                    country=self.run.country, currency=self.run.currency,
                    payment=payment, withdrawal=withdrawal, trip=trip,
                    reference=str(reference or '')[:256],
                    expected=str(expected)[:255], seen=str(seen)[:255],
                    anchor_node=anchor,
                )
        except IntegrityError:
            return None
        self.counts[kind] += 1
        return item


def _trip_city(trip):
    zone = getattr(trip, 'zone', None)
    return getattr(zone, 'geo_node', None)


def _success_webhooks(gateway, since, until):
    """Success webhook events in a received_at window, newest last."""
    return (WebhookEvent.objects
            .filter(gateway=gateway, received_at__gte=since, received_at__lt=until,
                    event_type__in=PAYMENT_SUCCESS_EVENTS)
            .only('id', 'event_type', 'raw_payload', 'received_at')
            .order_by('received_at')[:MAX_ITEMS_PER_CHECK * 5])


def _check_payments_and_webhooks(r, country, currency, gateway, start, end):
    now = r.now
    # -- the day's completed online payments --------------------------------
    payments = list(
        Payment.objects.filter(
            created_at__gte=start, created_at__lt=min(end, now - GRACE),
            payment_gateway=gateway, method='online', status='completed',
            currency=currency, trip_id__zone__country=country,
        ).select_related('trip_id__zone__geo_node')[:MAX_ITEMS_PER_CHECK]
    )
    events = list(_success_webhooks(
        gateway, start - WEBHOOK_LOOKBEHIND, min(end + WEBHOOK_LOOKAHEAD, now)))
    by_order = {}
    for event in events:
        parsed = parse_payment_webhook(event.raw_payload)
        if parsed['order_id']:
            by_order.setdefault(parsed['order_id'], parsed)

    for payment in payments:
        oids = [o for o in (payment.gateway_order_id, payment.cashfree_order_id) if o]
        hit = next((by_order[o] for o in oids if o in by_order), None)
        anchor = _trip_city(payment.trip_id)
        if hit is None:
            r.raise_item(K.PAYMENT_NO_WEBHOOK, f'p{payment.id}', payment=payment,
                         trip=payment.trip_id, reference=oids[0] if oids else '',
                         expected='confirming webhook', seen='none', anchor=anchor)
            continue
        if hit['currency'] and hit['amount_minor'] is not None and (
                hit['currency'] != payment.currency or hit['amount_minor'] != payment.amount_minor):
            r.raise_item(K.AMOUNT_MISMATCH, f'p{payment.id}', payment=payment,
                         trip=payment.trip_id, reference=hit['order_id'] or '',
                         expected=f'{payment.amount_minor} {payment.currency}',
                         seen=f"{hit['amount_minor']} {hit['currency']}", anchor=anchor)
    r.checks['payment_no_webhook'] = 'ran'
    r.checks['amount_mismatch'] = 'ran'

    # -- webhooks received in the window with no completed payment ----------
    in_window = [e for e in events if start <= e.received_at < end]
    parsed_in_window = [(e, parse_payment_webhook(e.raw_payload)) for e in in_window]
    order_ids = sorted({p['order_id'] for _e, p in parsed_in_window if p['order_id']})
    known = {}
    topups = set()
    if order_ids:
        from servers.rider.models import WalletTransaction

        for chunk_start in range(0, len(order_ids), 500):
            chunk = order_ids[chunk_start:chunk_start + 500]
            for p in (Payment.objects.filter(Q(gateway_order_id__in=chunk) | Q(cashfree_order_id__in=chunk))
                      .select_related('trip_id__zone__geo_node')):
                for o in (p.gateway_order_id, p.cashfree_order_id):
                    if o:
                        known[o] = p
            topups.update(WalletTransaction.objects.filter(gateway_order_id__in=chunk)
                          .values_list('gateway_order_id', flat=True))
    unattributable = 0
    for event, parsed in parsed_in_window:
        oid = parsed['order_id']
        if not oid or oid in topups:
            continue
        payment = known.get(oid)
        if payment is None:
            if not parsed['currency']:
                unattributable += 1
                continue
            if parsed['currency'] != currency:
                continue  # another country's run will see it
            r.raise_item(K.WEBHOOK_NO_PAYMENT, f'w{event.id}', reference=oid,
                         expected='a completed payment', seen='no payment for this order',
                         anchor=None)
        elif payment.status != 'completed':
            zone = getattr(payment.trip_id, 'zone', None)
            if getattr(zone, 'country', None) != country:
                continue
            r.raise_item(K.WEBHOOK_NO_PAYMENT, f'w{event.id}', payment=payment,
                         trip=payment.trip_id, reference=oid, expected='completed',
                         seen=f'payment is {payment.status}', anchor=_trip_city(payment.trip_id))
    if unattributable:
        r.checks['webhooks_without_currency'] = unattributable
    r.checks['webhook_no_payment'] = 'ran'


def _check_settlements(r, country, currency, start, end):
    from servers.ride.models import Trip

    now = r.now
    settled = TripSettlement.objects.filter(trip=OuterRef('pk'))
    missing = (Trip.objects
               .filter(completed_at__gte=start, completed_at__lt=min(end, now - GRACE),
                       status_id__status_code='completed', driver_id__isnull=False,
                       currency=currency, zone__country=country)
               .exclude(Exists(settled))
               # A platform-funded operator end has no settlement by design: the rider paid
               # nothing and no commission was recorded (its driver credit is the ledger line).
               .exclude(end_rule='platform_funded')
               .select_related('zone__geo_node')[:MAX_ITEMS_PER_CHECK])
    for trip in missing:
        r.raise_item(K.TRIP_NO_SETTLEMENT, f't{trip.id}', trip=trip,
                     expected='a settlement', seen='none', anchor=_trip_city(trip))
    r.checks['trip_no_settlement'] = 'ran'

    newer = TripSettlement.objects.filter(trip=OuterRef('trip'), version__gt=OuterRef('version'))
    off = (TripSettlement.objects
           .filter(settled_at__gte=start, settled_at__lt=end, currency=currency,
                   trip__zone__country=country, trip__final_fare_minor__isnull=False)
           # US pass-through (AUDIT 27.62 item 1): the settlement's gross is the fare BEFORE tax and
           # fees, so the trip's full charge is gross + tax + fee (tax/fee are NULL on India and on
           # settlements written before the rule, where gross alone is the charge).
           .annotate(_charge=F('gross_fare_minor') + Coalesce(F('tax_collected_minor'), 0)
                     + Coalesce(F('regulatory_fee_collected_minor'), 0))
           .exclude(_charge=F('trip__final_fare_minor'))
           .exclude(Exists(newer))
           .select_related('trip__zone__geo_node')[:MAX_ITEMS_PER_CHECK])
    for s in off:
        r.raise_item(K.SETTLEMENT_GROSS_MISMATCH, f's{s.id}', trip=s.trip,
                     expected=f'{s.trip.final_fare_minor} {currency}',
                     seen=f'{s.gross_fare_minor} {currency}', anchor=_trip_city(s.trip))
    r.checks['settlement_gross_mismatch'] = 'ran'


def _withdrawal_country_q(country):
    """Withdrawals whose payout rules come from `country` (home city, else currency)."""
    path = _node_country_path(country)
    q = Q(pk__in=[])
    if path:
        q |= Q(driver__home_city__path__startswith=path)
    q |= Q(driver__home_city__isnull=True, currency=get_country(country).currency)
    return q


def _check_payouts(r, country, currency, gateway, start, end):
    from servers.driver.models import WithdrawalRequest

    now = r.now
    # -- a payout success webhook while the withdrawal is not paid ----------
    events = (WebhookEvent.objects
              .filter(gateway__in=INBOX_GATEWAYS.get(gateway, (gateway,)),
                      received_at__gte=start, received_at__lt=min(end, now),
                      event_type__in=PAYOUT_SUCCESS_EVENTS)
              .only('id', 'raw_payload', 'received_at')[:MAX_ITEMS_PER_CHECK])
    for event in events:
        ids = parse_payout_webhook(event.raw_payload)
        if not ids:
            continue
        withdrawal = WithdrawalRequest.objects.filter(payout_reference_id__in=ids).select_related(
            'driver__home_city').first()
        if withdrawal is None:
            for ref in ids:
                if ref.startswith('withdrawal_') and len(ref.split('_')) > 1 and ref.split('_')[1].isdigit():
                    withdrawal = WithdrawalRequest.objects.filter(
                        id=int(ref.split('_')[1])).select_related('driver__home_city').first()
                    break
        if (withdrawal is None or withdrawal.status in ('completed', 'reversed')
                or withdrawal.currency != currency):
            continue
        if not WithdrawalRequest.objects.filter(_withdrawal_country_q(country), pk=withdrawal.pk).exists():
            continue
        r.raise_item(K.PAYOUT_WEBHOOK_NOT_PAID, f'w{withdrawal.id}', withdrawal=withdrawal,
                     reference=ids[0], expected='completed',
                     seen=f'withdrawal is {withdrawal.status}',
                     anchor=withdrawal.driver.home_city)
    r.checks['payout_webhook_not_paid'] = 'ran'

    # -- stuck in processing -------------------------------------------------
    config = PayoutCountryConfig.objects.filter(country=country, is_active=True).first()
    hours = config.stuck_payout_hours if config else None
    if not hours:
        r.checks['payout_stuck'] = 'not_configured'
        return
    cutoff = now - timedelta(hours=hours)
    stuck = (WithdrawalRequest.objects
             .filter(_withdrawal_country_q(country), status='processed', currency=currency,
                     processed_at__lt=cutoff, processed_at__gte=now - STUCK_LOOKBACK)
             .select_related('driver__home_city')[:MAX_ITEMS_PER_CHECK])
    for w in stuck:
        r.raise_item(K.PAYOUT_STUCK, f'w{w.id}', withdrawal=w, reference=w.payout_reference_id or '',
                     expected=f'paid within {hours} h', seen=f'processing since {w.processed_at:%Y-%m-%d %H:%M} UTC',
                     anchor=w.driver.home_city)
    r.checks['payout_stuck'] = 'ran'


def run_one(country, gateway, date, *, now=None, force=False):
    """Reconcile one (country, gateway, local date). Returns the run row.

    A finished run is left alone unless `force`; a failed one is retried.
    """
    now = now or timezone.now()
    currency = get_country(country).currency
    tz = country_timezone(country)
    start, end = day_bounds(date, date, tz)
    run, created = ReconciliationRun.objects.get_or_create(
        date=date, country=country, currency=currency, gateway=gateway,
        defaults={'window_start': start, 'window_end': end},
    )
    if not created and not force and run.finished_at and run.status != ReconciliationRun.ERROR:
        return run
    r = _Run(run, now)
    source = get_statement_source()
    r.checks['statement_source'] = 'connected' if source.connected else 'not_connected'
    try:
        _check_payments_and_webhooks(r, country, currency, gateway, start, end)
        _check_settlements(r, country, currency, start, end)
        _check_payouts(r, country, currency, gateway, start, end)
        run.status = ReconciliationRun.ISSUES if sum(r.counts.values()) else ReconciliationRun.OK
        run.error = ''
    except Exception as exc:  # noqa: BLE001 - one country's failure must not stop the others
        logger.exception('reconciliation failed for %s %s %s', country, gateway, date)
        run.status = ReconciliationRun.ERROR
        run.error = f'{type(exc).__name__}: {exc}'[:500]
    run.counts = dict(r.counts)
    run.checks = r.checks
    run.finished_at = timezone.now()
    run.save(update_fields=['status', 'error', 'counts', 'checks', 'finished_at'])
    return run


def run_daily(*, now=None, days=3, force=False):
    """Every supported country and gateway for the last `days` finished local days.

    Three days so a night the worker was down is made up on the next one;
    finished runs are skipped, so the normal cost is one day.
    """
    now = now or timezone.now()
    runs = []
    for code in sorted(COUNTRIES):
        today = now.astimezone(country_timezone(code)).date()
        for back in range(days, 0, -1):
            day = today - timedelta(days=back)
            for gateway in PaymentGateway.values:
                runs.append(run_one(code, gateway, day, now=now, force=force))
    return runs


def resolve_item(item_id, *, user, note, request=None):
    """Mark one finding resolved. Idempotent: resolving twice changes nothing.

    Returns `(item, changed)`. Writes the item and one audit row; touches no
    money.
    """
    from servers.admin_audit.services import record_admin_action

    with transaction.atomic():
        item = ReconciliationItem.objects.select_for_update().get(pk=item_id)
        if item.resolved_at is not None:
            return item, False
        item.resolved_at = timezone.now()
        item.resolved_by = user
        item.resolved_by_label = (getattr(user, 'full_name', '') or getattr(user, 'email', '') or '')[:255]
        item.resolution_note = note
        item.save(update_fields=['resolved_at', 'resolved_by', 'resolved_by_label', 'resolution_note'])
        record_admin_action(
            request, action='reconciliation_item_resolved', target_type='reconciliation_item',
            target_id=item.id,
            before={'resolved': False},
            after={'resolved': True, 'kind': item.kind, 'country': item.country,
                   'currency': item.currency},
            reason=note,
        )
    return item, True
