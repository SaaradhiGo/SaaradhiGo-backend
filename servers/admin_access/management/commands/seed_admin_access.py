"""Seed the geo hierarchy and the eight role templates.

    python manage.py seed_admin_access                # geo + roles
    python manage.py seed_admin_access --test-admins  # QA only

Idempotent: safe on every deploy. It creates and updates; it never removes,
so a custom role or a hand-added city survives.

WHAT IT WILL NOT DO
-------------------
It does not assign anybody to anything. Granting a human access — above all
Super Admin — is a decision with a name attached, not a line in a script.
`--test-admins` is the single exception and refuses to run in production.

A city's `status` is never overwritten either: launching or pausing a city
is an operational decision, and a redeploy must not quietly undo it.

WHAT IT LEAVES IN THE AUDIT LOG, AND WHO IT NAMES
-------------------------------------------------
Roles and geo nodes are access rows, so each one this creates or changes
appends an `AdminAuditLog` row (see `admin_audit.access_audit`). A redeploy
that changes nothing writes nothing: the receivers compare the row before
and after, so the idempotence above holds for the log as well as the
tables.

Every row names the operator who ran this: `--actor-email` is required,
and must be an active operator (`admin_audit.actor_option`). A deploy that
runs this unattended names them through `QA_BOOTSTRAP_ACTOR_EMAIL` instead
-- outside production only. In production a person runs it and passes the
flag, because a production deploy that writes access rows with nobody at
the keyboard is the thing the flag exists to prevent.
"""

import os

from django.core.management.base import BaseCommand, CommandError

from base.seeding import require_seedable_environment
from django.db import transaction

from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)

COMMAND = 'seed_admin_access'

from servers.admin_access.models import (
    ACTIVE,
    CITY,
    COUNTRY,
    GLOBAL,
    PAUSED,
    STATE,
    AdminRole,
    GeoNode,
)
from servers.admin_access.roles import ROLE_TEMPLATES, validate_templates

#: Hyderabad live; one US city present but paused, so every screen can be
#: exercised in two currencies, two unit systems and two time zones.
GEO = [
    (GLOBAL, 'GLOBAL', 'Global', None, ACTIVE),
    (COUNTRY, 'IN', 'India', 'GLOBAL', ACTIVE),
    (STATE, 'IN-TG', 'Telangana', 'IN', ACTIVE),
    (CITY, 'IN-TG-HYD', 'Hyderabad', 'IN-TG', ACTIVE),
    (CITY, 'IN-TG-WGL', 'Warangal', 'IN-TG', PAUSED),
    (STATE, 'IN-AP', 'Andhra Pradesh', 'IN', ACTIVE),
    (CITY, 'IN-AP-VJA', 'Vijayawada', 'IN-AP', PAUSED),
    (CITY, 'IN-AP-VTZ', 'Visakhapatnam', 'IN-AP', PAUSED),
    (COUNTRY, 'US', 'United States', 'GLOBAL', PAUSED),
    (STATE, 'US-TX', 'Texas', 'US', PAUSED),
    (CITY, 'US-TX-AUS', 'Austin', 'US-TX', PAUSED),
]

#: One test login per seeded role. QA only — see `_refuse_in_production`.
#: Every role needs its own login; testing them through one account with
#: everything proves nothing about scope.
#: NOTE THE ABSENCE OF A SUPER ADMIN.
#:
#: There was one. It made `list_admin_access` report "the floor is met" on
#: QA while the second real Super Admin was still unnamed -- the exact
#: complacency the warning exists to prevent, caused by the warning's own
#: test fixture. Found in the QA migration rehearsal.
#:
#: Super Admin is tested in QA through the real named Super Admin, because
#: that is also the only account that will hold it in production.
TEST_ADMINS = [
    ('qa.cityops@saaradhigo.test', 'city_ops_manager', 'IN-TG-HYD'),
    ('qa.support@saaradhigo.test', 'support_agent', 'IN-TG-HYD'),
    ('qa.safety@saaradhigo.test', 'safety_officer', 'IN-TG-HYD'),
    ('qa.kyc@saaradhigo.test', 'kyc_verifier', 'IN-TG-HYD'),
    ('qa.finance@saaradhigo.test', 'finance', 'IN-TG-HYD'),
    ('qa.marketing@saaradhigo.test', 'marketing', 'IN-TG-HYD'),
    ('qa.analyst@saaradhigo.test', 'analyst', 'IN-TG-HYD'),
    # Append only: a login's phone is its position in this list, and QA
    # already holds the earlier ones. Inserting in the middle gives the next
    # login an existing login's phone and the seed stops on a duplicate.
    ('qa.supportlead@saaradhigo.test', 'support_lead', 'IN-TG-HYD'),
]


