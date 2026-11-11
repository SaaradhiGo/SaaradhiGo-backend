"""Seed the payout configuration for India and the United States.

    python manage.py seed_payout_config --actor-email <operator>

A fresh environment gets the owner's values (AUDIT 27.43, see
OWNER_RULING in payout_config): India fee 0, minimum 500.00 INR, stuck after 24
hours, on request; United States fee 0, minimum 1.00 USD, stuck after 72 hours,
weekly (Mondays).

CREATE-IF-MISSING
-----------------
A country that already has any row is left exactly as it is (the config is
changed through the console, and a redeploy must not undo that). A second run
writes nothing, including to the audit log. An environment that already holds
the OLD India row (platform fee 2 percent) is moved to the owner's values by the
one-off apply_payout_ruling command, not by this seed. One summary audit row
per run that created something, naming the operator (--actor-email, mandatory).
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.models import actor_snapshot
from servers.admin_audit.services import record_admin_action
from servers.payments.models import PayoutCountryConfig
from servers.payments.payout_config import OWNER_RULING
from servers.payments.payout_config_write import CONFIG_FIELDS

COMMAND = 'seed_payout_config'
AUDIT_ACTION = 'payout_config_seeded'
SEED_REASON = ("Seed: the owner's payout values (AUDIT 27.43): India fee 0, minimum 500.00 INR, "
               'stuck after 24 hours, on request; United States fee 0, minimum 1.00 USD, '
               'stuck after 72 hours, weekly.')


class Command(BaseCommand):
    help = 'Seed the payout configuration (India, United States). Never modifies an existing row.'

    def add_arguments(self, parser):
        add_actor_argument(parser)
        parser.add_argument(
            '--country', action='append', default=None,
            help='Seed only this country (repeatable). Default: every country in the ruling.')

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)
        created, skipped = [], []
        with attributed(actor, COMMAND, reason=SEED_REASON):
            with transaction.atomic():
                snap = actor_snapshot(actor)
                only = {c.upper() for c in (options.get('country') or [])}
                for seed in OWNER_RULING:
                    country = seed['country']
                    if only and country not in only:
                        continue
                    if PayoutCountryConfig.objects.filter(country=country).exists():
                        skipped.append(country)
                        continue
                    PayoutCountryConfig.objects.create(
                        reason=SEED_REASON,
                        created_by_id=snap['actor_id'], created_by_label=snap['actor_label'],
                        created_by_email=snap['actor_email'], created_by_role=snap['actor_role'],
                        **{k: seed[k] for k in ('country',) + CONFIG_FIELDS},
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
