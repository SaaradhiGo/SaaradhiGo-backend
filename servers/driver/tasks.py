"""Celery tasks for the driver lifecycle.

The expiry sweeper enforces the MVA Aggregator Rules 2020 requirement
that drivers operate only with current credentials. It runs daily and
flips drivers with expired credentials to status='blocked' so they can
no longer accept rides via the consumer (which re-checks the field
inside the accept transaction).
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name='driver.block_expired_driver_licenses')
def block_expired_driver_licenses():
    """Block drivers whose driving licence has expired.

    Scope is intentionally narrow for Phase-0 (only license_expiry is
    captured on the Driver model). Vehicle insurance, RC, permit, and
    fitness expiry will be added once the corresponding Vehicle fields
    + admin UI are in place; this same job will gain those checks.
    """
    from servers.driver.models import Driver

    today = timezone.localdate()

    expired = Driver.objects.filter(
        license_expiry__isnull=False,
        license_expiry__lt=today,
    ).exclude(status='blocked')

    count = expired.count()
    if count == 0:
        logger.info("driver.block_expired_driver_licenses: nothing to do")
        return {'blocked': 0}

    blocked_ids = list(expired.values_list('id', flat=True))
    expired.update(status='blocked')

    logger.warning(
        f"driver.block_expired_driver_licenses: blocked {count} drivers "
        f"with expired licences: {blocked_ids}"
    )

    # Notify each blocked driver so they know why they can no longer go
    # online. Push goes via the Celery FCM task; no inline network I/O.
    try:
        from servers.auth_user.services import send_push_notification
        for driver in Driver.objects.filter(id__in=blocked_ids).select_related('user_id'):
            send_push_notification(
                driver.user_id,
                "Driving licence expired",
                "Your driving licence on file has expired. Update it in "
                "the app to resume accepting rides.",
                {"type": "license_expired", "driver_id": str(driver.id)},
            )
    except Exception as e:
        logger.error(f"Failed to dispatch expiry notifications: {e}")

    return {'blocked': count, 'driver_ids': blocked_ids}
