"""Completed trips whose online payment failed (Srini's ruling, 15 Nov 2026).

* The driver is paid. A completed trip earns the driver their pay even if the rider's payment
  fails; the platform carries the loss. The trip is settled through the normal settlement
  (`settle_or_defer`, the same ledger rows, statement line and payout as any online trip). Tips
  are separate charges and are credited when they succeed, as before.
* The card is retried. A US card trip is charged again off-session on the saved card, at most
  `MAX_RETRIES` times, `RETRY_EVERY_HOURS` apart (hourly beat, `payments.collect_unpaid_trips`).
  India's online payments (UPI, cards, netbanking) cannot be charged off-session: the rider pays
  again from the trip, through the existing retry.
* Until it is paid the rider cannot book: the booking and the card hold are refused with
  `UNPAID_LAST_TRIP` and "Pay for your last trip".
* Repeated failures between the same rider and driver raise a fraud flag for review
  (`fraud.rules.unpaid_pair`). The flag is internal; it blocks nothing.

The same rule holds in every market, for online payments only: cash is never in scope.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

UNPAID_CODE = 'UNPAID_LAST_TRIP'
UNPAID_MESSAGE = 'Pay for your last trip'
MAX_RETRIES = 5
RETRY_EVERY_HOURS = 6
#: Failed payments between one rider and one driver, in `PAIR_WINDOW_DAYS`, that raise a flag.
PAIR_MIN_FAILURES = 2
PAIR_WINDOW_DAYS = 90
#: Never paid: the trip's payment is still one of these.
OPEN_STATUSES = ('failed', 'pending', 'processing')


def _failed_payments():
    from .models import Payment

    # A row marked failed because the rider switched method is not a failure.
    return Payment.objects.filter(status='failed').exclude(method='cash').filter(
        Q(switch_reason__isnull=True) | Q(switch_reason=''))


def unpaid_trips():
    """Completed online trips that were never paid and whose payment has failed at least once."""
    from servers.ride.models import Trip

    failed = _failed_payments().filter(trip_id=OuterRef('pk'))
    return (Trip.objects.filter(status_id__status_code='completed', payment_status__in=OPEN_STATUSES)
            .exclude(payment_method__in=('cash', 'deferred'))
            .exclude(end_rule='platform_funded')
            .filter(Q(payment_status='failed') | Q(Exists(failed))))


def unpaid_trip_for(user):
    """The rider's most recent unpaid trip, or None."""
    if user is None or not getattr(user, 'pk', None):
        return None
    return unpaid_trips().filter(user_id=user).order_by('-completed_at', '-id').first()


def refusal_for(user):
    """The booking refusal for a rider with an unpaid trip, or None."""
    trip = unpaid_trip_for(user)
    if trip is None:
        return None
    return {'code': UNPAID_CODE, 'message': UNPAID_MESSAGE, **summary(trip)}


def summary(trip):
    from base.money import to_major_string

    from .card_hold import _payable_minor

    return {'trip_id': trip.id, 'amount': to_major_string(_payable_minor(trip), trip.currency),
            'currency': trip.currency, 'can_retry_card': _hold_with_card(trip) is not None}


def settle_driver(trip):
    """Pay the driver for a completed trip whose payment failed. Idempotent: once settled, no more."""
    from servers.driver.utils import settle_or_defer
    from servers.rider.models import Wallet, get_wallet

    from .models import TripSettlement

    if not trip.driver_id:
        return False
    with transaction.atomic():
        get_wallet(trip.driver_id.user_id, Wallet.SCOPE_DRIVER, lock=True)
        if TripSettlement.objects.filter(trip=trip).exists():
            return False
        settled = settle_or_defer(trip)
    if settled:
        logger.info('unpaid trip settled to the driver trip=%s', trip.id)
    return settled


def _hold_with_card(trip):
    from .models import CardHold

    hold = CardHold.objects.filter(trip=trip).first()
    if hold is None or not hold.customer_id:
        return None
    return hold


def _retries(payment):
    meta = (payment.gateway_metadata or {}) if payment is not None else {}
    return int(meta.get('card_retries') or 0), meta.get('card_retried_at')


def _due(payment, now):
    count, at = _retries(payment)
    if count >= MAX_RETRIES:
        return False
    if not at:
        return True
    try:
        from datetime import datetime
        return now - datetime.fromisoformat(at) >= timedelta(hours=RETRY_EVERY_HOURS)
    except (TypeError, ValueError):
        return True


