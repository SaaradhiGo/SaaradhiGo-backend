"""Seed version 1 of every Command Centre alert threshold, for every city.

    python manage.py seed_alert_thresholds --actor-email <operator>

WHAT IT SEEDS (brief, Command Centre module; `command_centre.metrics`)
----------------------------------------------------------------------
    unfulfilled_rate      warn 15 %    critical 30 %     over 24h
    acceptance_rate       warn < 70 %  critical < 50 %   over 24h
    avg_eta_minutes       warn > 8     critical > 12     over 24h
    sos_open              critical > 0                   now
    sos_unacknowledged    critical > 5 min               now
    withdrawal_pending    warn > 48 h                    now

for every CITY node in the geo tree, live or paused: a paused city is one
that will launch, and launching with no alert lines is how its first bad
night goes unnoticed.

IDEMPOTENT, AND IT NEVER OVERWRITES
-----------------------------------
A (city, metric) that already has an active threshold is left exactly as
it is, whatever its lines. Thresholds change through maker-checker
(`command_centre.threshold_change`), and a redeploy that reset them to
the seed would undo a two-person decision with nobody's name on it. So
this creates version 1 where none exists and touches nothing else; a
second run writes nothing, including to the audit log.

One summary audit row per run that created something, naming the operator
(`--actor-email`, required before the first write) and every row written.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from servers.admin_access.models import CITY, GeoNode
from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)
from servers.admin_audit.services import record_admin_action
from servers.command_centre.thresholds import (
    active_threshold, seed_defaults, write_threshold_version,
)

COMMAND = 'seed_alert_thresholds'
AUDIT_ACTION = 'alert_thresholds_seeded'
SEED_REASON = 'Seed: Command Centre default alert thresholds (brief, module 1).'


class Command(BaseCommand):
    help = 'Seed the default Command Centre alert thresholds for every city. Never overwrites.'

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)

        created, skipped = [], []
        with attributed(actor, COMMAND, reason=SEED_REASON):
            with transaction.atomic():
                for city in GeoNode.objects.filter(kind=CITY).order_by('path'):
                    for spec in seed_defaults():
                        if active_threshold(city, spec['metric']) is not None:
                            skipped.append(f'{city.code}:{spec["metric"]}')
                            continue
                        row, _superseded = write_threshold_version(
                            city=city, created_by=actor, reason=SEED_REASON, **spec,
                        )
                        created.append(row)

                if created:
                    record_admin_action(
                        None,
                        action=AUDIT_ACTION,
                        target_type='alert_threshold',
                        target_id='seed',
                        after={
                            'created': [t.as_dict() for t in created],
                            'skipped_existing': skipped,
                        },
                        reason=SEED_REASON,
                    )

        for row in created:
            self.stdout.write(
                f'  {row.city.code} {row.metric}: warn={row.warn_value} '
                f'critical={row.critical_value} window={row.window}'
            )
        if skipped:
            self.stdout.write(f'  left as configured: {len(skipped)}')
        self.stdout.write(self.style.SUCCESS(
            f'{len(created)} thresholds created, {len(skipped)} already configured.'
        ))
