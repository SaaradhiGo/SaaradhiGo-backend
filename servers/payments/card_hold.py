"""Card payments held at booking and captured at trip end; tips (14 Nov, USA from day 1).

Where cash is not allowed (Idaho Code 49-3712) the rider pays in the app. The flow:

1. Fares: the rider sees a locked quote (`ride.quotes`).
2. Hold:  POST /payments/card/hold/ {quote_id} -> a Stripe PaymentIntent for exactly the quoted
          fare, `capture_method=manual`, Apple Pay, Google Pay and cards (automatic payment
          methods), saved to the rider's Stripe customer for the tip. The browser confirms it with
          Stripe.js; the card never reaches this server. The hold is `authorised` once Stripe says
          `requires_capture`.
3. Book:  the booking socket links the hold to the trip (`link_to_trip`).
4. End:   at completion the trip's payable amount (never more than the hold) is captured
          (`capture_for_trip`, a task after commit) and the trip settles as any online payment.
          A failed capture leaves the trip `failed`: the existing unsettled-trip retry path.
5. Cancel: an uncaptured hold is released.
6. Tip:   POST /ride/trips/<id>/tip/ {percent | amount} after the trip, charged off-session to the
          saved card; 100% is credited to the driver (`WalletTransaction` purpose `tip`).
"""
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response as _error_response, success_response

logger = logging.getLogger(__name__)

TIP_PURPOSE = 'tip'
#: A tip is at most the fare (a fat-finger guard, not a policy) and at least one minor unit.
TIP_MAX_OF_FARE = Decimal('1.00')


def error_response(*, code, message, status, field=''):
    return _error_response(code=code, message=message, field=field, issue=message, status=status)


def _gateway():
    """Stripe when configured for the US; on QA without keys, the fake card processor."""
    from integrations.payments.fake_card import FakeCardGateway, allowed
    from servers.payments.payment_gateways.factory import get_payment_gateway_for_payments

    gw = get_payment_gateway_for_payments('US')
    if gw and gw.get_name() == 'stripe':
        return gw
    return FakeCardGateway() if allowed() else None


def card_available(country):
    """Card payment can be offered in this country (Stripe, or the QA fake)."""
    return str(country or '').upper() == 'US' and _gateway() is not None


def _customer_for(user, gateway):
    """The rider's Stripe customer: reused from an earlier hold, else created."""
    from .models import CardHold

    existing = (CardHold.objects.filter(user=user).exclude(customer_id='')
                .order_by('-created_at').values_list('customer_id', flat=True).first())
    return existing or gateway.create_customer(user)


def create_hold(user, quote_id):
    """`(hold, None)` or `(None, (code, message))`."""
    from servers.ride import quotes

    from .models import CardHold

    quote = quotes.cache.get(quotes.KEY.format(quote_id)) if quote_id else None
    if quote is None or (quote['rider_id'] is not None and quote['rider_id'] != user.id):
        return None, ('QUOTE_EXPIRED', 'This fare has expired. Get a new one.')
    fare = quote['fare']
    currency = str(fare.get('currency') or '').upper()
    gateway = _gateway()
    if gateway is None or currency != 'USD':
        return None, ('CARD_UNAVAILABLE', 'Card payment is not available here yet.')
    from base.money import to_minor

    amount_minor = to_minor(Decimal(str(fare['total_fare'])), currency)
    customer = _customer_for(user, gateway)
    if not customer:
        return None, ('CARD_UNAVAILABLE', 'We could not reach the card processor. Try again.')
    intent = gateway.create_hold(amount_minor, currency=currency, customer_id=customer,
                                 reference=f'hold_{user.id}_{quote_id}',
                                 metadata={'quote_id': quote_id, 'rider_id': user.id})
    if not intent:
        return None, ('CARD_UNAVAILABLE', 'We could not reach the card processor. Try again.')
    fake = gateway.get_name() == 'fake_card'
    hold, _ = CardHold.objects.get_or_create(intent_id=intent['id'], defaults={
        'user': user, 'provider': gateway.get_name(), 'amount_minor': amount_minor, 'currency': currency,
        'quote_id': quote_id, 'customer_id': customer,
        # The QA fake authorises at once (no browser step); Stripe waits for the browser.
        'status': CardHold.AUTHORISED if fake else CardHold.CREATED,
        'payment_method_id': 'pm_fake_visa_4242' if fake else ''})
    hold.test_mode = fake
    hold.client_secret = intent.get('client_secret')
    return hold, None


