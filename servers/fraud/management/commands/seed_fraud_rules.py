"""Seed the fraud detection rule settings for India and the United States.

    python manage.py seed_fraud_rules --actor-email <operator>

Writes version 1 of every scheduled rule (multi_accounting, collusion, incentive_farming,
gps_anomaly, cancellation_abuse, payment_abuse) for IN and US from the standard defaults in
`servers/fraud/rules/*` (`Param.default_in` / `default_us`), only where a country has no row for the
rule at all. NEVER OVERWRITES: thresholds change in the console through `fraud.rule_change`, always
with a checker, and a redeploy that reset them would undo a two-person decision. A second run
writes nothing, including to the audit log. One summary audit row per run that created something,
naming the operator (`--actor-email`, mandatory). A country that is not seeded runs no rule.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.services import record_admin_action
from servers.fraud.config import seed_defaults

COMMAND = 'seed_fraud_rules'
AUDIT_ACTION = 'fraud_rules_seeded'
SEED_REASON = 'Seed: standard fraud detection thresholds (India, United States).'


class Command(BaseCommand):
    help = 'Seed fraud rule thresholds for IN and US where none exist. Never overwrites.'

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)
        with attributed(actor, COMMAND, reason=SEED_REASON):
            with transaction.atomic():
                created, skipped = seed_defaults(actor, SEED_REASON)
                if created:
                    record_admin_action(
                        None, action=AUDIT_ACTION, target_type='fraud_rule_config',
                        target_id='seed', after={'created': [r.as_dict() for r in created],
                                                 'skipped_existing': skipped},
                        reason=SEED_REASON)
        self.stdout.write(self.style.SUCCESS(
            f'{len(created)} rule settings seeded, {len(skipped)} already configured.'))
