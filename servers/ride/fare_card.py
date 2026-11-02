"""The admin trip page's fare card: who paid what, in one place (owner ruling, AUDIT 27.74).

The page showed "Charged ₹159.72" on a promo trip where the rider actually paid ₹139.72: the
platform-funded discount was in one card and the rider's payment in another. The owner asked for one
line that reads as the money moved:

    Fare ₹159.72 · Promo −₹20.00 (paid by SaaradhiGo) · Rider paid ₹139.72

and, on a cash trip, what the driver collected; with the same treatment for referral rewards and for
trips an operator ended early. Everything here is read from the records that already exist (the
settlement inputs, the payments, the refunds, the promo and referral ledgers, the operator-end
snapshot); nothing is recomputed from today's rates. Amounts are minor units in `currency`.
"""

from django.db.models import Sum

PLATFORM = 'SaaradhiGo'


def _line(kind, label, amount_minor, **extra):
    return {'kind': kind, 'label': label, 'amount_minor': int(amount_minor), **extra}


def fare_card(trip):
    from servers.payments.models import Payment
    from servers.promotions import rider_promos
    from servers.promotions.models import REWARDED, Referral, ReferralTrip
    from servers.ride.models import FarePricing, TripMoneyAction

    currency = trip.currency
    full = rider_promos.fare_minor(trip)
    promo = rider_promos.discount_minor(trip) or 0
    payable = rider_promos.payable_minor(trip)
    payment = (Payment.objects.filter(trip_id=trip, status__in=('completed', 'refunded'))
               .order_by('-created_at', '-id').first())
    method = (payment.method if payment is not None else trip.payment_method) or ''
    if payment is not None:
        rider_paid = payment.amount_minor
    elif (trip.payment_status or '') in ('completed', 'paid'):
        rider_paid = payable
    else:
        rider_paid = None
    refunded = int(TripMoneyAction.objects.filter(
        trip=trip, kind=TripMoneyAction.REFUND, currency=currency,
        status__in=(TripMoneyAction.COMPLETED, TripMoneyAction.PENDING_GATEWAY),
    ).aggregate(total=Sum('amount_minor'))['total'] or 0)
    if payment is not None and payment.status == 'refunded':
        refunded = max(refunded, payment.amount_minor)

    lines = [_line('fare', 'Fare', full)]
    if promo:
        lines.append(_line('promo', 'Promo', -promo, funded_by=PLATFORM,
                           note=f'paid by {PLATFORM}'))
    if rider_paid is not None:
        lines.append(_line('rider_paid', 'Rider paid', rider_paid, method=method))
    else:
        lines.append(_line('rider_owes', 'Rider to pay', payable, method=method))
    if refunded:
        lines.append(_line('refunded', 'Refunded to the rider', -refunded))

    # Cash: what the driver holds (or is still to collect). The platform pays them any promo.
    cash = None
    if method == 'cash' or (payment is None and trip.payment_method == 'cash'):
        collected = payment is not None and payment.status == 'completed'
        cash = {'collected': collected, 'amount_minor': payable,
                'label': 'Driver collected in cash' if collected else 'Driver to collect in cash'}
        if promo:
            cash['platform_pays_driver_minor'] = promo

    # Referral rewards this trip earned (it was the trip that completed the requirement).
    referrals = []
    for referral in (Referral.objects.filter(status=REWARDED, currency=currency,
                                             counted_trips__trip=trip).distinct()):
        last = (ReferralTrip.objects.filter(referral=referral).order_by('-created_at', '-id')
                .values_list('trip_id', flat=True).first())
        if last != trip.id:
            continue
        if referral.referee_reward_minor:
            referrals.append(_line('referral_reward', 'Referral reward to the rider',
                                   referral.referee_reward_minor, to='rider', funded_by=PLATFORM,
                                   note=f'paid by {PLATFORM}'))
        if referral.referrer_reward_minor:
            referrals.append(_line('referral_reward', 'Referral reward to the referrer',
                                   referral.referrer_reward_minor, to='referrer',
                                   funded_by=PLATFORM, note=f'paid by {PLATFORM}'))

    # An operator ended the trip early: the fare is for the distance driven.
    early_end = None
    if trip.end_rule:
        snapshot = (FarePricing.objects.filter(trip_id=trip,
                                               snapshot_type=FarePricing.SNAPSHOT_OPERATOR_END)
                    .order_by('-id').first())
        early_end = {
            'rule': trip.end_rule, 'reason_code': trip.end_reason_code or None,
            'fare_basis': 'the distance driven',
            'min_fare_applied': bool(getattr(snapshot, 'min_fare_applied', False)),
            'distance_km': (str(snapshot.actual_distance_km)
                            if snapshot is not None and snapshot.actual_distance_km is not None
                            else None),
            'platform_paid_driver_minor': trip.platform_funded_minor,
            'funded_by': PLATFORM if trip.end_rule == 'platform_funded' else None,
        }

    summary = [line for line in lines if line['kind'] in ('fare', 'promo', 'rider_paid')]
    return {'currency': currency, 'lines': lines, 'summary': summary, 'cash': cash,
            'referral_rewards': referrals, 'early_end': early_end}
