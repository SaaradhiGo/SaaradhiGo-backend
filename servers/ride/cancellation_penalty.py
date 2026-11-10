"""The MVAG 2025 cancellation penalty (owner brief, 2026-11-09).

THE RULE (config per country and state: `pricing.regulation.cancel_policy`)
-------------------------------------------------------------------------
An UNJUSTIFIED cancellation costs `percent` of the fare, at most `cap_minor` (India: 10%, at most
100.00 INR), for the rider or the driver alike. Never charged when:
  * the market has no penalty configured, or the `cancellation_penalty` flag is off there;
  * no driver had accepted (nobody was inconvenienced);
  * it falls inside the free window after acceptance (`free_seconds`);
  * the reason is `safety`;
  * it is a no-show by the OTHER side: a rider cancelling `no_show` before the driver reached,
    or a driver cancelling `no_show` after reaching the pickup.

THE MONEY
---------
RIDER pays: nothing is charged now. The penalty is DUE: the rider is shown it before the next
booking (`due_for_rider`), the next trip carries it (`attach_to_trip`) and its payable amount
includes it (`rider_promos.payable_minor`), so a cash rider hands it over with the fare, never
without notice. When that trip settles (`settle_collected`) the driver who was let down is paid
it: a `cancellation_compensation` credit. On a cash trip the collecting driver holds it, so their
wallet carries a matching `cancellation_fee_collected` debit.
DRIVER pays: a `cancellation_penalty` debit on their settlement balance at once. The existing
lockout rule (`driver.fatigue`) is untouched and still applies.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

FLAG = 'cancellation_penalty'
PURPOSE_COMPENSATION = 'cancellation_compensation'
PURPOSE_PENALTY = 'cancellation_penalty'
PURPOSE_COLLECTED = 'cancellation_fee_collected'


def _market(trip):
    """`(country, state_code, city_node)` of the trip's pickup zone."""
    from servers.pricing.commission import city_node_of, country_code_of_zone
    from servers.pricing.regulation import state_code_of_zone

    zone = getattr(trip, 'zone', None)
    if zone is None:
        return None, '', None
    return (country_code_of_zone(zone), state_code_of_zone(zone),
            city_node_of(getattr(zone, 'geo_node', None)))


def _reason(trip):
    return (trip.cancellation_reason or '').split(':')[0].strip().lower()


def exemption(trip, now):
    """Why this cancellation costs nothing, or None when it is unjustified."""
    if trip.cancelled_by not in ('rider', 'driver'):
        return 'not_a_party'
    if trip.driver_id_id is None or trip.accepted_at is None:
        return 'no_driver_accepted'
    reason = _reason(trip)
    if reason == 'safety':
        return 'safety'
    if reason == 'no_show':
        if trip.cancelled_by == 'rider' and trip.reached_at is None:
            return 'driver_no_show'
        if trip.cancelled_by == 'driver' and trip.reached_at is not None:
            return 'rider_no_show'
    return None


def amount_for(policy, fare_minor):
    raw = (Decimal(int(fare_minor)) * policy.percent / Decimal('100')).quantize(
        Decimal('1'), rounding=ROUND_HALF_UP)
    amount = int(raw)
    if policy.cap_minor is not None:
        amount = min(amount, int(policy.cap_minor))
    return max(amount, 0)


def assess(trip, now=None):
    """Apply the penalty for a trip just saved as cancelled. Idempotent; returns the row or None.

    Call inside the cancelling transaction: the row and the driver's debit commit with it.
    """
    from servers.platform_ops.flags import flag_enabled
    from servers.pricing.regulation import cancel_policy

    from .models import CancellationPenalty

    existing = CancellationPenalty.objects.filter(trip=trip).first()
    if existing is not None:
        return existing
    now = now or trip.cancelled_at or timezone.now()
    if exemption(trip, now) is not None:
        return None
    country, state, city = _market(trip)
    policy = cancel_policy(country, state) if country else None
    if policy is None or not flag_enabled(FLAG, city=city, country=country):
        return None
    if (now - trip.accepted_at).total_seconds() < policy.free_seconds:
        return None
    fare_minor = trip.estimated_fare_minor or 0
    amount = amount_for(policy, fare_minor)
    if amount <= 0:
        return None
    from base.money import currency_for_trip

    currency = currency_for_trip(trip)
    party = trip.cancelled_by
    with transaction.atomic():
        try:
            with transaction.atomic():
                row = CancellationPenalty.objects.create(
                    trip=trip, party=party, rider=trip.user_id, driver=trip.driver_id,
                    currency=currency, amount_minor=amount, percent=policy.percent,
                    cap_minor=policy.cap_minor, fare_minor=fare_minor,
                    status=(CancellationPenalty.DUE if party == 'rider'
                            else CancellationPenalty.CHARGED))
        except IntegrityError:
            return CancellationPenalty.objects.get(trip=trip)
        type(trip).objects.filter(pk=trip.pk).update(cancellation_fee_minor=amount)
        trip.cancellation_fee_minor = amount
        if party == 'driver':
            _wallet_line(trip.driver_id, currency, amount, 'debit', PURPOSE_PENALTY,
                         f'TRIP_{trip.id}', f'TRIP_{trip.id}_CANCEL_PENALTY')
            row.settled_at = now
            row.save(update_fields=['settled_at'])
    logger.info('cancellation_penalty', extra={
        'event': 'cancellation_penalty', 'trip_id': trip.id, 'party': party,
        'amount_minor': amount})
    return row