def refresh(hold):
    """Read the intent's state from Stripe (money moves on Stripe's word only)."""
    gateway = _gateway()
    data = gateway.get_intent(hold.intent_id) if gateway else None
    if not data:
        return hold
    state = data.get('status')
    fields = []
    if data.get('payment_method') and data['payment_method'] != hold.payment_method_id:
        hold.payment_method_id = str(data['payment_method'])
        fields.append('payment_method_id')
    new = {'requires_capture': hold.AUTHORISED, 'succeeded': hold.CAPTURED,
           'canceled': hold.RELEASED}.get(state)
    if new and new != hold.status:
        hold.status = new
        fields.append('status')
    if fields:
        hold.save(update_fields=fields + ['updated_at'])
    return hold


def link_to_trip(user, hold_id, trip):
    """Attach an authorised hold to the trip just booked. Returns an error string or None."""
    from .models import CardHold

    with transaction.atomic():
        hold = CardHold.objects.select_for_update().filter(pk=hold_id, user=user).first()
        if hold is None or hold.trip_id is not None:
            return 'This card hold cannot be used. Confirm your card again.'
        if hold.status != CardHold.AUTHORISED:
            refresh(hold)
        if hold.status != CardHold.AUTHORISED:
            return 'Your card has not been confirmed yet.'
        payable = _payable_minor(trip)
        if payable and payable > hold.amount_minor:
            return 'The fare is more than the amount held on your card. Confirm again.'
        hold.trip = trip
        hold.save(update_fields=['trip', 'updated_at'])
    return None


def _payable_minor(trip):
    from servers.promotions.rider_promos import payable

    from base.money import to_minor

    amount = payable(trip)
    return to_minor(Decimal(str(amount)), trip.currency) if amount else 0


def capture_for_trip(trip_id):
    """Capture the held amount for a completed trip and settle it. Idempotent."""
    from servers.ride.models import Trip

    from .models import CardHold, Payment, TransactionHistory

    trip = Trip.objects.select_related('driver_id', 'user_id').filter(pk=trip_id).first()
    hold = CardHold.objects.filter(trip_id=trip_id).first()
    if trip is None or hold is None or hold.status == CardHold.CAPTURED:
        return 'nothing to do'
    gateway = _gateway()
    amount = min(_payable_minor(trip), hold.amount_minor)
    data = gateway.capture_hold(hold.intent_id, amount, reference=f'capture_trip_{trip.id}') if gateway else None
    with transaction.atomic():
        trip = Trip.objects.select_for_update(of=('self',)).select_related('driver_id', 'user_id').get(pk=trip_id)
        payment = Payment.objects.select_for_update().filter(trip_id=trip, gateway_order_id=hold.intent_id).first()
        if payment is None:
            payment = Payment.objects.create(trip_id=trip, user_id=trip.user_id, amount_minor=amount,
                                             method='online', status='pending',
                                             payment_gateway=gateway.get_name() if gateway else hold.provider,
                                             gateway_order_id=hold.intent_id)
        if not data or data.get('status') != 'succeeded':
            payment.status = 'failed'
            payment.gateway_metadata = {**(payment.gateway_metadata or {}), 'capture_error':
                                        (data or {}).get('error', 'no answer')}
            payment.save(update_fields=['status', 'gateway_metadata'])
            hold.status = CardHold.FAILED
            hold.save(update_fields=['status', 'updated_at'])
            trip.payment_status = 'failed'
            trip.save(update_fields=['payment_status'])
            logger.error('card capture failed trip=%s', trip.id)
            return 'failed'
        payment.gateway_payment_id = str(data.get('latest_charge') or hold.intent_id)
        payment.status = 'completed'
        payment.save(update_fields=['gateway_payment_id', 'status'])
        hold.status = CardHold.CAPTURED
        hold.captured_minor = amount
        if data.get('payment_method'):
            hold.payment_method_id = str(data['payment_method'])
        hold.save(update_fields=['status', 'captured_minor', 'payment_method_id', 'updated_at'])
        trip.payment_status = 'completed'
        trip.save(update_fields=['payment_status'])
        if trip.driver_id:
            TransactionHistory.objects.create(
                trip_id=trip, user_id=trip.user_id, driver_id=trip.driver_id, amount=payment.amount,
                method='online', payment_gateway=hold.provider,
                user_name=trip.user_id.full_name or trip.user_id.phone_number or '', status='completed')
            from servers.driver.utils import settle_paid_by_platform
            settle_paid_by_platform(trip, payment.amount_minor)
    return 'captured'


