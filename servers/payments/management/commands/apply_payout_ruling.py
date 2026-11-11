"""Apply the owner's payout ruling (AUDIT 27.43) to an environment that has the old values.

    python manage.py apply_payout_ruling --actor-email <operator>

A ONE-OFF RULING COMMAND, NOT A SEED
------------------------------------
seed_payout_config only creates what is missing, and an environment that
already holds India's old row (platform fee 2 percent) would never get the
ruling from it. This command writes the ruling:

  * India: a NEW config version with fee 0 (reason "Owner ruling, AUDIT 27.43")
    when the active row differs from the ruling; the old version is closed and
    stays in the history. Minimum, stuck hours and schedule are the ruling's.
  * United States: created if it has no row at all; an existing US row is left
    as it is (a person set it since).

It is audited (one summary row naming the operator; --actor-email is
mandatory) and idempotent: a second run finds the active India row already
equal to the ruling and the US row present, and writes nothing, not even an
audit row. It is not named seed_*, so the seed guard does not apply; it is
registered in tests/test_actor_email_mandatory.py as a writing command.

It changes the platform fee a driver is shown in India from 2 to 0 percent (a
driver-favourable change the owner ruled; nothing ever charged the 2 percent).
It moves no money by itself.
"""

import json

from django.core.management.base import BaseCommand
from django.db import transaction

from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.models import actor_snapshot
from servers.admin_audit.services import record_admin_action
from servers.payments.models import PayoutCountryConfig
from servers.payments.payout_config import INDIA_SEED, RULING_REASON, US_SEED
from servers.payments.payout_config_write import CONFIG_FIELDS, values_of, write_config_version

COMMAND = 'apply_payout_ruling'
AUDIT_ACTION = 'payout_ruling_applied'


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


class Command(BaseCommand):
    help = ('Apply the owner payout ruling (AUDIT 27.43): India fee 0 as a new version; '
            'create the US row.')

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)
        changes = []
        with attributed(actor, COMMAND, reason=RULING_REASON):
            with transaction.atomic():
                snap = actor_snapshot(actor)
                active = PayoutCountryConfig.objects.filter(
                    country=INDIA_SEED['country'], is_active=True).first()
                wanted = {k: INDIA_SEED[k] for k in CONFIG_FIELDS}
                if active is None or values_of(active) != wanted:
                    row, previous = write_config_version(
                        INDIA_SEED['country'], wanted, reason=RULING_REASON, snapshot=snap)
                    changes.append({
                        'country': 'IN', 'version': row.version,
                        'before': previous.as_dict() if previous else None,
                        'after': row.as_dict(),
                    })
                if not PayoutCountryConfig.objects.filter(country=US_SEED['country']).exists():
                    row, _ = write_config_version(
                        US_SEED['country'], {k: US_SEED[k] for k in CONFIG_FIELDS},
                        reason=RULING_REASON, snapshot=snap)
                    changes.append({'country': 'US', 'version': row.version,
                                    'before': None, 'after': row.as_dict()})
                if changes:
                    record_admin_action(
                        None, action=AUDIT_ACTION, target_type='payout_config',
                        target_id='ruling-27.43',
                        after={'changes': _jsonable(changes)}, reason=RULING_REASON)
        for change in changes:
            self.stdout.write(f"  {change['country']}: version {change['version']} written")
        self.stdout.write(self.style.SUCCESS(
            f'{len(changes)} change(s) applied.' if changes
            else 'Nothing to change: the ruling is already in place.'))
