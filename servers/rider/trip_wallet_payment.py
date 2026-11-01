"""A rider pays a completed trip from their wallet (`POST /rider/wallet/payment/`).

The server is the only authority on the amount (owner ruling, AUDIT 27.71):

* The amount charged is the trip's own payable amount (`rider_promos.payable_minor`: the fare
  less any promo discount). A client `amount` is never charged. If one is sent and differs, the
  request is refused (`AMOUNT_MISMATCH`) and nothing moves, so a stale or tampered screen cannot
  charge a different figure.
* A trip is charged at most once. The ledger key is `trip_<id>_payment`, the same key the
  completion-time wallet charge (`ride.utils.create_payment_on_complete`) uses, so the two paths
  can never both debit the same trip. A repeat of a wallet charge returns the original
  transaction (`duplicate: true`) and moves nothing; a trip already paid any other way (cash the
  driver confirmed, an online payment) is refused (`ALREADY_PAID`).

Before this, the app sent its own amount with a timestamp idempotency key, so every tap was a new
debit, and the trip itself was never marked paid: the driver could still collect cash for it.

THE DRIVER'S LEDGER
-------------------
A cash trip is settled at completion as "the driver holds the fare and owes the commission". When
the rider pays it from the wallet instead, the platform has collected that money on the driver's
behalf, so the driver is credited exactly what the rider was charged
(`driver.utils.credit_fare_collected_by_platform`). That one append-only line brings the balance to
the trip's net, as on an online trip; the settlement and its commission snapshot are untouched. A
trip not yet settled (no commission rule) is settled now as a wallet trip, the normal way.
"""

import re

from django.db import transaction

FARE_KEY = 'trip_{}_payment'
TRIP_REFERENCE = re.compile(r'^(?:TRIP_)?(\d+)$')


class Refusal(Exception):
    def __init__(self, code, message, field, http_status, **extra):
        super().__init__(message)
        self.code, self.message, self.field, self.http_status, self.extra = (
            code, message, field, http_status, extra)


def trip_id_from(data):
    """The trip being paid: `trip_id`, or the legacy `reference_id` the app sends (`123` or
    `TRIP_123`). None when neither names a trip."""
    for key in ('trip_id', 'reference_id'):
        raw = data.get(key)
        if raw in (None, ''):
            continue
        match = TRIP_REFERENCE.match(str(raw).strip())
        if match:
            return int(match.group(1))
    return None


def _replay(user, trip, key):
    from servers.rider.models import WalletTransaction

    txn = WalletTransaction.objects.filter(idempotency_key=key, user_id=user).first()
    if txn is None:
        return None
    return {'transaction_id': txn.id, 'trip_id': trip.id, 'amount': str(txn.amount),
            'currency': txn.currency, 'status': txn.status, 'duplicate': True,
            'message': 'This trip is already paid from your wallet'}


