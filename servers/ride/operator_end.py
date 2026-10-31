"""An operator ends a STARTED trip that has gone stale (Stale trip panel, case C).

THE OWNER'S RULING (2026-10-31)
-------------------------------
Ops ends a started trip: the rider pays the fare for the distance actually driven,
capped at the upfront price; the driver earns normally on that, with commission as
usual. Exception: when the release reason is "safety incident" or "platform/app
fault" the rider pays 0 and the platform pays the driver the earnings for the
distance driven (a platform-funded ledger entry). Reason codes are required and
decide which rule applies.

HOW THE NUMBERS ARE MADE
------------------------
Distance driven  The recorded GPS trail from `started_at` up to the driver's LAST
                 ACTIVITY (haversine sum, a segment implying more than 200 km/h is
                 GPS noise and is dropped); else `actual_distance_km` if already
                 set; else 0.
Time             `started_at` to the driver's last activity. The silent period after
                 it is never charged.
Fare             The trip's OWN quoted fare card (the rate card in force when it was
                 requested; rate cards are immutable and versioned) applied to that
                 distance and time, with the minimum fare, then the surge multiplier
                 the quote was booked with (recorded on the trip, NOT recomputed),
                 then in an additive-tax market the tax the quote carried; then
                 capped at the upfront price. Never above the quote.
Commission       The trip's booking-time snapshot, exactly as a normal settlement.

NOTHING NEW IS CHARGED. A normal completion charges `final_fare or estimated_fare`
and adds no fee, tip or toll; neither does this. No fare-card, surge, fee or
commission value is changed anywhere.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from base.money import currency_for_trip, format_money, from_minor, quantize, to_minor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reason codes: one place
# ---------------------------------------------------------------------------

STANDARD = 'standard'
PLATFORM_FUNDED = 'platform_funded'

DRIVER_UNRESPONSIVE = 'driver_unresponsive'
TRIP_ABANDONED = 'trip_abandoned'
OTHER = 'other'
SAFETY_INCIDENT = 'safety_incident'
PLATFORM_FAULT = 'platform_fault'

#: code -> (label, rule). The order is the order the console shows.
REASON_CODES = {
    DRIVER_UNRESPONSIVE: ('Driver unresponsive', STANDARD),
    TRIP_ABANDONED: ('Trip abandoned', STANDARD),
    OTHER: ('Other', STANDARD),
    SAFETY_INCIDENT: ('Safety incident', PLATFORM_FUNDED),
    PLATFORM_FAULT: ('Platform or app fault', PLATFORM_FUNDED),
}

#: What the audit row and the trip carry for the platform-funded rule.
PLATFORM_FUNDED_LEDGER_PURPOSE = 'platform_funded_trip_end'
RECEIPT_NOT_CHARGED_LINE = 'Not charged: ended by SaaradhiGo'

#: A segment implying a higher speed than this is GPS noise, not a car on a road.
MAX_SPEED_KMH = 200.0

ALERT_SOURCE_TYPE = 'trip_platform_funded_end'


def rule_for(code):
    entry = REASON_CODES.get(code)
    return entry[1] if entry else None


def reason_code_options():
    """For the console: the codes, their labels and the rule each one triggers."""
    return [{'code': code, 'label': label, 'rule': rule}
            for code, (label, rule) in REASON_CODES.items()]


class Unavailable(Exception):
    """The numbers cannot be made. `code` is the refusal the caller reports."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------------------------------------------------------------------
# Distance and time
# ---------------------------------------------------------------------------

def _two(value):
    return Decimal(str(value)).quantize(Decimal('0.01'))


def last_activity(trip):
    """The driver's last sign of life on this trip, never before the trip started."""
    from servers.ride.liveness import last_known_activity

    started = trip.started_at
    last = trip.last_driver_activity_at or last_known_activity(trip) or started
    if started is not None and (last is None or last < started):
        last = started
    return min(last, timezone.now()) if last else None


def driven_distance(trip, until):
    """`(distance_km Decimal, source, points)`; source is trail / recorded / none."""
    from servers.ride.location_trail import haversine_metres
    from servers.ride.models import TripLocationPoint

    if trip.started_at is not None and until is not None:
        points = list(TripLocationPoint.objects
                      .filter(trip=trip, recorded_at__gte=trip.started_at, recorded_at__lte=until)
                      .order_by('recorded_at', 'sequence', 'id'))
        if len(points) >= 2:
            total_m = 0.0
            for prev, cur in zip(points, points[1:], strict=False):
                seg_m = haversine_metres(prev.latitude, prev.longitude,
                                         cur.latitude, cur.longitude)
                gap_s = (cur.recorded_at - prev.recorded_at).total_seconds()
                if gap_s > 0:
                    if (seg_m / 1000.0) / (gap_s / 3600.0) > MAX_SPEED_KMH:
                        continue
                elif seg_m > 50.0:
                    continue  # two fixes at one instant, far apart: one of them is wrong
                total_m += seg_m
            return _two(total_m / 1000.0), 'trail', len(points)
    if trip.actual_distance_km is not None:
        return _two(trip.actual_distance_km), 'recorded', 0
    return Decimal('0.00'), 'none', 0


