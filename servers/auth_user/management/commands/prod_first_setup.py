"""First setup of an EMPTY production database (17 Nov, PROD round 1).

    python manage.py prod_first_setup --owner-email <Srini's address> [--owner-name Srini]

Every seed writes access or config rows and names the operator who ran it. On an empty database
nobody exists, and nobody should be credited with what a build did. So this:

  1. refuses unless the database holds no active operator (it runs once);
  2. creates a labelled build operator ("PROD build (Claude)", unusable password, no assignment),
     the actor every seed row names;
  3. runs the owner-ruled seeds: seed_admin_access (geo tree, roles; only Hyderabad opens, per the
     14 Nov launch gate), approval policies, alert thresholds, fraud rules, notification templates,
     payout config, safety config, support SLA. Never a QA or test seed;
  4. invites the owner as the single Super Admin (invite_super_admin);
  5. deactivates the build operator, so no account but the owner's can act.

Prints a summary of what exists afterwards; nothing secret.
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from django.db.models import Q

User = get_user_model()

BUILD_EMAIL = 'prod-build@saaradhigo.invalid'
SEEDS = ('seed_admin_access', 'seed_approval_policies', 'seed_alert_thresholds', 'seed_fraud_rules',
         'seed_notification_templates', 'seed_payout_config', 'seed_safety_config', 'seed_support_sla')


class Command(BaseCommand):
    help = 'Seed an empty production database and invite the owner as Super Admin.'

    def add_arguments(self, parser):
        parser.add_argument('--owner-email', required=True)
        parser.add_argument('--owner-name', default='')

    def handle(self, *args, **options):
        if User.objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(role='admin', is_staff=True)).exists():
            raise CommandError('An operator already exists: this database has been set up. Nothing was changed.')
        build = User(phone_number='invite-prod-build', username='u-prod-build', email=BUILD_EMAIL,
                     full_name='PROD build (Claude)', role='admin', is_staff=True, is_active=True)
        build.set_unusable_password()
        build.save()
        try:
            for name in SEEDS:
                call_command(name, actor_email=BUILD_EMAIL, stdout=StringIO())
                self.stdout.write(f'seeded: {name}')
            call_command('invite_super_admin', email=options['owner_email'], name=options['owner_name'],
                         stdout=self.stdout)
        finally:
            User.objects.filter(pk=build.pk).update(is_active=False)
            self.stdout.write('build operator deactivated')
        self._summary()

    def _summary(self):
        from servers.admin_access.models import CITY, AdminAssignment, GeoNode
        from servers.pricing.models import ServiceZone

        cities = GeoNode.objects.filter(kind=CITY).order_by('code').values_list('code', 'status')
        self.stdout.write('cities: ' + ', '.join(f'{c}={s}' for c, s in cities))
        zones = ServiceZone.objects.order_by('code').values_list('code', 'is_active')
        self.stdout.write('zones: ' + ', '.join(f'{c}={"on" if a else "off"}' for c, a in zones))
        self.stdout.write(f'users: {User.objects.count()} (active operators: '
                          f'{User.objects.filter(is_active=True, is_staff=True).count()})')
        self.stdout.write(f'assignments: {AdminAssignment.objects.filter(is_active=True).count()} active')