class Command(BaseCommand):
    help = 'Seed the geo hierarchy and role templates. Assigns nobody.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test-admins',
            action='store_true',
            help='Also create one labelled test admin per role. Non-production only.',
        )
        add_actor_argument(
            parser,
            help='Email of the operator running this; every row the seed '
                 'writes names them. A deploy running this unattended sets '
                 'QA_BOOTSTRAP_ACTOR_EMAIL instead (not in production).',
        )

    def handle(self, *args, **options):
        problems = validate_templates()
        if problems:
            raise CommandError(
                'Role templates reference permissions that do not exist:\n  '
                + '\n  '.join(problems)
            )

        if options['test_admins']:
            # Geo nodes and roles are configuration and run everywhere; test
            # admins are test data and go through the shared gate. Checked
            # BEFORE anything is written, so a production run with the flag
            # refuses whole rather than seeding half.
            require_seedable_environment('the QA test admins')
            self._refuse_in_production()

        # Before the first write.
        actor = require_actor(options, COMMAND, unattended=True)

        with attributed(actor, COMMAND,
                        reason='Seed: geo hierarchy and role templates.'):
            with transaction.atomic():
                nodes = self._seed_geo()
                self._seed_roles()
                self._link_existing_rows()

            if options['test_admins']:
                self._seed_test_admins(nodes)

        self.stdout.write(self.style.SUCCESS(
            'Seed complete. Nobody was assigned to any role: that is a '
            'decision with a name attached, not a script.'
        ))

    def _seed_geo(self):
        nodes = {}
        created = updated = 0
        # GEO is ordered parents-first, so a parent always exists.
        for kind, code, name, parent_code, status in GEO:
            parent = nodes.get(parent_code)
            node = GeoNode.objects.filter(code=code).first()
            if node is None:
                node = GeoNode(
                    kind=kind, code=code, name=name,
                    parent=parent, status=status,
                )
                node.save()
                created += 1
            else:
                node.kind, node.name, node.parent = kind, name, parent
                node.save()
                updated += 1
            nodes[code] = node
        self.stdout.write(f'  geo: {created} created, {updated} updated')
        return nodes

    def _seed_roles(self):
        created = updated = 0
        for template in ROLE_TEMPLATES:
            _, was_created = AdminRole.objects.update_or_create(
                code=template['code'],
                defaults={
                    'name': template['name'],
                    'description': template['description'],
                    'permissions': sorted(template['permissions']),
                    'is_global_only': template.get('is_global_only', False),
                    'is_template': True,
                    'is_active': True,
                },
            )
            created += bool(was_created)
            updated += not was_created
        self.stdout.write(f'  roles: {created} created, {updated} updated')

    def _link_existing_rows(self):
        """Attach rows that existed before the spine did.

        On a fresh database `pricing/0010` runs before any node exists, so
        it links nothing and this does the work. On an existing database
        0010 already did it and this finds nothing to do. One
        implementation either way -- see `admin_access.linking`.
        """
        from servers.driver.models import Driver
        from servers.pricing.models import ServiceZone

        from servers.admin_access.linking import (
            backfill_driver_home_city,
            link_zones_to_nodes,
        )

        # Each link and each backfilled driver leaves an audit row naming
        # this command and the operator behind the context (see
        # `admin_access.linking`).
        linked, unmatched = link_zones_to_nodes(ServiceZone, GeoNode, caller=COMMAND)
        self.stdout.write(f'  zones linked: {linked}')
        if unmatched:
            self.stdout.write(self.style.WARNING(
                f'  zones with no matching node: {sorted(unmatched)} '
                f'-- trips in them are invisible to scoped admins'
            ))

        assigned, skipped = backfill_driver_home_city(Driver, GeoNode, caller=COMMAND)
        if assigned:
            self.stdout.write(f'  drivers given a home city: {assigned}')
        if skipped:
            self.stdout.write(self.style.WARNING(
                f'  drivers left unassigned: {skipped} -- there is not '
                f'exactly one active city to attribute them to'
            ))

    def _refuse_in_production(self):
        from django.conf import settings

        if getattr(settings, 'IS_PRODUCTION', False):
            raise CommandError(
                'Refusing to create test admins in production. These are '
                'labelled accounts at known addresses; they exist so the '
                'eight roles can be exercised in QA.'
            )
        if os.environ.get('ENVIRONMENT', '').lower() in {'prod', 'production'}:
            raise CommandError(
                'Refusing to create test admins: ENVIRONMENT is production.'
            )

    def _seed_test_admins(self, nodes):
        from django.contrib.auth import get_user_model

        from servers.admin_access.models import AdminAssignment

        user_model = get_user_model()
        made = 0
        for index, (email, role_code, scope_code) in enumerate(TEST_ADMINS):
            role = AdminRole.objects.get(code=role_code)
            scope = nodes[scope_code]
            phone = f'+9188000000{index:02d}'
            user, _ = user_model.objects.get_or_create(
                email=email,
                defaults={
                    'phone_number': phone,
                    'username': phone,
                    'full_name': f'QA {role.name} (seed)',
                    'role': 'admin',
                    'is_staff': True,
                },
            )
            # No usable password: these exist for scope tests, not to be
            # signed into with a guessable credential.
            if user.has_usable_password():
                user.set_unusable_password()
                user.save(update_fields=['password'])
            AdminAssignment.objects.get_or_create(
                admin=user, role=role, scope=scope,
                defaults={'reason': 'QA seed: one test login per role.'},
            )
            made += 1
        self.stdout.write(self.style.WARNING(
            f'  test admins: {made} seeded — labelled, QA only, no usable password'
        ))