# ---------------------------------------------------------------------------
# The fare
# ---------------------------------------------------------------------------

def _card(trip):
    from servers.pricing.services import rate_card_for_trip

    return rate_card_for_trip(trip=trip)


def metered_fare(trip, distance_km, minutes):
    """The quoted fare card applied to what was driven. Returns a dict of Decimals.

    Mirrors the arithmetic of `pricing.services.quote_fare` (base + per km + per
    minute, the minimum fare, the booked surge, additive tax) without any of its
    lookups: no demand is recorded and surge is not recomputed.
    """
    card = _card(trip)
    if card is None:
        raise Unavailable(
            'fare_card_unavailable',
            'The fare card this trip was quoted on cannot be found, so the fare for the '
            'distance driven cannot be worked out.')
    currency = currency_for_trip(trip)
    base, per_km, per_min = card.base_fare, card.per_km_fare, card.per_min_fare
    distance_fare = per_km * distance_km
    time_fare = per_min * minutes
    pre_surge = base + distance_fare + time_fare
    surge = Decimal(str(trip.surge_multiplier or '1.00'))
    subtotal = pre_surge * surge if surge != Decimal('1.00') else pre_surge
    min_applied = False
    if subtotal < card.min_fare:
        subtotal, min_applied = card.min_fare, True
    total = quantize(subtotal, currency)
    zone = getattr(trip, 'zone', None)
    if (getattr(zone, 'tax_mode', '') or 'inclusive') == 'additive':
        tax_percent = Decimal(str(getattr(card, 'gst_percent', 0) or 0))
        fee = quantize(Decimal(str(getattr(zone, 'per_trip_fee', None) or '0')), currency)
        total = total + quantize(total * tax_percent / Decimal('100'), currency) + fee
    return {
        'base': quantize(base, currency), 'distance_fare': quantize(distance_fare, currency),
        'time_fare': quantize(time_fare, currency), 'surge_multiplier': surge,
        'min_fare_applied': min_applied, 'total': total, 'card': card,
    }


# ---------------------------------------------------------------------------
# The plan: everything the release will do, worked out before anything is written
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    trip_id: int
    reason_code: str
    rule: str
    currency: str
    payment_method: str
    last_activity_at: object
    distance_km: Decimal
    distance_source: str
    trail_points: int
    elapsed_minutes: Decimal
    uncapped_minor: int
    upfront_minor: int
    capped_minor: int
    rider_pays_minor: int
    commission_percent: object
    commission_minor: object
    driver_earnings_minor: object
    funded_by: str
    metered: dict = field(default_factory=dict, repr=False)

    def as_dict(self):
        """The preview payload and the audit figures. No coordinates, no contact detail."""
        return {
            'trip_id': self.trip_id,
            'reason_code': self.reason_code,
            'rule': self.rule,
            'currency': self.currency,
            'payment_method': self.payment_method,
            'last_activity_at': self.last_activity_at.isoformat() if self.last_activity_at else None,
            'distance_km': str(self.distance_km),
            'distance_source': self.distance_source,
            'elapsed_minutes': str(self.elapsed_minutes),
            'uncapped_fare_minor': self.uncapped_minor,
            'upfront_price_minor': self.upfront_minor,
            'capped_fare_minor': self.capped_minor,
            'cap_applied': self.uncapped_minor > self.upfront_minor,
            'rider_pays_minor': self.rider_pays_minor,
            'commission_percent': (str(self.commission_percent)
                                   if self.commission_percent is not None else None),
            'commission_minor': self.commission_minor,
            'driver_earnings_minor': self.driver_earnings_minor,
            'funded_by': self.funded_by,
            'driver_collects_cash': (self.rule == STANDARD and self.payment_method == 'cash'),
        }


