"""Telling drivers a commission increase is coming. THE SEND IS A STUB.

AUDIT 27.36: a rule that raises the commission needs `notice_days` of notice, and
the drivers in scope must be told. Telling them is the Notifications module's job
(module 13), which does not exist yet. So:

* the OUTBOX is real: `commission_workflow.activate_rule` writes one `DriverNotice`
  row per raise, with the old and new rate, the scope and the date it bites;
* the SEND is not: `deliver` logs and leaves the row `queued`.

When module 13 lands, replace the body of `deliver` (or have the module drain
`DriverNotice.objects.filter(status='queued')`) and stamp `status='sent'`.
Nothing else in the pricing code needs to change.
"""

import logging

logger = logging.getLogger(__name__)


def deliver(notice):
    """STUB: no driver is notified. Leaves the row queued."""
    logger.warning('driver_notice_queued_not_sent', extra={
        'event': 'driver_notice_queued_not_sent', 'notice_id': notice.id,
        'country': notice.country, 'old_percent': str(notice.old_percent),
        'new_percent': str(notice.new_percent),
        'effective_from': notice.effective_from.isoformat(),
        'note': 'Notifications module (13) does not exist; send is stubbed.'})
    return False
