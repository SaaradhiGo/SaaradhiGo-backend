"""How long a driver's documents may wait for review (12 Nov, console QA).

Per city: `ServiceZone.kyc_review_target_days` (working days, Monday to Friday, in the city's
time zone), else the parent zone's, else KYC_REVIEW_TARGET_WORKING_DAYS (1). The KYC queue marks a
driver past it, `sweep` raises one Command Centre alert per city while any are, and the driver's
"We usually review within ..." text reads the same number, so the promise and the target agree.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def _zone_for(node):
    from servers.pricing.models import ServiceZone

    seen = 0
    while node is not None and seen < 8:
        zone = ServiceZone.objects.filter(geo_node=node).order_by('id').first()
        if zone is not None:
            return zone
        node, seen = node.parent, seen + 1
    return None


def target_days(node):
    zone = _zone_for(node)
    seen = 0
    while zone is not None and seen < 8:
        if zone.kyc_review_target_days:
            return int(zone.kyc_review_target_days)
        zone, seen = zone.parent, seen + 1
    return int(getattr(settings, 'KYC_REVIEW_TARGET_WORKING_DAYS', 1))


def add_working_days(start, days, tz):
    """`start` plus `days` working days (Saturday and Sunday skipped), same local clock time."""
    local = timezone.localtime(start, tz)
    left = days
    while left > 0:
        local += timedelta(days=1)
        if local.weekday() < 5:
            left -= 1
    return local


def due(driver, since=None):
    """(due_at, target_days) for this driver's review, or (None, days) with no start time."""
    from .admin_drivers import city_tz

    node = getattr(driver, 'home_city', None)
    days = target_days(node)
    since = since or getattr(driver, 'waiting_since', None) or driver.uploaded_timestamp
    if since is None:
        return None, days
    return add_working_days(since, days, city_tz(node)), days


def sweep(now=None):
    """Raise one alert per city with drivers past the review target; clear it when none are."""
    from servers.admin_access.models import GeoNode
    from servers.command_centre.models import KYC_REVIEW_PAST_TARGET, OperatorAlert, WARNING
    from servers.command_centre.services import acknowledge, raise_alert

    from .admin_drivers import active_suspension_q, annotate, kyc_state_q
    from .models import Driver

    now = now or timezone.now()
    waiting = (annotate(Driver.objects.select_related('home_city', 'home_city__parent'))
               .filter(kyc_state_q('in_review')).exclude(active_suspension_q()))
    over = {}
    for driver in waiting:
        at, _ = due(driver)
        if at is not None and at < now:
            key = driver.home_city_id or 0
            over[key] = over.get(key, 0) + 1
    for node_id, count in over.items():
        node = GeoNode.objects.filter(pk=node_id).first() if node_id else None
        source_id = f'kyc:{node_id}'
        open_alert = OperatorAlert.objects.filter(source_type='kyc_review_target', source_id=source_id,
                                                  acknowledged_at__isnull=True).first()
        if open_alert is not None:
            if (open_alert.payload or {}).get('past_target') != count:
                open_alert.payload = {**(open_alert.payload or {}), 'past_target': count}
                open_alert.save(update_fields=['payload'])
            continue
        where = f' in {node.name}' if node is not None else ''
        raise_alert(KYC_REVIEW_PAST_TARGET, severity=WARNING,
                    title=f'{count} driver{"s" if count != 1 else ""} waiting past the review target{where}',
                    payload={'past_target': count, 'path': '/kyc?past_target=1'},
                    source_type='kyc_review_target', source_id=source_id, city=node)
    for alert in OperatorAlert.objects.filter(source_type='kyc_review_target', acknowledged_at__isnull=True):
        node_id = int(alert.source_id.split(':')[1])
        if node_id not in over:
            acknowledge(alert, by=None, note='Cleared: every driver is within the review target.')
    return sum(over.values())