def build_plan(trip, reason_code):
    """Work out the release without writing anything. Raises `Unavailable`."""
    from servers.pricing.commission import CommissionNotConfigured
    from servers.pricing.services import commission_percent_for_trip

    rule = rule_for(reason_code)
    if rule is None:
        raise Unavailable('INVALID_REASON_CODE', 'Unknown reason code.')
    if trip.estimated_fare_minor is None:
        raise Unavailable('no_upfront_price',
                          'This trip has no upfront price, so there is no cap to apply.')
    currency = currency_for_trip(trip)
    until = last_activity(trip)
    distance, source, points = driven_distance(trip, until)
    seconds = (until - trip.started_at).total_seconds() if (until and trip.started_at) else 0
    minutes = _two(max(0.0, seconds) / 60.0)

    metered = metered_fare(trip, distance, minutes)
    uncapped = to_minor(metered['total'], currency)
    upfront = int(trip.estimated_fare_minor)
    capped = min(uncapped, upfront)

    try:
        percent = commission_percent_for_trip(trip)
        commission = to_minor(from_minor(capped, currency) * percent / Decimal('100'), currency)
    except CommissionNotConfigured:
        percent = commission = None
    if rule == PLATFORM_FUNDED and commission is None:
        raise Unavailable(
            'commission_not_configured',
            'No commission rule governs this trip, so the earnings the platform would pay '
            'cannot be worked out.')
    earnings = None if commission is None else capped - commission
    return Plan(
        trip_id=trip.id, reason_code=reason_code, rule=rule, currency=currency,
        payment_method=trip.payment_method or 'deferred', last_activity_at=until,
        distance_km=distance, distance_source=source, trail_points=points,
        elapsed_minutes=minutes, uncapped_minor=uncapped, upfront_minor=upfront,
        capped_minor=capped, rider_pays_minor=0 if rule == PLATFORM_FUNDED else capped,
        commission_percent=percent, commission_minor=commission,
        driver_earnings_minor=earnings,
        funded_by='platform' if rule == PLATFORM_FUNDED else 'rider', metered=metered)


# ---------------------------------------------------------------------------
# Applying it (inside the caller's transaction and row lock)
# ---------------------------------------------------------------------------

def _snapshot(trip, plan):
    from servers.ride.models import FarePricing

    m = plan.metered
    card = m['card']
    return FarePricing.objects.create(
        trip_id=trip, base_fare=m['base'], distance_fare=m['distance_fare'],
        time_fare=m['time_fare'], surge_multiplier=m['surge_multiplier'],
        total_fare=from_minor(plan.uncapped_minor, plan.currency),
        snapshot_type=FarePricing.SNAPSHOT_OPERATOR_END,
        fare_basis=(FarePricing.BASIS_METERED if plan.distance_source == 'trail'
                    else FarePricing.BASIS_ESTIMATE_NO_TELEMETRY),
        finalized_at=timezone.now(),
        quoted_distance_km=trip.estimated_distance_km,
        quoted_duration_min=trip.estimated_duration_min,
        actual_distance_km=plan.distance_km, actual_duration_min=plan.elapsed_minutes,
        min_fare_applied=m['min_fare_applied'],
        rate_card_version=getattr(card, 'version', None),
        zone_code=getattr(getattr(trip, 'zone', None), 'code', '') or '',
        vehicle_type=getattr(trip.requested_vehicle_type, 'type', '') or '',
        pricing_source='db', trail_points=plan.trail_points,
        gross_fare_minor=plan.capped_minor, discount_amount_minor=0,
        rider_payable_minor=plan.rider_pays_minor)


def _refund_prepaid(trip, plan, keep_minor):
    """Give back what the rider paid in advance beyond `keep_minor`. Returns minor refunded.

    Full refund (the platform-funded rule): the existing cancellation refund helper. A
    partial refund (the standard rule, the rider prepaid more than the capped fare), or a
    full refund the gateway did not take: recorded as a refund the platform owes
    (`TripMoneyAction`, pending gateway for an online payment, completed for a wallet
    credit), the same ledger the manual refunds use, so Finance sees it.
    """
    from servers.payments.models import Payment
    from servers.ride.models import TripMoneyAction
    from servers.ride.utils import process_refund_on_cancel

    paid = sum(Payment.objects.filter(trip_id=trip, status='completed', method__in=('online', 'wallet'))
               .values_list('amount_minor', flat=True))
    owed = paid - keep_minor
    if owed <= 0:
        return 0
    currency = plan.currency
    online = Payment.objects.filter(trip_id=trip, status='completed', method='online').exists()
    if keep_minor == 0 and online:
        try:
            if process_refund_on_cancel(trip).get('refunded'):
                return owed
        except Exception:  # noqa: BLE001 - recorded as owed below
            logger.exception('operator end: gateway refund failed for trip %s', trip.id)
    method, state, wallet_txn = TripMoneyAction.GATEWAY, TripMoneyAction.PENDING_GATEWAY, None
    if not online:
        from servers.rider.credits import issue_refund_credit

        result = issue_refund_credit(
            trip.user_id, str(from_minor(owed, currency)), trip.id,
            idempotency_key=f'trip_operator_end_refund:{trip.id}')
        if result.ok:
            method, state, wallet_txn = (TripMoneyAction.WALLET_CREDIT, TripMoneyAction.COMPLETED,
                                         result.transaction_id)
    TripMoneyAction.objects.create(
        trip=trip, kind=TripMoneyAction.REFUND, amount_minor=owed, currency=currency,
        method=method, status=state, wallet_transaction_id=wallet_txn,
        reason=f'Trip ended by an operator ({plan.reason_code}): returning what was paid '
               'beyond the fare for the distance driven.')
    return owed


