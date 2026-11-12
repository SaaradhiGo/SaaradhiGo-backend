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

    blocked_ids = list(expired.values_list('id', flat=True))
    # MVAG 2025 driver records (health and term insurance, induction training): only where the
    # market's gate is on. With every gate off (the default) this adds nothing.
    from servers.driver import records
    record_reasons = {}
    if records.any_gate_enabled():
        for driver in (Driver.objects.filter(approved=True).exclude(status='blocked')
                       .exclude(id__in=blocked_ids).select_related('user_id', 'home_city')):
            problems = records.blocking_problems(driver, today)
            if problems:
                record_reasons[driver.id] = problems
        blocked_ids += list(record_reasons)

    count = len(blocked_ids)
    if count == 0:
        logger.info("driver.block_expired_driver_licenses: nothing to do")
        return {'blocked': 0}

    # update() can't be combined with distinct(); re-filter on ids.
    Driver.objects.filter(id__in=blocked_ids).update(status='blocked',approved=False)

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
            reasons += [r.lower() for r in record_reasons.get(driver.id, [])]
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


@shared_task(name='driver.sweep_unattached_uploads')
def sweep_unattached_uploads():
    """Delete uploads never attached to a record within 24 hours (hourly beat).

    See servers/driver/upload_sweep.py. A storage outage logs and returns:
    the next hour tries again, and nothing is deleted on an error.
    """
    from servers.driver.upload_sweep import sweep_unattached_uploads as sweep

    try:
        return sweep()
    except Exception:  # noqa: BLE001
        logger.exception('unattached upload sweep failed')
        return None


#: Days before an expiry date on which the driver is reminded (11 Nov).
REMIND_DAYS = (30, 7, 1)


@shared_task(name='driver.remind_expiring_documents')
def remind_expiring_documents(today=None):
    """Daily: remind each approved driver of a dated document that expires within 30 days, once in
    each window (30, 7, 1 days left), from the dates the checklist reads: the licence on the driver,
    insurance, permit, fitness and PUC on the active vehicle. Returns how many were queued."""
    from datetime import timedelta

    from servers.driver.models import Driver
    from servers.notifications import dispatcher
    from servers.notifications.seed_data import DOCUMENT_EXPIRING

    today = today or timezone.localdate()
    horizon = today + timedelta(days=REMIND_DAYS[0])
    dated = [('driving_licence', 'Driving licence', 'license_expiry', False),
             ('insurance', 'Vehicle insurance', 'insurance_expiry', True),
             ('permit', 'Commercial permit', 'permit_expiry', True),
             ('fitness', 'Fitness certificate', 'fitness_expiry', True),
             ('puc', 'Pollution Under Control (PUC) certificate', 'puc_expiry', True)]
    soon = Q()
    for _key, _label, column, on_vehicle in dated:
        path = f'active_vehicle__{column}' if on_vehicle else column
        soon |= Q(**{f'{path}__gte': today, f'{path}__lte': horizon})
    queued = 0
    for driver in Driver.objects.filter(soon, approved=True).select_related('user_id', 'active_vehicle'):
        for key, label, column, on_vehicle in dated:
            owner = driver.active_vehicle if on_vehicle else driver
            expiry = getattr(owner, column, None) if owner is not None else None
            if not expiry or not today <= expiry <= horizon:
                continue
            left = (expiry - today).days
            window = min(w for w in REMIND_DAYS if w >= left)
            rows = dispatcher.notify(
                DOCUMENT_EXPIRING, driver.user_id,
                {'document': label, 'date': f'{expiry.day} {expiry:%b %Y}', 'days': str(left)},
                idempotency_key=f'doc_expiring:{driver.id}:{key}:{expiry.isoformat()}:{window}',
                source=DOCUMENT_EXPIRING)
            queued += len(rows)
    return queued


@shared_task(name='driver.kyc_review_target_sweep')
def kyc_review_target_sweep():
    """12 Nov: one alert per city while drivers wait past the KYC review target (review_target.py)."""
    from .review_target import sweep

    return sweep()
