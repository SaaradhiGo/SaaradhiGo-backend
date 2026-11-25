"""The daily report email (owner ruling, 25 Nov): yesterday's numbers, by email, each morning.

A staff member who can read reports opts in (`DailyReportSubscription`). Each hour the beat task
looks for cities where it is now 08:00 local; for each subscriber whose scope covers some of them it
queues ONE email per country and local day (`reports.daily_summary`, email channel) with one line per
city: completed trips, cancellations, fares, refunds and SOS. It goes through the notifications
outbox, so it is really sent only once SES (the email provider) is configured and live; until then
the message is recorded and nobody receives it. Never personal data: counts and sums only.
"""

import logging
from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone

from base.money import format_money, from_minor
from base.words import plural

logger = logging.getLogger(__name__)
TEMPLATE = 'reports.daily_summary'
SEND_HOUR = 8


def city_lines(cities, day):
    from .models import DailyCityMetrics, DailySafety

    lines = []
    for city in cities:
        m = DailyCityMetrics.objects.filter(city=city, date=day)
        agg = m.aggregate(done=Sum('trips_completed'), rc=Sum('cancelled_by_rider'), dc=Sum('cancelled_by_driver'),
                          oc=Sum('cancelled_by_ops'), refunds=Sum('refunds_minor'))
        money = {}
        for row in m.values('currency').annotate(gmv=Sum('gmv_minor')):
            money[row['currency']] = row['gmv'] or 0
        fares = ', '.join(format_money(from_minor(v, c), c) for c, v in money.items()) or 'no fares'
        cancelled = (agg['rc'] or 0) + (agg['dc'] or 0) + (agg['oc'] or 0)
        sos = DailySafety.objects.filter(city=city, date=day, source='sos').aggregate(n=Sum('count'))['n'] or 0
        cur = next(iter(money), None)
        refunds = format_money(from_minor(agg['refunds'] or 0, cur), cur) if cur else 'no refunds'
        lines.append(f"{city.name}: {plural(agg['done'] or 0, 'trip')} completed, {cancelled} cancelled, "
                     f"fares {fares}, refunds {refunds}, {plural(sos, 'SOS', 'SOS')}.")
    return lines


def subscribers():
    from .models import DailyReportSubscription

    return (DailyReportSubscription.objects.filter(is_active=True, user__is_active=True, user__is_staff=True)
            .select_related('user'))


def send_due(now=None):
    """Queue today's email for every subscriber whose cities reached 08:00 local this hour."""
    from servers.admin_access.bridge import granted
    from servers.notifications.dispatcher import enqueue, resolve_template

    from .compute import active_cities, city_context

    now = now or timezone.now()
    due = {}
    for city in active_cities():
        ctx = city_context(city)
        local = now.astimezone(ctx.tz)
        if local.hour == SEND_HOUR:
            due.setdefault(city.code.split('-')[0], []).append((city, local.date() - timedelta(days=1)))
    queued = 0
    for sub in subscribers():
        for country, rows in due.items():
            mine = [(c, d) for c, d in rows if granted(sub.user, 'report.read', c)[0]]
            if not mine:
                continue
            day = mine[0][1]
            template = resolve_template(TEMPLATE, 'email', country)
            if template is None:
                logger.warning('daily report: no active template for %s', country)
                continue
            summary = '\n'.join(city_lines([c for c, _ in mine], day))
            enqueue(sub.user, template, {'date': f'{day:%d %b %Y}', 'summary': summary},
                    idempotency_key=f'daily_report:{sub.user_id}:{country}:{day.isoformat()}',
                    source=TEMPLATE, country=country)
            queued += 1
    return {'queued': queued}
