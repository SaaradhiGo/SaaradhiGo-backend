"""payment_abuse: a rider whose payments keep failing, being refunded or going unpaid.

Per rider, in the window: online payments that failed, payments that were refunded, and completed
cash trips whose payment never completed. Any one at or over its line is a hit; two or more
signals, or a very high count, raise the severity. It reads payment rows and trips and nothing
else: it collects nothing and blocks nothing.
"""

from collections import defaultdict

from django.db.models import Count

from servers.fraud.models import HIGH

from .base import Hit, Param, Rule, clamp_score, completed_status_id, severity_for, window_start

MAX_ROWS = 1000
UNPAID = ('pending', 'processing', 'failed')


def evaluate(th, country, now):
    from django.contrib.auth import get_user_model

    from servers.payments.models import Payment
    from servers.ride.models import Trip

    since = window_start(now, days=th['window_days'])
    found = defaultdict(lambda: {'signals': {}, 'trips': set()})
    pays = Payment.objects.filter(created_at__gte=since, trip_id__zone__country=country)

    for status, key, line in (('failed', 'failed_online_payments', th['min_failed']),
                              ('refunded', 'refunds', th['min_refunds'])):
        qs = pays.filter(status=status)
        if status == 'failed':
            qs = qs.filter(method='online')
        for row in qs.values('user_id').annotate(n=Count('id')).filter(n__gte=line)[:MAX_ROWS]:
            found[row['user_id']]['signals'][key] = row['n']
    done = completed_status_id()
    if done is not None:
        for row in (Trip.objects.filter(status_id=done, completed_at__gte=since,
                                        zone__country=country, payment_method='cash',
                                        payment_status__in=UNPAID)
                    .values('user_id').annotate(n=Count('id'))
                    .filter(n__gte=th['min_unpaid_cash'])[:MAX_ROWS]):
            found[row['user_id']]['signals']['unpaid_cash_trips'] = row['n']

    User = get_user_model()
    lines = {'failed_online_payments': th['min_failed'], 'refunds': th['min_refunds'],
             'unpaid_cash_trips': th['min_unpaid_cash']}
    hits = []
    for user_id, entry in found.items():
        user = User.objects.filter(pk=user_id).first()
        if user is None:
            continue
        trip_ids = set(Payment.objects.filter(user_id=user_id, created_at__gte=since)
                       .exclude(status='completed').values_list('trip_id_id', flat=True)[:50])
        trip_ids |= set(Trip.objects.filter(user_id=user_id, completed_at__gte=since,
                                            payment_method='cash', payment_status__in=UNPAID)
                        .values_list('pk', flat=True)[:50])
        over = {k: v / lines[k] for k, v in entry['signals'].items()}
        score = clamp_score(35 + 10 * (len(over) - 1) + 8 * (max(over.values()) - 1))
        severity = HIGH if len(over) >= 2 or max(over.values()) >= 3 else severity_for(score)
        hits.append(Hit(
            subject=user, severity=severity, score=score, signals=sorted(entry['signals']),
            refs={'trip_ids': sorted(trip_ids)[:50]}, measures=dict(entry['signals'])))
    return hits


RULE = Rule(
    key='payment_abuse', title='Payment abuse', subject_role='rider',
    summary='A rider has repeated failed online payments, refunds or unpaid cash trips.',
    data_used='Payment rows (status, method) and completed trips with a cash payment that never '
              'completed, in the window.',
    data_missing='No card or UPI fingerprint is stored (the gateway response carries none), so '
                 'several accounts failing on one instrument cannot be tied together. A failed '
                 'payment is not separated from a rider\'s bank declining it.',
    params=(
        Param('window_days', 'Look back', 'days', 1, 90, 14, 14),
        Param('min_failed', 'Failed online payments', 'payments', 2, 100, 5, 5),
        Param('min_refunds', 'Refunded payments', 'payments', 2, 100, 4, 4),
        Param('min_unpaid_cash', 'Completed cash trips left unpaid', 'trips', 1, 100, 3, 3),
    ),
    evaluate=evaluate)