def release_for_trip(trip):
    """A cancelled trip's uncaptured hold is released (the rider is never charged for it)."""
    from .models import CardHold

    hold = CardHold.objects.filter(trip=trip, status__in=(CardHold.CREATED, CardHold.AUTHORISED)).first()
    if hold is None:
        return False
    gateway = _gateway()
    if gateway and gateway.release_hold(hold.intent_id):
        hold.status = CardHold.RELEASED
        hold.save(update_fields=['status', 'updated_at'])
        return True
    return False


# ---------------------------------------------------------------------------
# Tips
# ---------------------------------------------------------------------------

def tip_amount_minor(trip, *, percent=None, amount=None):
    from base.money import to_minor

    fare = _payable_minor(trip)
    if percent is not None:
        value = (Decimal(fare) * Decimal(str(percent)) / Decimal(100)).quantize(Decimal('1'), ROUND_HALF_UP)
        return int(value)
    return to_minor(Decimal(str(amount)), trip.currency)


def give_tip(user, trip, *, percent=None, amount=None):
    """`(tip, None)` or `(None, (code, message, http))`. 100% goes to the driver."""
    from servers.pricing.regulation import rules_for_zone

    from .models import CardHold, Tip

    rules = rules_for_zone(getattr(trip, 'zone', None))
    if not rules['tips_enabled']:
        return None, ('TIPS_OFF', 'Tips are not offered here.', 409)
    if (trip.status_id.status_code if trip.status_id else '') != 'completed' or trip.driver_id is None:
        return None, ('NOT_COMPLETED', 'You can tip once the trip has ended.', 409)
    if Tip.objects.filter(trip=trip, status=Tip.SUCCEEDED).exists():
        return None, ('ALREADY_TIPPED', 'You have already tipped for this trip.', 409)
    try:
        minor = tip_amount_minor(trip, percent=percent, amount=amount)
    except Exception:  # noqa: BLE001
        return None, ('INVALID_VALUE', 'Enter a tip amount.', 400)
    fare = _payable_minor(trip)
    if minor <= 0 or minor > int(Decimal(fare) * TIP_MAX_OF_FARE) + 0:
        return None, ('INVALID_VALUE', 'A tip must be more than zero and at most the fare.', 400)
    hold = CardHold.objects.filter(trip=trip).first()
    if hold is not None and not hold.payment_method_id:
        refresh(hold)
    gateway = _gateway()
    if hold is None or not hold.payment_method_id or gateway is None:
        return None, ('TIP_UNAVAILABLE', 'We cannot charge a tip to this trip\'s card.', 409)
    try:
        tip = Tip.objects.create(trip=trip, rider=user, driver=trip.driver_id, amount_minor=minor,
                                 currency=trip.currency, percent=percent, status=Tip.PENDING)
    except IntegrityError:
        return None, ('ALREADY_TIPPED', 'You have already tipped for this trip.', 409)
    data = gateway.charge_saved(hold.customer_id, hold.payment_method_id, minor, currency=trip.currency,
                                reference=f'tip_trip_{trip.id}', metadata={'trip_id': trip.id, 'kind': 'tip'})
    if not data or data.get('status') != 'succeeded':
        tip.status = Tip.FAILED
        tip.save(update_fields=['status'])
        return None, ('TIP_FAILED', 'Your card declined the tip. Nothing was charged.', 402)
    with transaction.atomic():
        tip.status = Tip.SUCCEEDED
        tip.intent_id = str(data.get('id') or '')
        tip.save(update_fields=['status', 'intent_id'])
        _credit_driver(tip)
    try:
        from servers.ride.receipts import reissue_receipt
        reissue_receipt(trip)
    except Exception:  # noqa: BLE001 - the tip is paid either way; the receipt follows
        logger.exception('receipt reissue after tip failed trip=%s', trip.id)
    return tip, None


