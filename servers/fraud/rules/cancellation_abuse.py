"""cancellation_abuse: drivers who accept and then cancel, riders who cancel and get the fee undone.

* Driver: of the trips the driver accepted in the window, the share the DRIVER then cancelled. A
  driver with `min_accepted` or more accepted trips and a cancel share of `driver_cancel_pct` or
  more is a hit.
* Rider: trips the rider cancelled after a driver had accepted, in the window. A rider with
  `min_rider_cancels` or more such trips, of which `min_refunded` or more carry a refund recorded
  on the trip (`TripMoneyAction`), is a hit: the cancel-and-get-refunded loop.

Reads only; the fee and the refund stay exactly as the cancellation policy and Support left them.
"""

from django.db.models import Count, Exists, OuterRef, Q

from servers.fraud.models import HIGH

from .base import Hit, Param, Rule, clamp_score, severity_for, window_start

MAX_ROWS = 500
MAX_TRIP_REFS = 50


def _driver_hits(th, country, since):
    from servers.driver.models import Driver
    from servers.ride.models import Trip

    rows = (Trip.objects.filter(accepted_at__gte=since, zone__country=country,
                                driver_id__isnull=False)
            .values('driver_id')
            .annotate(accepted=Count('id'), cancelled=Count('id', filter=Q(cancelled_by='driver')))
            .filter(accepted__gte=th['min_accepted']).order_by('-cancelled')[:MAX_ROWS])
    hits = []
    for row in rows:
        pct = row['cancelled'] * 100 / row['accepted']
        if pct < th['driver_cancel_pct']:
            continue
        driver = Driver.objects.filter(pk=row['driver_id']).select_related('user_id').first()
        if driver is None:
            continue
        trips = list(Trip.objects.filter(driver_id=row['driver_id'], cancelled_by='driver',
                                         accepted_at__gte=since)
                     .order_by('-id').values_list('pk', flat=True)[:MAX_TRIP_REFS])
        score = clamp_score(35 + (pct - th['driver_cancel_pct']) + min(row['cancelled'], 20))
        hits.append(Hit(
            subject=driver.user_id, severity=HIGH if pct >= 70 else severity_for(score),
            score=score, signals=['driver_cancels_after_accept'], refs={'trip_ids': sorted(trips)},
            measures={'accepted': row['accepted'], 'cancelled': row['cancelled'],
                      'cancel_pct': round(pct)}))
    return hits


def _rider_hits(th, country, since):
    from django.contrib.auth import get_user_model

    from servers.ride.models import Trip, TripMoneyAction

    refunded = Exists(TripMoneyAction.objects.filter(trip=OuterRef('pk'), kind='refund'))
    rows = (Trip.objects.filter(cancelled_by='rider', accepted_at__gte=since,
                                zone__country=country, driver_id__isnull=False)
            .values('user_id')
            .annotate(cancels=Count('id'), refunded=Count('id', filter=refunded))
            .filter(cancels__gte=th['min_rider_cancels'], refunded__gte=th['min_refunded'])
            .order_by('-refunded')[:MAX_ROWS])
    User = get_user_model()
    hits = []
    for row in rows:
        user = User.objects.filter(pk=row['user_id']).first()
        if user is None:
            continue
        trips = list(Trip.objects.filter(user_id=row['user_id'], cancelled_by='rider',
                                         accepted_at__gte=since, driver_id__isnull=False)
                     .order_by('-id').values_list('pk', flat=True)[:MAX_TRIP_REFS])
        score = clamp_score(35 + 8 * (row['refunded'] - th['min_refunded'])
                            + 3 * (row['cancels'] - th['min_rider_cancels']))
        hits.append(Hit(
            subject=user, severity=severity_for(score), score=score,
            signals=['rider_cancels_then_refunded'], refs={'trip_ids': sorted(trips)},
            measures={'cancelled_after_accept': row['cancels'], 'refunded': row['refunded']}))
    return hits


def evaluate(th, country, now):
    since = window_start(now, days=th['window_days'])
    return _driver_hits(th, country, since) + _rider_hits(th, country, since)


RULE = Rule(
    key='cancellation_abuse', title='Cancellation abuse',
    summary='A driver accepts and then cancels too often, or a rider cancels after a driver '
            'accepted and keeps getting the money back.',
    data_used='Trips with an accepted time, who cancelled; refunds recorded on trips.',
    data_missing='There is no explicit "fee waived" marker; a refund recorded on a rider-cancelled '
                 'trip stands in for it. The cancellation reason is free text, so a driver '
                 'cancelling because the rider asked them to is not separated out.',
    params=(
        Param('window_days', 'Look back', 'days', 1, 90, 7, 7),
        Param('min_accepted', 'Driver: accepted trips before judging', 'trips', 3, 500, 10, 10),
        Param('driver_cancel_pct', 'Driver: share cancelled by the driver', 'percent', 10, 100,
              40, 40),
        Param('min_rider_cancels', 'Rider: cancels after a driver accepted', 'trips', 2, 100, 5, 5),
        Param('min_refunded', 'Rider: of which refunded', 'trips', 1, 100, 3, 3),
    ),
    evaluate=evaluate)