def _credit_driver(trip, plan):
    """The platform-funded earnings: one idempotent ledger credit, no commission."""
    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    if not trip.driver_id or not plan.driver_earnings_minor:
        return None
    key = f'TRIP_{trip.id}_PLATFORM_FUNDED'
    existing = WalletTransaction.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing
    wallet = get_wallet(trip.driver_id.user_id, Wallet.SCOPE_DRIVER, lock=True)
    txn = WalletTransaction.objects.create(
        user_id=trip.driver_id.user_id, currency=plan.currency,
        amount_minor=plan.driver_earnings_minor, txn_type='credit', status='completed',
        purpose=PLATFORM_FUNDED_LEDGER_PURPOSE, reference_id=f'TRIP_{trip.id}',
        idempotency_key=key)
    wallet.balance_minor = wallet.balance_minor + plan.driver_earnings_minor
    wallet.save(update_fields=['balance_minor'])
    return txn


def apply_plan(trip, plan, *, actor):
    """End the trip and move the money. Call inside `transaction.atomic()` with the trip
    row locked. Returns a dict of what was done (for the audit row and the response)."""
    from servers.command_centre.models import FINANCE_PLATFORM_FUNDED_END, WARNING
    from servers.command_centre.services import raise_alert
    from servers.driver.models import Driver
    from servers.payments.unsettled import city_node_of
    from servers.ride.models import TripStatus

    now = timezone.now()
    snapshot = _snapshot(trip, plan)
    completed, _ = TripStatus.objects.get_or_create(
        status_code='completed', defaults={'description': 'Trip completed'})
    trip.status_id = completed
    trip.completed_at = now
    trip.final_fare_minor = plan.rider_pays_minor
    trip.end_reason_code, trip.end_rule, trip.ended_by = plan.reason_code, plan.rule, actor
    trip.stale_flagged_at, trip.stale_reason = None, ''
    fields = ['status_id', 'completed_at', 'final_fare_minor', 'end_reason_code', 'end_rule',
              'ended_by', 'stale_flagged_at', 'stale_reason']
    if trip.actual_distance_km is None:
        trip.actual_distance_km = plan.distance_km
        fields.append('actual_distance_km')
    if trip.actual_duration_min is None:
        trip.actual_duration_min = plan.elapsed_minutes
        fields.append('actual_duration_min')
    if plan.rule == PLATFORM_FUNDED:
        trip.platform_funded_minor = plan.driver_earnings_minor
        fields.append('platform_funded_minor')
    trip.save(update_fields=fields)
    if trip.driver_id_id is not None:
        Driver.objects.filter(id=trip.driver_id_id).update(total_trips=F('total_trips') + 1)

    done = {'fare_pricing_id': snapshot.id}
    if plan.rule == STANDARD:
        from servers.payments.models import TripSettlement
        from servers.ride.utils import create_payment_on_complete

        create_payment_on_complete(trip)
        refunded = _refund_prepaid(trip, plan, keep_minor=plan.capped_minor)
        if not TripSettlement.objects.filter(trip=trip).exists() and (
                trip.payment_method == 'cash' or refunded
                or trip.payments.filter(status='completed').exists()):
            from servers.driver.utils import settle_or_defer

            settle_or_defer(trip)
        done.update(prepaid_refunded_minor=refunded,
                    settled=TripSettlement.objects.filter(trip=trip).exists())
    else:
        refunded = _refund_prepaid(trip, plan, keep_minor=0)
        if not trip.payment_status or trip.payment_status != 'refunded':
            trip.payment_status = 'waived'
            trip.save(update_fields=['payment_status'])
        txn = _credit_driver(trip, plan)
        city = city_node_of(trip)
        raise_alert(
            FINANCE_PLATFORM_FUNDED_END, severity=WARNING,
            title=(f'Trip {trip.id} ended by SaaradhiGo: the platform paid the driver'
                   f'{" in " + city.name if city is not None else ""}')[:200],
            payload={'trip_id': trip.id, 'city': city.code if city is not None else None,
                     'reason_code': plan.reason_code,
                     'driver_earnings_minor': plan.driver_earnings_minor,
                     'currency': plan.currency},
            source_type=ALERT_SOURCE_TYPE, source_id=str(trip.id), city=city)
        done.update(prepaid_refunded_minor=refunded,
                    ledger_transaction_id=txn.id if txn is not None else None)

    rider_id, driver_id, pk = trip.user_id_id, trip.driver_id_id, trip.id
    transaction.on_commit(lambda: after_commit(pk, rider_id, driver_id, plan))
    return done


