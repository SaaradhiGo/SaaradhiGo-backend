import redis
import logging
from django.conf import settings
from base.utils import success_response, error_response
from rest_framework import status

logger = logging.getLogger(__name__)

# Initialize Redis client with connection pool and error handling
redis_client = None
try:
    if getattr(settings, 'TESTING', False):
        redis_client = None
    else:
        redis_client = redis.Redis.from_url(
            settings.REDIS_URL + '/3',
            decode_responses=True,
            socket_connect_timeout=2,
            socket_keepalive=True,
            socket_keepalive_options={} if hasattr(redis, 'SOCKET_KEEPALIVE_OPTIONS') else None
        )
        # Test the connection
        redis_client.ping()
        logger.info("Stream connection established successfully")
except (redis.ConnectionError, redis.TimeoutError, Exception) as e:
    logger.error(f"Failed to connect to Redis: {str(e)}")
    redis_client = None

def update_driver_location(driver_id, lng, lat):
    if redis_client is None:
        return error_response(
            code="REDIS_CONNECTION_ERROR",
            message="Failed to connect to Redis",
            field="redis",
            issue="Redis connection error",
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    redis_client.xadd('driver_location_stream', {'driver_id': driver_id, 'lng': lng, 'lat': lat}, maxlen=100000)

    return success_response(
        data={
            'message': "Driver location updated successfully",
            'driver_id': driver_id,
            'lng': lng,
            'lat': lat
        },
        status_code=status.HTTP_200_OK
    )

#: Wallet-ledger purposes of the US pass-through lines on a CASH trip (the driver holds the rider's
#: tax and fee in hand and owes them to SaaradhiGo, which remits them). Debits, never earnings.
PURPOSE_TAX_COLLECTED = 'tax_collected'
PURPOSE_FEE_COLLECTED = 'regulatory_fee_collected'
#: A cash trip the rider then paid through SaaradhiGo (from the wallet): the platform holds the money
#: the cash settlement assumed the driver had. A credit of exactly what the rider was charged.
PURPOSE_FARE_COLLECTED_BY_PLATFORM = 'fare_collected_by_platform'


def credit_fare_collected_by_platform(trip, amount_minor, currency):
    """Credit the driver the fare the platform collected on a trip settled as cash.

    The cash settlement debited the commission (and, in the US, the tax and fee the driver was
    assumed to hold) and, with a promo, credited the discount. Adding what the rider actually paid
    the platform leaves the balance at the trip's net, as on an online trip. Append-only and keyed
    per trip, so a second call writes nothing. Call inside the payment's transaction.
    """
    from django.db import IntegrityError, transaction

    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    if not trip.driver_id or amount_minor <= 0:
        return None
    wallet = get_wallet(trip.driver_id.user_id, Wallet.SCOPE_DRIVER, lock=True)
    try:
        with transaction.atomic():
            txn = WalletTransaction.objects.create(
                user_id=trip.driver_id.user_id, currency=currency, amount_minor=amount_minor,
                txn_type='credit', status='completed', purpose=PURPOSE_FARE_COLLECTED_BY_PLATFORM,
                reference_id=f'TRIP_{trip.id}', idempotency_key=f'TRIP_{trip.id}_COLLECTED')
    except IntegrityError:
        return None
    wallet.balance_minor = wallet.balance_minor + amount_minor
    wallet.save(update_fields=['balance_minor'])
    return txn


def settle_paid_by_platform(trip, amount_minor, currency=None):
    """Settle a completed trip the platform has just been PAID for (online, or from the wallet).

    Every path that completes a platform payment calls this, so the driver ends credited exactly
    once with the trip's net, whatever happened before (AUDIT 27.72):

    * not settled yet: the normal settlement (`credit_driver_wallet`), as an online trip;
    * already settled as a platform-collected trip: nothing more (a repeat webhook, a poll);
    * settled as CASH at completion, then switched and paid online or from the wallet: the driver
      did not collect the cash, so they are credited exactly what the platform collected
      (`credit_fare_collected_by_platform`), which leaves the balance at the net, and the
      settlement gets a version 2 (append only) with the same amounts and the new payment method,
      so reports read the trip as paid through the platform.

    Call with `trip.payment_method` already set to the method that paid. Inside a transaction.
    """
    from django.db import transaction

    from servers.payments.models import TripSettlement
    from servers.rider.models import Wallet, get_wallet

    # Read the settlement under the driver-wallet lock every settlement takes, so a cash completion
    # settling at the same moment is either committed (seen) or not started (review MEDIUM-2).
    if trip.driver_id:
        get_wallet(trip.driver_id.user_id, Wallet.SCOPE_DRIVER, lock=True)
    current = TripSettlement.objects.filter(trip=trip).order_by('-version').first()
    if current is None:
        # No commission rule must not undo the rider's payment (review MEDIUM-3): settle or defer.
        settle_or_defer(trip)
        return None
    if current.payment_method != 'cash':
        return None
    with transaction.atomic():
        txn = credit_fare_collected_by_platform(trip, amount_minor, currency or current.currency)
        if txn is None:
            return None
        TripSettlement.objects.create(
            trip=trip, driver=current.driver, wallet_transaction=txn, currency=current.currency,
            gross_fare_minor=current.gross_fare_minor,
            commission_percent=current.commission_percent,
            commission_amount_minor=current.commission_amount_minor,
            driver_net_minor=current.driver_net_minor,
            tax_collected_minor=current.tax_collected_minor,
            regulatory_fee_collected_minor=current.regulatory_fee_collected_minor,
            payment_method=trip.payment_method or 'online', settled_at=current.settled_at,
            version=current.version + 1, source=current.source,
            provenance_note='Paid through SaaradhiGo after a cash settlement (AUDIT 27.72)')
    return txn


def settle_or_defer(trip):
    """Settle a completed trip, but never let a missing commission rule strand the ride.

    `credit_driver_wallet` raises `CommissionNotConfigured` when no rule governs the trip
    (module 8: there is no default rate). Completing a trip, and telling the rider and
    driver it is done, must not depend on that: the failure is logged loudly and the trip
    stays completed but UNSETTLED. Nothing is written, so settling it later (once a rule
    exists) is the normal idempotent call, and the daily reconciliation's trips-vs-
    settlements check lists it. Returns True when settled, False when deferred.
    """
    from servers.pricing.commission import CommissionNotConfigured

    try:
        credit_driver_wallet(trip)
        return True
    except CommissionNotConfigured as exc:
        logger.error('settlement_deferred_no_commission_rule', extra={
            'event': 'settlement_deferred_no_commission_rule', 'trip_id': trip.id,
            'detail': str(exc)})
        # Logging is not enough: raise ONE critical Command Centre alert for finance
        # (idempotent per trip). It must never fail the completion it is reporting on.
        try:
            from servers.payments.unsettled import raise_unsettled_alert
            raise_unsettled_alert(trip, str(exc))
        except Exception:  # noqa: BLE001
            logger.exception('could not raise the unsettled-trip alert for trip %s', trip.id)
        return False


def credit_driver_wallet(trip):
    """Settle a completed trip against the driver's settlement balance."""
    from servers.payments.models import TransactionHistory, TripSettlement
    from servers.rider.models import Wallet, WalletTransaction, get_wallet
    from django.utils import timezone
    from servers.pricing.services import commission_percent_for_trip
    from decimal import Decimal
    from django.db import transaction, IntegrityError
    from base.money import currency_for_trip, from_minor, to_minor

    if not trip.driver_id:
        return

    # The ledger is integers in the trip's currency. The one rounding is the
    # commission, done by the money helper; the driver's net is then an exact
    # subtraction, so gross = commission + net holds to the paisa by
    # construction rather than by luck.
    currency = currency_for_trip(trip)
    gross_minor = trip.final_fare_minor or trip.estimated_fare_minor or 0

    # US (additive-tax) trips: sales tax and the per-trip regulatory fee are PASS-THROUGH (owner
    # ruling, AUDIT 27.62 item 1). They are collected from the rider and remitted by SaaradhiGo,
    # never part of the driver's earnings, and commission is worked out on the fare BEFORE tax and
    # fees. So the settlement's gross is the pre-tax fare and the tax and fee are recorded beside it.
    # India (inclusive tax) and a zone with nothing to split: `passthrough` is None and every line
    # below is exactly what it was. US accountant to confirm (go-live blocker #15).
    from servers.pricing.promo_tax import settlement_split

    passthrough = settlement_split(trip, gross_minor, currency)
    tax_minor = fee_minor = None
    if passthrough is not None:
        gross_minor, tax_minor, fee_minor = (
            passthrough['pretax'], passthrough['tax'], passthrough['fee'])

    # Commission comes from the RateCard in force for the trip's zone, not
    # from a global env var. PLATFORM_COMMISSION_PERCENT defaulted to "0",
    # so an unset env var meant the platform took nothing on every ride,
    # and the per-city commission on each RateCard was never applied.
    commission_rate = commission_percent_for_trip(trip)

    commission_minor = to_minor(
        from_minor(gross_minor, currency) * commission_rate / Decimal('100'), currency)
    net_minor = gross_minor - commission_minor

    with transaction.atomic():
        # Driver settlement balance — a DIFFERENT row from the rider credit
        # wallet, so a driver who also rides cannot spend settlement money
        # through the rider payment path.
        wallet = get_wallet(trip.driver_id.user_id, Wallet.SCOPE_DRIVER, lock=True)

        # Handle cash vs non-cash trips
        if trip.payment_method == 'cash':
            # Driver collected full fare in cash, so they owe the platform the commission
            txn_minor = commission_minor
            txn_type = 'debit'
            purpose = 'trip_commission'
            new_balance_minor = wallet.balance_minor - commission_minor
            # The driver also holds the tax and fee the rider handed over: it is not theirs, so
            # the wallet owes it to SaaradhiGo as two more debits, each its own labelled line
            # (written below, once the commission line has gone in).
            new_balance_minor -= (tax_minor or 0) + (fee_minor or 0)
        else:
            # Online/Wallet payment, platform owes driver the net amount
            txn_minor = net_minor
            txn_type = 'credit'
            purpose = 'trip_earnings'
            new_balance_minor = wallet.balance_minor + net_minor

        # Idempotency block
        wallet_txn = None
        try:
            with transaction.atomic():
                wallet_txn = WalletTransaction.objects.create(
                    user_id=trip.driver_id.user_id,
                    currency=currency,
                    amount_minor=txn_minor,
                    txn_type=txn_type,
                    status='completed',
                    purpose=purpose,
                    reference_id=f'TRIP_{trip.id}',
                    idempotency_key=f'TRIP_{trip.id}_EARNING'
                )
        except IntegrityError:
            # Duplicate webhook/retry: already processed. The idempotency key
            # TRIP_<id>_EARNING is what refuses it, and returning here is what
            # stops the balance being credited twice -- so this branch working
            # is load-bearing for the ledger.
            #
            # Logged because a rising rate here means something upstream is
            # retrying settlement more than expected, and that is worth knowing
            # before it turns into a support question about a missing payment.
            logger.info('driver_earning_duplicate_suppressed', extra={
                'event': 'driver_earning_duplicate_suppressed',
                'trip_id': trip.id,
                'idempotency_key': f'TRIP_{trip.id}_EARNING',
            })
            return

        wallet.balance_minor = new_balance_minor
        wallet.save(update_fields=['balance_minor'])

        if trip.payment_method == 'cash':
            for amount, purpose_, key in ((tax_minor, PURPOSE_TAX_COLLECTED, 'TAX'),
                                          (fee_minor, PURPOSE_FEE_COLLECTED, 'FEE')):
                if amount:
                    WalletTransaction.objects.create(
                        user_id=trip.driver_id.user_id, currency=currency, amount_minor=amount,
                        txn_type='debit', status='completed', purpose=purpose_,
                        reference_id=f'TRIP_{trip.id}', idempotency_key=f'TRIP_{trip.id}_{key}')

        # Create transaction history
        TransactionHistory.objects.create(
            trip_id=trip,
            user_id=trip.user_id,
            driver_id=trip.driver_id,
            currency=currency,
            amount_minor=txn_minor,
            method=trip.payment_method or 'online',
            status='completed',
            txn_type=txn_type,
            user_name=trip.user_id.full_name or trip.user_id.phone_number,
        )

        # Immutable economic evidence for this trip.
        #
        # Inside the SAME transaction as the ledger row it explains, so the two
        # cannot disagree after a crash -- a settlement describing money that never
        # moved would be worse than no settlement at all.
        #
        # It needs no idempotency logic of its own. The `TRIP_<id>_EARNING` key on
        # WalletTransaction above is the guard: a second call returns at the
        # IntegrityError before reaching this line, so this runs exactly once for
        # exactly the reason the ledger row does. The partial unique index on
        # (trip) where version=1 is the database's independent backstop.
        #
        # Amounts are copied, not referenced: `commission_percent` is the rate AS
        # APPLIED, so editing a rate card later cannot rewrite what this ride earned.
        TripSettlement.objects.create(
            trip=trip,
            driver=trip.driver_id,
            wallet_transaction=wallet_txn,
            currency=currency,
            gross_fare_minor=gross_minor,
            commission_percent=commission_rate,
            commission_amount_minor=commission_minor,
            driver_net_minor=net_minor,
            # Pass-through amounts owed by the platform (None = nothing passed through: India, and
            # every settlement written before this existed, which read exactly as they always did).
            tax_collected_minor=tax_minor,
            regulatory_fee_collected_minor=fee_minor,
            payment_method=trip.payment_method or 'online',
            settled_at=timezone.now(),
            source=TripSettlement.SOURCE_NATIVE,
        )

        # A rider promo is PLATFORM-FUNDED (owner ruling, AUDIT 27.51): everything above was
        # computed on the FULL fare, so the driver's earnings and commission are what they would
        # be without the promo. This only counts the discount against its campaign's budget,
        # writes the platform-funded ledger entry and, on a cash trip, pays the driver the
        # discount they did not collect. In its own savepoint: a promotions defect must never
        # undo a settlement.
        try:
            with transaction.atomic():
                from servers.promotions.rider_promos import settle_trip as _settle_trip_promo
                _settle_trip_promo(trip, wallet, is_cash=(trip.payment_method == 'cash'))
        except Exception:  # noqa: BLE001
            logger.exception('promo_settlement_failed', extra={
                'event': 'promo_settlement_failed', 'trip_id': trip.id})