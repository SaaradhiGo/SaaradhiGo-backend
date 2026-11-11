"""Send the notices queued since module 8, once, through the notifications outbox.

`pricing.DriverNotice` (a commission increase is coming) and `payments.DriverPayoutNotice`
(add your payout details) rows were written with a stubbed send and have waited `queued`
since. This hands each to the dispatcher: one outbox row per driver per channel, keyed so that
running it twice (or alongside the beat sweep) creates nothing twice.

WHAT "SEND" MEANS HERE is whatever the dispatcher decides: with NOTIFY_LIVE_SEND off (the
default everywhere) every external channel goes to the stub, which records and sends nothing;
with it on outside production only recipients on NOTIFY_TEST_RECIPIENTS are sent to and everyone
else is recorded as suppressed. In-app rows are written either way.

    python manage.py send_queued_notices --dry-run
    python manage.py send_queued_notices --actor-email you@company.example
"""

from django.core.management.base import BaseCommand

from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.services import record_admin_action
from servers.notifications import events

COMMAND = 'send_queued_notices'
AUDIT_ACTION = 'notification_queued_notices_sent'
REASON = 'One-off: hand the notices queued before module 11 to the notifications outbox.'


class Command(BaseCommand):
    help = 'Hand every queued DriverNotice / DriverPayoutNotice to the notifications outbox.'

    def add_arguments(self, parser):
        add_actor_argument(parser)
        parser.add_argument('--dry-run', action='store_true',
                            help='Count what would be sent; write nothing.')

    def handle(self, *args, **options):
        from servers.payments.models import DriverPayoutNotice
        from servers.pricing.commission_models import DriverNotice

        if options['dry_run']:
            self.stdout.write(
                f'Dry run: {DriverNotice.objects.filter(status="queued").count()} commission '
                f'notice(s) and {DriverPayoutNotice.objects.filter(status="queued").count()} '
                f'payout notice(s) are queued. Nothing was written.')
            return

        actor = require_actor(options, COMMAND)
        with attributed(actor, COMMAND, reason=REASON):
            summary = events.drain_all()
            record_admin_action(
                None, action=AUDIT_ACTION, target_type='notification_outbox', target_id='backlog',
                after=summary, reason=REASON)
        self.stdout.write(
            f"commission notices: {summary['commission_notices']} handled, "
            f"{summary['commission_messages']} message(s) queued")
        self.stdout.write(
            f"payout notices: {summary['payout_notices']} handled, "
            f"{summary['payout_messages']} message(s) queued")
        self.stdout.write(self.style.SUCCESS(
            f"{summary['left_queued']} notice(s) left queued (no active template or no "
            f"recipient yet); run seed_notification_templates first if that is not zero."))