def retry_card(trip, *, now=None, by_rider=False):
    """Charge the trip's saved card again. Returns 'paid', 'declined', 'not_due' or 'no_card'.

    A rider pressing "Pay now" retries at once (still at most `MAX_RETRIES` in all)."""
    from servers.ride.models import Trip

    from .card_hold import _gateway, _payable_minor, refresh
    from .models import Payment, TransactionHistory

    now = now or timezone.now()
    hold = _hold_with_card(trip)
    if hold is not None and not hold.payment_method_id:
        refresh(hold)
    gateway = _gateway()
    if hold is None or not hold.payment_method_id or gateway is None:
        return 'no_card'
    last = Payment.objects.filter(trip_id=trip).exclude(method='cash').order_by('-id').first()
    count, at = _retries(last)
    if count >= MAX_RETRIES or (not by_rider and not _due(last, now)):
        return 'not_due'
    amount = _payable_minor(trip)
    if amount <= 0:
        return 'no_card'
    if last is not None:
        last.gateway_metadata = {**(last.gateway_metadata or {}), 'card_retries': count + 1,
                                 'card_retried_at': now.isoformat()}
        last.save(update_fields=['gateway_metadata'])
    data = gateway.charge_saved(hold.customer_id, hold.payment_method_id, amount, currency=trip.currency,
                                reference=f'unpaid_trip_{trip.id}_{count + 1}',
                                metadata={'trip_id': trip.id, 'kind': 'unpaid_trip'})
    if not data or data.get('status') != 'succeeded':
        logger.warning('unpaid trip card retry declined trip=%s attempt=%s', trip.id, count + 1)
        return 'declined'
    with transaction.atomic():
        trip = Trip.objects.select_for_update(of=('self',)).select_related('driver_id', 'user_id').get(pk=trip.pk)
        if trip.payment_status == 'completed':
            return 'paid'
        payment = Payment.objects.create(
            trip_id=trip, user_id=trip.user_id, amount_minor=amount, method='online', status='completed',
            payment_gateway=gateway.get_name(), gateway_order_id=str(data.get('id') or ''),
            gateway_payment_id=str(data.get('latest_charge') or data.get('id') or ''),
            gateway_metadata={'kind': 'unpaid_trip_retry', 'attempt': count + 1})
        trip.payment_status = 'completed'
        trip.save(update_fields=['payment_status'])
        if trip.driver_id:
            TransactionHistory.objects.create(
                trip_id=trip, user_id=trip.user_id, driver_id=trip.driver_id, amount=payment.amount,
                method='online', payment_gateway=hold.provider,
                user_name=trip.user_id.full_name or trip.user_id.phone_number or '', status='completed')
            from servers.driver.utils import settle_paid_by_platform
            # Already settled when the payment failed: this writes nothing more.
            settle_paid_by_platform(trip, payment.amount_minor)
    logger.info('unpaid trip paid on card retry trip=%s attempt=%s', trip.id, count + 1)
    return 'paid'


def flag_pair(trip):
    """Raise (or extend) a fraud flag when this rider's payments failed repeatedly with this driver."""
    if not trip.driver_id or not trip.user_id_id:
        return None
    since = timezone.now() - timedelta(days=PAIR_WINDOW_DAYS)
    trip_ids = sorted(set(_failed_payments().filter(
        user_id=trip.user_id_id, trip_id__driver_id=trip.driver_id, created_at__gte=since,
    ).values_list('trip_id_id', flat=True)[:100]))
    if len(trip_ids) < PAIR_MIN_FAILURES:
        return None
    from servers.fraud.models import HIGH, MEDIUM
    from servers.fraud.raising import raise_hit
    from servers.fraud.rules import unpaid_pair
    from servers.fraud.rules.base import Hit, clamp_score

    n = len(trip_ids)
    hit = Hit(subject=trip.user_id, severity=HIGH if n >= 3 else MEDIUM, score=clamp_score(40 + 15 * (n - 1)),
              signals=['failed_payments_same_driver'],
              refs={'trip_ids': trip_ids, 'counterpart_user_ids': [trip.driver_id.user_id_id]},
              measures={'failed_payments_same_driver': n})
    flag, outcome = raise_hit(unpaid_pair.RULE, None, hit)
    return outcome


def on_payment_failed(trip):
    """Called once a trip's payment has failed: pay the driver, then look for a repeated pattern."""
    try:
        settle_driver(trip)
    except Exception:  # noqa: BLE001 - the hourly sweep settles it later
        logger.exception('settling unpaid trip %s failed', trip.id)
    try:
        flag_pair(trip)
    except Exception:  # noqa: BLE001 - a flag never stops the payment path
        logger.exception('unpaid pair flag failed trip=%s', trip.id)


def sweep(now=None, limit=200):
    """Hourly: settle every unpaid trip's driver, flag repeated pairs, retry US cards when due."""
    now = now or timezone.now()
    out = {'settled': 0, 'paid': 0, 'declined': 0}
    for trip in unpaid_trips().select_related('driver_id', 'user_id').order_by('id')[:limit]:
        try:
            if settle_driver(trip):
                out['settled'] += 1
            flag_pair(trip)
            result = retry_card(trip, now=now)
            if result in ('paid', 'declined'):
                out[result] += 1
        except Exception:  # noqa: BLE001 - one bad trip never stops the rest
            logger.exception('unpaid trip sweep failed trip=%s', trip.id)
    return out
