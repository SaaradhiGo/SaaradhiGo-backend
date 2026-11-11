"""System events wired to the dispatcher in module 11.

    commission.increase_notice   drains `pricing.DriverNotice` (a commission increase is coming)
    payout.missing_details       drains `payments.DriverPayoutNotice` (add payout details)

Both outboxes were written by earlier modules with a stubbed send. They are drained here: one
`notify()` per driver, keyed `driver_notice:<id>:<driver>` / `payout_notice:<id>`, so running a
drain twice (the task, the beat sweep, the one-off command) sends nothing twice. A notice is
marked `sent` when every driver in scope has an outbox row; "sent" means "handed to the
notifications outbox", whose own status says what then happened.

NOT WIRED IN THIS MODULE (listed as follow-ups in the report): the trip-event pushes, OTP
(`base.utils.send_otp_via_sns`), receipts and every other direct FCM / SNS / email call.
"""

import logging

from django.contrib.auth import get_user_model
from django.utils import timezone

from base.countries import COUNTRIES
from servers.admin_access.models import COUNTRY, GeoNode

from . import dispatcher, recipients

logger = logging.getLogger(__name__)

COMMISSION_NOTICE = 'commission.increase_notice'
PAYOUT_NOTICE = 'payout.missing_details'


def drivers_in_scope(notice):
    """Approved, active drivers whose home city sits under the notice's scope node, of the
    notice's vehicle class when it names one."""
    User = get_user_model()
    node = notice.geo_node or GeoNode.objects.filter(kind=COUNTRY, code=notice.country).first()
    if node is None:
        return User.objects.none()
    users = User.objects.filter(role='driver', is_active=True, driver__approved=True,
                                driver__home_city__path__startswith=node.path)
    if notice.vehicle_type_id:
        users = users.filter(driver__active_vehicle__vehicle_type_id=notice.vehicle_type_id)
    return users


def _rate(value):
    return f'{value:.2f}%' if value is not None else 'the current rate'


def commission_context(notice, user):
    """The words of a commission notice, with the date in the driver's own time zone."""
    zone = recipients.timezone_of(user, notice.country)
    local = notice.effective_from.astimezone(zone)
    vehicle = notice.vehicle_type.type if notice.vehicle_type_id else 'all vehicle classes'
    place = notice.geo_node.name if notice.geo_node_id else COUNTRIES[notice.country].name
    return {
        'scope': f'{vehicle} in {place}',
        'old_rate': _rate(notice.old_percent),
        'new_rate': _rate(notice.new_percent),
        'effective_date': f'{local.day} {local:%B %Y}',
    }


def queue_driver_notice(notice, now=None):
    """Write the outbox rows for one DriverNotice. Idempotent. Returns counts."""
    from servers.pricing.commission_models import DriverNotice

    now = now or timezone.now()
    counts = {'drivers': 0, 'messages': 0, 'no_template': 0}
    notice = DriverNotice.objects.select_related('geo_node', 'vehicle_type').get(pk=notice.pk)
    for user in drivers_in_scope(notice).iterator(chunk_size=500):
        counts['drivers'] += 1
        rows = dispatcher.notify(
            COMMISSION_NOTICE, user, commission_context(notice, user),
            idempotency_key=f'driver_notice:{notice.pk}:{user.pk}', source=COMMISSION_NOTICE,
            now=now)
        counts['messages'] += len(rows)
        if not rows:
            counts['no_template'] += 1
    if counts['drivers'] == 0 or counts['messages'] > 0:
        DriverNotice.objects.filter(pk=notice.pk, status=DriverNotice.QUEUED).update(
            status=DriverNotice.SENT, sent_at=now)
    return counts


def queue_payout_notice(notice, now=None):
    """Write the outbox rows for one DriverPayoutNotice. Idempotent. Returns counts."""
    from servers.payments.models import DriverPayoutNotice

    now = now or timezone.now()
    notice = DriverPayoutNotice.objects.select_related('driver__user_id', 'run').get(pk=notice.pk)
    user = notice.driver.user_id
    rows = dispatcher.notify(
        PAYOUT_NOTICE, user, {}, idempotency_key=f'payout_notice:{notice.pk}',
        source=PAYOUT_NOTICE, now=now)
    if rows:
        DriverPayoutNotice.objects.filter(pk=notice.pk, status='queued').update(
            status='sent', sent_at=now)
    return {'drivers': 1, 'messages': len(rows), 'no_template': 0 if rows else 1}


def drain_all(now=None):
    """Every queued notice of both kinds. Returns a summary for the log or the command."""
    from servers.payments.models import DriverPayoutNotice
    from servers.pricing.commission_models import DriverNotice

    summary = {'commission_notices': 0, 'commission_messages': 0, 'payout_notices': 0,
               'payout_messages': 0, 'left_queued': 0}
    for notice in DriverNotice.objects.filter(status=DriverNotice.QUEUED).order_by('pk'):
        counts = queue_driver_notice(notice, now)
        summary['commission_notices'] += 1
        summary['commission_messages'] += counts['messages']
    for notice in DriverPayoutNotice.objects.filter(status='queued').order_by('pk'):
        counts = queue_payout_notice(notice, now)
        summary['payout_notices'] += 1
        summary['payout_messages'] += counts['messages']
    summary['left_queued'] = (
        DriverNotice.objects.filter(status=DriverNotice.QUEUED).count()
        + DriverPayoutNotice.objects.filter(status='queued').count())
    return summary

