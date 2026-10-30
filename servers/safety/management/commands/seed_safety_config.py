"""Seed the per-country safety config for the countries in the registry.

    python manage.py seed_safety_config --actor-email <operator>

For each country in `base/countries.py`, ONLY when it has no safety config at
all (active or historical), write version 1 with these starting numbers:

    sos_ack_minutes 2          incident_assign_minutes 5
    long_stop 8 min within 50 m (exempt 150 m of pickup / drop; incident at 20 min)
    eta_overrun 50 percent and 15 min (incident at 100 percent and 30 min)

and the emergency numbers the registry holds for that country (none if it
has none). The route-deviation numbers are left blank: nothing measures it.

IDEMPOTENT, AND IT NEVER OVERWRITES
-----------------------------------
A country that already has a row is left exactly as it is: the numbers are
changed in the console by a `safety.config` holder, and a redeploy that reset
them would undo that decision. A second run writes nothing, including to the
audit log. One summary audit row per run that created something, naming the
operator (`--actor-email`, mandatory). NOT a fallback: a country that is not in
the registry and is never seeded has no timer and no detection.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from base.countries import COUNTRIES
from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.services import record_admin_action
from servers.safety.config import registry_numbers
from servers.safety.models import SafetyCountryConfig

COMMAND = 'seed_safety_config'
AUDIT_ACTION = 'safety_config_seeded'
SEED_REASON = ('Seed: safety defaults (SOS ack 2 min, critical assign 5 min, long stop 8 min / 50 m, '
               'ETA overrun 50% and 15 min) and the registry emergency numbers.')

#: Starting numbers, written only where a country has no row.
DEFAULTS = {
    'sos_ack_minutes': 2, 'incident_assign_minutes': 5,
    'long_stop_minutes': 8, 'long_stop_radius_m': 50, 'stop_exempt_radius_m': 150,
    'long_stop_incident_minutes': 20,
    'eta_overrun_percent': 50, 'eta_overrun_minutes': 15,
    'eta_overrun_incident_percent': 100, 'eta_overrun_incident_minutes': 30,
}


class Command(BaseCommand):
    help = 'Seed safety config for every registry country that has none. Never overwrites.'

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)
        created, skipped = [], []
        with attributed(actor, COMMAND, reason=SEED_REASON):
            with transaction.atomic():
                for code in sorted(COUNTRIES):
                    if SafetyCountryConfig.objects.filter(country=code).exists():
                        skipped.append(code)
                        continue
                    row = SafetyCountryConfig(
                        country=code, version=1, is_active=True, reason=SEED_REASON,
                        emergency_numbers=registry_numbers(code), **DEFAULTS)
                    row.set_created_by(actor)
                    row.save()
                    created.append(code)
                if created:
                    record_admin_action(
                        None, action=AUDIT_ACTION, target_type='safety_config', target_id='seed',
                        after={'created': created, 'skipped_existing': skipped}, reason=SEED_REASON)
        self.stdout.write(self.style.SUCCESS(
            f'{len(created)} countries seeded ({", ".join(created) or "none"}), '
            f'{len(skipped)} already configured.'))