def pay_trip(user, data):
    """Charge the trip's payable amount to the rider's wallet once. Returns (payload, created)."""
    from base.money import currency_for_trip, from_minor, parse_input, to_minor
    from servers.driver.utils import credit_fare_collected_by_platform, settle_or_defer
    from servers.fraud.money import held_minor
    from servers.payments.models import Payment, TripSettlement
    from servers.promotions.rider_promos import is_fully_covered, payable_minor
    from servers.ride.models import Trip
    from servers.rider.models import Wallet, WalletTransaction

    trip_id = trip_id_from(data)
    if trip_id is None:
        raise Refusal('TRIP_REQUIRED', 'Say which trip you are paying for', 'trip_id', 400)

    client_amount = data.get('amount')
    client_minor = None
    if client_amount not in (None, ''):
        try:
            client_value = parse_input(client_amount)
        except (ValueError, TypeError):
            raise Refusal('INVALID_AMOUNT', 'Invalid amount provided', 'amount', 400)

    with transaction.atomic():
        # Locked first: the driver's cash confirmation locks the same row, so the two serialise.
        trip = (Trip.objects.select_for_update(of=('self',)).select_related('status_id')
                .filter(id=trip_id, user_id=user).first())
        if trip is None:
            # Someone else's trip reads exactly like a missing one.
            raise Refusal('NOT_FOUND', 'Trip not found', 'trip_id', 404)
        if not trip.status_id or trip.status_id.status_code != 'completed':
            raise Refusal('INVALID_STATE', 'A trip can be paid once it is completed',
                          'trip_id', 409)

        key = FARE_KEY.format(trip.id)
        replay = _replay(user, trip, key)
        if replay is not None:
            return replay, False

        payments = list(Payment.objects.select_for_update().filter(trip_id=trip)
                        .order_by('-created_at', '-id'))
        if any(p.status == 'completed' for p in payments) or trip.payment_status == 'completed':
            raise Refusal('ALREADY_PAID', 'This trip has already been paid', 'trip_id', 409)
        if is_fully_covered(trip):
            raise Refusal('NOT_CHARGED', 'A promotion covered this trip, so you were not charged.',
                          'trip_id', 409)

        currency = currency_for_trip(trip)
        amount_minor = payable_minor(trip)
        if amount_minor <= 0:
            raise Refusal('NOTHING_TO_PAY', 'There is no fare to pay on this trip', 'trip_id', 409)
        amount = from_minor(amount_minor, currency)
        if client_amount not in (None, ''):
            client_minor = to_minor(client_value, currency)
            if client_minor != amount_minor:
                raise Refusal('AMOUNT_MISMATCH', 'The amount does not match this trip',
                              'amount', 409, amount=str(amount), currency=currency)

        wallet = Wallet.objects.select_for_update().filter(
            user_id=user, scope=Wallet.SCOPE_RIDER).first()
        if wallet is None:
            raise Refusal('WALLET_NOT_FOUND', 'Wallet not found', 'wallet', 404)
        if wallet.currency and wallet.currency != currency:
            raise Refusal('CURRENCY_MISMATCH', 'Your wallet is in a different currency',
                          'wallet', 409)
        # A reward pending review (AUDIT 27.56) is in the wallet but not spendable.
        if wallet.balance_minor - held_minor(user, currency) < amount_minor:
            raise Refusal('INSUFFICIENT_BALANCE', 'Insufficient wallet balance', 'wallet', 400,
                          amount=str(amount), currency=currency)

        wallet.balance_minor = wallet.balance_minor - amount_minor
        wallet.save()
        txn = WalletTransaction.objects.create(
            user_id=user, currency=currency, amount_minor=amount_minor, txn_type='debit',
            status='completed', purpose='Trip payment', reference_id=f'TRIP_{trip.id}',
            idempotency_key=key)

        new_payment = Payment.objects.create(
            trip_id=trip, user_id=user, currency=currency, amount_minor=amount_minor,
            method='wallet', status='completed')
        for old in payments:
            if old.status in ('pending', 'processing'):
                old.status = 'failed'
                old.switch_reason = 'user_requested'
                old.save(update_fields=['status', 'switch_reason'])
        if payments and payments[0].replaced_by_id is None:
            payments[0].replaced_by = new_payment
            payments[0].save(update_fields=['replaced_by'])

        settled_as_cash = (TripSettlement.objects.filter(trip=trip).order_by('-version')
                           .values_list('payment_method', flat=True).first())
        trip.payment_method = 'wallet'
        trip.payment_status = 'completed'
        trip.save(update_fields=['payment_method', 'payment_status'])
        if settled_as_cash is None:
            settle_or_defer(trip)
        elif settled_as_cash == 'cash':
            credit_fare_collected_by_platform(trip, amount_minor, currency)

    return {'transaction_id': txn.id, 'trip_id': trip.id, 'amount': str(amount),
            'currency': currency, 'new_balance': str(wallet.balance), 'purpose': 'Trip payment',
            'reference_id': f'TRIP_{trip.id}', 'idempotency_key': key, 'duplicate': False,
            'message': 'Payment successful'}, True
