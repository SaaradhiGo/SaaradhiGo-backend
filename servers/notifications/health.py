"""Provider health: the `notify.provider_failing` Command Centre alert (warning, Super Admin).

Per (provider, channel), over the last NOTIFY_FAILURE_WINDOW_MINUTES (15): failed divided by
(sent + delivered + failed). Above NOTIFY_FAILURE_RATE_THRESHOLD, with at least
NOTIFY_FAILURE_MIN_SAMPLE messages (a single failure out of two is not an outage), one alert is
raised per (provider, channel); it clears itself when the rate falls back. The stub and the
in-app writer are never measured: they cannot fail by a provider's fault.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from servers.command_centre.models import NOTIFY_PROVIDER_FAILING, WARNING, OperatorAlert
from servers.command_centre.services import acknowledge, raise_alert

from .config import INTERNAL_PROVIDERS
from .models import DELIVERED, FAILED, SENT, NotificationMessage

logger = logging.getLogger(__name__)

SOURCE_TYPE = 'notify_provider'


def measure(now=None):
    """`[{provider, channel, total, failed, rate}]` for the window."""
    now = now or timezone.now()
    since = now - timedelta(minutes=int(getattr(settings, 'NOTIFY_FAILURE_WINDOW_MINUTES', 15)))
    rows = (NotificationMessage.objects
            .filter(created_at__gte=since, status__in=(SENT, DELIVERED, FAILED))
            .exclude(provider__in=list(INTERNAL_PROVIDERS) + [''])
            .values('provider', 'channel')
            .annotate(total=Count('id'), failed=Count('id', filter=Q(status=FAILED))))
    return [{**r, 'rate': (r['failed'] / r['total']) if r['total'] else 0.0} for r in rows]


def check(now=None):
    """Raise and clear alerts. Returns `{raised, cleared}` provider keys."""
    threshold = float(getattr(settings, 'NOTIFY_FAILURE_RATE_THRESHOLD', 0.3))
    minimum = int(getattr(settings, 'NOTIFY_FAILURE_MIN_SAMPLE', 10))
    window = int(getattr(settings, 'NOTIFY_FAILURE_WINDOW_MINUTES', 15))
    raised, cleared, failing = [], [], set()
    for row in measure(now):
        key = f"{row['provider']}:{row['channel']}"
        if row['total'] >= minimum and row['rate'] > threshold:
            failing.add(key)
            alert = raise_alert(
                NOTIFY_PROVIDER_FAILING, severity=WARNING,
                title=f"{row['provider']} {row['channel']}: {row['failed']} of {row['total']} "
                      f"messages failed in {window} minutes",
                payload={'provider': row['provider'], 'channel': row['channel'],
                         'failed': row['failed'], 'total': row['total'],
                         'rate': round(row['rate'], 3), 'threshold': threshold,
                         'window_minutes': window},
                source_type=SOURCE_TYPE, source_id=key)
            raised.append(alert.pk)
    for alert in OperatorAlert.objects.filter(
            kind=NOTIFY_PROVIDER_FAILING, acknowledged_at__isnull=True):
        if alert.source_id not in failing:
            if acknowledge(alert, by=None, note='cleared: the failure rate is back under the line'):
                cleared.append(alert.pk)
    return {'raised': raised, 'cleared': cleared}
