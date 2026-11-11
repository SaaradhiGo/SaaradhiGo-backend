"""Seed the support SLA policy for India and the United States.

    python manage.py seed_support_sla --actor-email <operator>

Same numbers for both countries (minutes, first response / resolution):

    urgent   15 /  240
    high     60 / 1440
    normal  240 / 2880
    low    1440 / 4320

IDEMPOTENT, AND IT NEVER OVERWRITES
-----------------------------------
A (country, priority) that already has a row is left exactly as it is: the
policy is changed through the console by a `support.sla` holder, and a
redeploy that reset it would undo that decision. A second run writes
nothing, including to the audit log. It also seeds SupportCountryConfig
(reminder 2 days, auto-close 7 days, refund review 72 h) for IN and US, only
where a country has no row. One summary audit row per run that
created something, naming the operator (`--actor-email`, mandatory).
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.services import record_admin_action
from servers.support.models import SupportCountryConfig, SupportSlaPolicy
from servers.support.sla import DEFAULT_MINUTES, PRIORITIES

COMMAND = 'seed_support_sla'
AUDIT_ACTION = 'support_sla_seeded'
COUNTRIES = ('IN', 'US')
#: Starting numbers for the per-country support settings: reminder after 2
#: days waiting, auto-close after 7, refund review 72 hours. Written only
#: where a country has no row. NOT a fallback: a country that is never seeded
#: has no reminder, no auto-close and no refund timeframe.
CONFIG_DEFAULTS = {'waiting_reminder_days': 2, 'waiting_autoclose_days': 7,
                   'refund_review_hours': 72}
SEED_REASON = 'Seed: support SLA defaults (urgent 15/240, high 60/1440, normal 240/2880, low 1440/4320).'


class Command(BaseCommand):
    help = 'Seed the support SLA policy for IN and US. Never overwrites an existing row.'

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)
        created, skipped = [], []
        with attributed(actor, COMMAND, reason=SEED_REASON):
            with transaction.atomic():
                for country in COUNTRIES:
                    for priority in PRIORITIES:
                        first, resolution = DEFAULT_MINUTES[priority]
                        _row, made = SupportSlaPolicy.objects.get_or_create(
                            country=country, priority=priority,
                            defaults={'first_response_minutes': first,
                                      'resolution_minutes': resolution, 'updated_by': actor})
                        (created if made else skipped).append(f'{country}:{priority}')
                config_created = []
                for country in COUNTRIES:
                    _row, made = SupportCountryConfig.objects.get_or_create(
                        country=country,
                        defaults={**CONFIG_DEFAULTS, 'updated_by': actor})
                    if made:
                        config_created.append(country)
                if created or config_created:
                    record_admin_action(
                        None, action=AUDIT_ACTION, target_type='support_sla', target_id='seed',
                        after={'created': created, 'skipped_existing': skipped,
                               'country_config_created': config_created},
                        reason=SEED_REASON)
        self.stdout.write(self.style.SUCCESS(
            f'{len(created)} SLA rows created, {len(skipped)} already configured; '
            f'{len(config_created)} country settings created.'))
