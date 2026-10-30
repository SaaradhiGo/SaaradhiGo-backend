"""Seed the payout configuration for India.

    python manage.py seed_payout_config --actor-email <operator>

India gets EXACTLY what the code did before `PayoutCountryConfig` existed:
platform fee 2.00 percent, minimum withdrawal 500.00 INR (50000 paise), a
payout counted as stuck after 24 hours.

CREATE-IF-MISSING, AND NO UNITED STATES ROW
-------------------------------------------
A country that already has any row is left exactly as it is (the config is
changed through the console, Part B, and a redeploy must not undo that). A
second run writes nothing, including to the audit log. Nothing here creates a
row for the United States or any other country: what a new market's fee and
minimum should be is the owner's decision, and until it is made the market has
no payout rules (withdrawals are refused with WITHDRAWALS_NOT_AVAILABLE).
One summary audit row per run that created something, naming the operator
(`--actor-email`, mandatory).
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.models import actor_snapshot
from servers.admin_audit.services import record_admin_action
from servers.payments.models import PayoutCountryConfig
from servers.payments.payout_config import INDIA_SEED

COMMAND = 'seed_payout_config'
AUDIT_ACTION = 'payout_config_seeded'
SEED_REASON = ('Seed: India payout values as they were hard-coded '
               '(fee 2.00 percent, minimum 500.00 INR, stuck after 24 hours).')


class Command(BaseCommand):
    help = 'Seed the India payout configuration. Never modifies an existing row.'

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)
        created, skipped = [], []
        with attributed(actor, COMMAND, reason=SEED_REASON):
            with transaction.atomic():
                country = INDIA_SEED['country']
                if PayoutCountryConfig.objects.filter(country=country).exists():
                    skipped.append(country)
                else:
                    snap = actor_snapshot(actor)
                    PayoutCountryConfig.objects.create(
                        country=country,
                        fee_percent=INDIA_SEED['fee_percent'],
                        minimum_withdrawal_minor=INDIA_SEED['minimum_withdrawal_minor'],
                        stuck_payout_hours=INDIA_SEED['stuck_payout_hours'],
                        reason=SEED_REASON,
                        created_by_id=snap['actor_id'], created_by_label=snap['actor_label'],
                        created_by_email=snap['actor_email'], created_by_role=snap['actor_role'],
                    )
                    created.append(country)
                if created:
                    record_admin_action(
                        None, action=AUDIT_ACTION, target_type='payout_config',
                        target_id='seed',
                        after={'created': created, 'skipped_existing': skipped},
                        reason=SEED_REASON)
        self.stdout.write(self.style.SUCCESS(
            f'{len(created)} payout config created, {len(skipped)} already configured.'))