def _wallet_line(driver, currency, amount, txn_type, purpose, reference, key):
    """One completed driver-ledger row and the balance move it explains, exactly once."""
    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER, lock=True)
    try:
        with transaction.atomic():
            WalletTransaction.objects.create(
                user_id=driver.user_id, currency=currency, amount_minor=amount,
                txn_type=txn_type, status='completed', purpose=purpose,
                reference_id=reference, idempotency_key=key)
    except IntegrityError:
        return False
    wallet.balance_minor += amount if txn_type == 'credit' else -amount
    wallet.save(update_fields=['balance_minor'])
    return True


# ---------------------------------------------------------------------------
# The rider's due: shown before booking, carried by the next trip
# ---------------------------------------------------------------------------

def _open_dues(user, currency=None):
    """Due penalties not yet carried by a live trip (one carried by a trip that was then
    cancelled, or ended by SaaradhiGo with nothing charged, is free to be carried again)."""
    from django.db.models import Q

    from .models import CancellationPenalty

    qs = CancellationPenalty.objects.filter(rider=user, status=CancellationPenalty.DUE).filter(
        Q(collected_on_trip__isnull=True)
        | Q(collected_on_trip__status_id__status_code='cancelled')
        | Q(collected_on_trip__end_rule='platform_funded'))
    if currency:
        qs = qs.filter(currency=currency)
    return qs


def due_for_rider(user):
    """What the rider is told before booking, or None. Amounts are minor units."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    rows = list(_open_dues(user).order_by('created_at'))
    if not rows:
        return None
    from base.money import from_minor

    currency = rows[0].currency
    total = sum(r.amount_minor for r in rows if r.currency == currency)
    return {'amount': str(from_minor(total, currency)), 'amount_minor': total,
            'currency': currency, 'trip_ids': [r.trip_id for r in rows if r.currency == currency],
            'label': 'Cancellation fee from an earlier trip',
            'note': 'Added to your next ride.'}


def attach_to_trip(trip):
    """The new trip carries the rider's open dues in its currency. Returns the amount."""
    from base.money import currency_for_trip

    from .models import CancellationPenalty

    # FOR UPDATE cannot reach across the nullable join in `_open_dues` (PostgreSQL refuses it):
    # find the ids, then lock those rows by primary key.
    ids = list(_open_dues(trip.user_id, currency_for_trip(trip)).values_list('id', flat=True))
    rows = list(CancellationPenalty.objects.select_for_update().filter(pk__in=ids))
    for row in rows:
        row.collected_on_trip = trip
        row.save(update_fields=['collected_on_trip'])
    return sum(r.amount_minor for r in rows)


def dues_minor(trip):
    """The cancellation dues this trip carries (added to what the rider pays)."""
    from .models import CancellationPenalty

    if trip is None or trip.pk is None:
        return 0
    return sum(CancellationPenalty.objects.filter(
        collected_on_trip=trip,
        status__in=(CancellationPenalty.DUE, CancellationPenalty.COLLECTED),
    ).values_list('amount_minor', flat=True))


def due_lines(trip):
    """`[(label, amount Decimal)]` for the receipt of the trip that carried them."""
    from base.money import from_minor

    from .models import CancellationPenalty

    return [(f'Cancellation fee (trip {r.trip_id})', from_minor(r.amount_minor, r.currency))
            for r in CancellationPenalty.objects.filter(
                collected_on_trip=trip,
                status__in=(CancellationPenalty.DUE, CancellationPenalty.COLLECTED))]


def settle_collected(trip, *, is_cash):
    """The trip carrying the dues settled: pay each to the driver who was let down."""
    from .models import CancellationPenalty

    now = timezone.now()
    for row in (CancellationPenalty.objects.select_for_update().select_related('driver__user_id')
                .filter(collected_on_trip=trip, status=CancellationPenalty.DUE)):
        if is_cash and trip.driver_id_id:
            _wallet_line(trip.driver_id, row.currency, row.amount_minor, 'debit', PURPOSE_COLLECTED,
                         f'TRIP_{trip.id}', f'TRIP_{trip.id}_CANCEL_DUE_{row.id}')
        _wallet_line(row.driver, row.currency, row.amount_minor, 'credit', PURPOSE_COMPENSATION,
                     f'TRIP_{row.trip_id}', f'CANCEL_COMP_{row.id}')
        row.status = CancellationPenalty.COLLECTED
        row.settled_at = now
        row.save(update_fields=['status', 'settled_at'])


def preview(trip, now=None):
    """What cancelling now would cost the rider, for the cancel button (9 Nov):
    {free, free_until (ISO or None), fee (str or None), fee_minor, currency}.

    Free while no driver has accepted, inside the free window, and wherever no penalty applies;
    after the window the fee the rider would owe, never a guess."""
    from base.money import currency_for_trip, from_minor
    from servers.platform_ops.flags import flag_enabled
    from servers.pricing.regulation import cancel_policy

    now = now or timezone.now()
    currency = currency_for_trip(trip)
    free = {'free': True, 'free_until': None, 'fee': None, 'fee_minor': 0, 'currency': currency}
    if trip.driver_id_id is None or trip.accepted_at is None:
        return free
    country, state, city = _market(trip)
    policy = cancel_policy(country, state) if country else None
    if policy is None or not flag_enabled(FLAG, city=city, country=country):
        return free
    from datetime import timedelta

    until = trip.accepted_at + timedelta(seconds=policy.free_seconds)
    if now < until:
        return {**free, 'free_until': until.isoformat()}
    amount = amount_for(policy, trip.estimated_fare_minor or 0)
    return {'free': amount <= 0, 'free_until': None, 'fee': str(from_minor(amount, currency)),
            'fee_minor': amount, 'currency': currency}