# ---------------------------------------------------------------------------
# After the commit: receipt, driver freed, both sides told
# ---------------------------------------------------------------------------

def _charge_text(plan):
    if plan.rider_pays_minor <= 0:
        return 'You have not been charged.'
    return f'You pay {format_money(from_minor(plan.rider_pays_minor, plan.currency), plan.currency)} ' \
           'for the distance driven.'


def _earnings_text(plan):
    if plan.driver_earnings_minor is None:
        return 'Your earnings for the distance driven are being worked out.'
    amount = format_money(from_minor(plan.driver_earnings_minor, plan.currency), plan.currency)
    if plan.rule == PLATFORM_FUNDED:
        return f'SaaradhiGo pays you {amount} for the distance driven.'
    return f'You earn {amount} for the distance driven, after commission.'


def after_commit(trip_id, rider_id, driver_id, plan):
    """Best effort, outside the lock. Nothing here may undo the release."""
    from django.contrib.auth import get_user_model

    try:
        from servers.ride.tasks import issue_receipt_for_trip

        issue_receipt_for_trip.delay(trip_id)
    except Exception:  # noqa: BLE001
        logger.exception('operator end: receipt job not queued for trip %s', trip_id)
    try:
        from servers.redis_client import clear_driver_active_trip, invalidate_trip
        from servers.ride.dispatch import dismiss_outstanding_offers

        if driver_id:
            clear_driver_active_trip(driver_id)
        dismiss_outstanding_offers(trip_id, reason='operator_ended')
        invalidate_trip(trip_id)
    except Exception:  # noqa: BLE001
        logger.exception('operator end: cache cleanup failed for trip %s', trip_id)

    User = get_user_model()
    rider = User.objects.filter(pk=rider_id).first()
    driver_user = None
    if driver_id:
        from servers.driver.models import Driver

        uid = Driver.objects.filter(pk=driver_id).values_list('user_id', flat=True).first()
        driver_user = User.objects.filter(pk=uid).first() if uid else None
    try:
        from servers.notifications import dispatcher

        if rider is not None:
            dispatcher.notify(
                RIDER_TEMPLATE, rider,
                {'trip_id': str(trip_id), 'charge_text': _charge_text(plan)},
                channels=('in_app', 'push'), idempotency_key=f'{RIDER_TEMPLATE}:{trip_id}')
        if driver_user is not None:
            dispatcher.notify(
                DRIVER_TEMPLATE, driver_user,
                {'trip_id': str(trip_id), 'earnings_text': _earnings_text(plan)},
                channels=('in_app', 'push'), idempotency_key=f'{DRIVER_TEMPLATE}:{trip_id}')
    except Exception:  # noqa: BLE001
        logger.exception('operator end: notices failed for trip %s', trip_id)
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is not None:
            message = 'Your ride was ended by SaaradhiGo support.'
            async_to_sync(layer.group_send)(f'trip_{trip_id}', {
                'type': 'trip_status_update', 'trip_id': trip_id, 'status': 'complete',
                'message': message, 'driver_id': driver_id})
            async_to_sync(layer.group_send)(f'rider_{rider_id}', {
                'type': 'trip_update', 'trip_id': trip_id, 'status': 'complete',
                'message': message, 'driver_id': driver_id, 'driver_name': ''})
    except Exception:  # noqa: BLE001
        logger.warning('operator end: ride socket event failed for trip %s', trip_id,
                       exc_info=True)


RIDER_TEMPLATE = 'trips.operator_ended_rider'
DRIVER_TEMPLATE = 'trips.operator_ended_driver'
