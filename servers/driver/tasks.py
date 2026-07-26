"""Celery tasks for the driver lifecycle.

The expiry sweeper enforces the MVA Aggregator Rules 2020 requirement
that drivers operate only with current credentials. It runs daily and
flips affected drivers to status='blocked' so they can no longer accept
rides via the consumer (which re-checks the field inside the accept
transaction).

Scope expanded in the Phase-0 hardening batch to also cover:
  - vehicle insurance expiry
  - vehicle permit expiry
  - vehicle fitness certificate expiry
  - vehicle pollution-under-control (PUC) expiry
on the driver's ACTIVE vehicle. Any single expired credential blocks
the driver until they re-upload current documents.
"""

import logging

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name='driver.block_expired_driver_licenses')
def block_expired_driver_licenses():
    """Block drivers whose driving licence OR active-vehicle credentials
    have expired.

    Task name is preserved for the existing Celery beat schedule even
    though scope is now broader than just the licence.
    """
    from servers.driver.models import Driver

    today = timezone.localdate()

    # Drivers whose own licence is past expiry.
    license_expired = Q(
        license_expiry__isnull=False,
        license_expiry__lt=today,
    )
    # Drivers whose ACTIVE vehicle has at least one expired credential.
    vehicle_expired = (
        Q(active_vehicle__insurance_expiry__lt=today)
        | Q(active_vehicle__permit_expiry__lt=today)
        | Q(active_vehicle__fitness_expiry__lt=today)
        | Q(active_vehicle__puc_expiry__lt=today)
    )

    expired = (
        Driver.objects
        .filter(license_expired | vehicle_expired)
        .exclude(status='blocked')
        .distinct()
    )

    count = expired.count()
    if count == 0:
        logger.info("driver.block_expired_driver_licenses: nothing to do")
        return {'blocked': 0}

    blocked_ids = list(expired.values_list('id', flat=True))
    # update() can't be combined with distinct(); re-filter on ids.
    Driver.objects.filter(id__in=blocked_ids).update(status='blocked')

    logger.warning(
        f"driver.block_expired_driver_licenses: blocked {count} drivers "
        f"with expired licence or vehicle credentials: {blocked_ids}"
    )

    # Notify each blocked driver so they know why they can no longer go
    # online. Push goes via the Celery FCM task; no inline network I/O.
    try:
        from servers.auth_user.services import send_push_notification
        for driver in (
            Driver.objects.filter(id__in=blocked_ids)
            .select_related('user_id', 'active_vehicle')
        ):
            reasons = []
            if driver.license_expiry and driver.license_expiry < today:
                reasons.append("driving licence")
            v = driver.active_vehicle
            if v:
                if v.insurance_expiry and v.insurance_expiry < today:
                    reasons.append("vehicle insurance")
                if v.permit_expiry and v.permit_expiry < today:
                    reasons.append("vehicle permit")
                if v.fitness_expiry and v.fitness_expiry < today:
                    reasons.append("vehicle fitness certificate")
                if v.puc_expiry and v.puc_expiry < today:
                    reasons.append("vehicle PUC")
            label = ", ".join(reasons) or "credentials"
            send_push_notification(
                driver.user_id,
                f"{label.capitalize()} expired",
                f"Your {label} on file has expired. Update it in the app "
                f"to resume accepting rides.",
                {"type": "credentials_expired", "driver_id": str(driver.id)},
            )
    except Exception as e:
        logger.error(f"Failed to dispatch expiry notifications: {e}")

    return {'blocked': count, 'driver_ids': blocked_ids}


@shared_task(name='driver.sweep_stale_driver_presence')
def sweep_stale_driver_presence():
    """Evict drivers whose presence heartbeat has expired from the geo index.

    A driver's app being force-killed, the phone losing signal, or a Daphne
    worker crashing all skip the WebSocket `disconnect()` handler, so the
    driver stayed matchable forever. Riders then wait out the whole accept
    timeout on a driver who is not there.

    Runs every 60s on Celery beat; the heartbeat TTL is 45s.
    """
    from servers.redis_client import sweep_stale_drivers

    evicted = sweep_stale_drivers()
    if evicted:
        logger.info('Presence sweep evicted %s stale driver(s)', evicted)
    return {'evicted': evicted}
