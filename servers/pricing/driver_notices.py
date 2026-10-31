"""Telling drivers a commission increase is coming (AUDIT 27.36), through the Notifications module.

A rule that raises the commission needs `notice_days` of notice, and the drivers in scope must be
told:

* the OUTBOX is `commission_workflow.activate_rule`'s `DriverNotice` row per raise, with the old
  and new rate, the scope and the date it bites;
* `deliver` hands it to the notifications outbox: it schedules the sweep that fans the notice
  out to every approved driver in scope through `notify()` (template
  `commission.increase_notice` on in_app, push, SMS and email), after the activation commits.
  The row turns `sent` when every driver has an outbox row. Rows queued before this module
  existed are sent by `send_queued_notices`.

The fan-out is idempotent (`driver_notice:<id>:<driver>`), so the task, the beat sweep and the
command can all run over the same notice and nothing is sent twice. What actually reaches a
phone is decided by the dispatcher: the stub by default, the test allowlist when live outside
production.
"""

import logging

from django.db import transaction

logger = logging.getLogger(__name__)


def deliver(notice):
    """Queue the fan-out for `notice`. Returns False: nothing is sent inline."""
    def go():
        try:
            from servers.notifications.tasks import dispatch_queued
            dispatch_queued.delay()
        except Exception:  # noqa: BLE001 - the beat sweep drains DriverNotice rows regardless
            logger.exception('driver_notice_schedule_failed notice_id=%s', notice.id)

    transaction.on_commit(go)
    logger.info('driver_notice_handed_to_outbox', extra={
        'event': 'driver_notice_handed_to_outbox', 'notice_id': notice.id,
        'country': notice.country})
    return False
