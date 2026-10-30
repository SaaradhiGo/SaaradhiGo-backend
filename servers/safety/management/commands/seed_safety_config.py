"""Seed the per-country safety config for the countries in the registry.

    python manage.py seed_safety_config --actor-email <operator>

For each country in `base/countries.py`, ONLY when it has no safety config at
all (active or historical), write version 1 with these starting numbers:

    sos_ack_minutes 2          incident_assign_minutes 5
    long_stop 8 min within 50 m (exempt 150 m of pickup / drop; incident at 20 min)
    eta_overrun 50 percent and 15 min (incident at 100 percent and 30 min)

    route_deviation 300 m for 3 min      min_safety_officers 1

and the emergency numbers the registry holds for that country (none if it
has none).

THE LATER ADDITIONS (route deviation, minimum officers)
-------------------------------------------------------
A country row this seed wrote earlier (version 1, still the seed's own, never
edited by a person) has those three numbers blank because they did not exist
yet. For such a row ONLY, the blanks are filled through the normal versioned,
audited edit (`change_config`), so QA picks them up on the next run. A row a
person has edited (version above 1, or any reason that is not a seed's) is
never touched: a blank there may be a deliberate "off".

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
from servers.safety.config import change_config, config_for, registry_numbers
from servers.safety.models import SafetyCountryConfig

COMMAND = 'seed_safety_config'
AUDIT_ACTION = 'safety_config_seeded'
SEED_REASON = ('Seed: safety defaults (SOS ack 2 min, critical assign 5 min, long stop 8 min / 50 m, '
               'ETA overrun 50% and 15 min, route deviation 300 m / 3 min, at least 1 Safety '
               'Officer per live city) and the registry emergency numbers.')

#: Starting numbers, written only where a country has no row.
DEFAULTS = {
    'sos_ack_minutes': 2, 'incident_assign_minutes': 5,
    'long_stop_minutes': 8, 'long_stop_radius_m': 50, 'stop_exempt_radius_m': 150,
    'long_stop_incident_minutes': 20,
    'eta_overrun_percent': 50, 'eta_overrun_minutes': 15,
    'eta_overrun_incident_percent': 100, 'eta_overrun_incident_minutes': 30,
    'route_deviation_meters': 300, 'route_deviation_minutes': 3,
    'min_safety_officers': 1,
}
#: The numbers added after the first seed; see the module docstring.
ADDED_LATER = ('route_deviation_meters', 'route_deviation_minutes', 'min_safety_officers')
SEED_PREFIX = 'Seed:'


class Command(BaseCommand):
    help = 'Seed safety config for every registry country that has none. Never overwrites.'

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)
        created, skipped, filled = [], [], []
        with attributed(actor, COMMAND, reason=SEED_REASON):
            with transaction.atomic():
                for code in sorted(COUNTRIES):
                    if SafetyCountryConfig.objects.filter(country=code).exists():
                        if self._fill_added_numbers(code, actor):
                            filled.append(code)
                        else:
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
            f'{len(skipped)} already configured, '
            f'{len(filled)} filled with the newer numbers ({", ".join(filled) or "none"}).'))

    def _fill_added_numbers(self, code, actor):
        """Fill the later-added blanks of a still-pristine seed row.

        Pristine means: the active row is version 1 and its reason is a
        seed's. Returns True if a new version was written.
        """
        row = config_for(code)
        if row is None or row.version != 1 or not (row.reason or '').startswith(SEED_PREFIX):
            return False
        blanks = {name: DEFAULTS[name] for name in ADDED_LATER if getattr(row, name) is None}
        if not blanks:
            return False
        _row, changed = change_config(actor, code, blanks, reason=SEED_REASON)
        return changed