def _credit_driver(tip):
    """100% of the tip to the driver's wallet: append-only and keyed per trip."""
    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    user = tip.driver.user_id
    wallet = get_wallet(user, Wallet.SCOPE_DRIVER, lock=True)
    try:
        with transaction.atomic():
            WalletTransaction.objects.create(
                user_id=user, currency=tip.currency, amount_minor=tip.amount_minor, txn_type='credit',
                status='completed', purpose=TIP_PURPOSE, reference_id=f'TRIP_{tip.trip_id}',
                idempotency_key=f'TRIP_{tip.trip_id}_TIP')
    except IntegrityError:
        return
    wallet.balance_minor = wallet.balance_minor + tip.amount_minor
    wallet.save(update_fields=['balance_minor'])


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def hold_view(request):
    """POST /payments/card/hold/ {quote_id} -> {hold_id, client_secret, publishable_key, amount}"""
    from django.conf import settings

    hold, err = create_hold(request.user, str(request.data.get('quote_id') or ''))
    if err:
        return error_response(code=err[0], message=err[1], status=status.HTTP_409_CONFLICT, field='quote_id')
    from base.money import to_major_string

    return success_response({
        'hold_id': hold.id, 'client_secret': hold.client_secret,
        'publishable_key': getattr(settings, 'STRIPE_PUBLISHABLE_KEY', '') or '',
        'amount': to_major_string(hold.amount_minor, hold.currency), 'currency': hold.currency,
        'status': hold.status,
        # QA without Stripe keys: no card is charged, and the web says so.
        'test_mode': bool(getattr(hold, 'test_mode', False)),
    }, status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def hold_status_view(request, hold_id):
    from .models import CardHold

    hold = CardHold.objects.filter(pk=hold_id, user=request.user).first()
    if hold is None:
        return error_response(code='NOT_FOUND', message='No such card hold.', status=status.HTTP_404_NOT_FOUND)
    refresh(hold)
    return success_response({'hold_id': hold.id, 'status': hold.status}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def tip_view(request, trip_id):
    """POST /ride/trips/<id>/tip/ {percent} or {amount}"""
    from servers.ride.models import Trip
    from servers.ride.views import trip_not_found

    trip = Trip.objects.select_related('status_id', 'driver_id__user_id', 'zone').filter(
        id=trip_id, user_id=request.user).first()
    if trip is None:
        return trip_not_found(trip_id)
    percent = request.data.get('percent')
    amount = request.data.get('amount')
    if (percent is None) == (amount is None):
        return error_response(code='INVALID_VALUE', field='percent', message='Give a percent or an amount.',
                              status=status.HTTP_400_BAD_REQUEST)
    tip, err = give_tip(request.user, trip, percent=percent, amount=amount)
    if err:
        return error_response(code=err[0], message=err[1], status=err[2])
    from base.money import to_major_string

    return success_response({'tip_id': tip.id, 'amount': to_major_string(tip.amount_minor, tip.currency),
                             'currency': tip.currency, 'to_driver': '100%',
                             'at': timezone.now().isoformat()}, status.HTTP_201_CREATED)
