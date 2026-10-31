"""Seed the English templates for commission.increase_notice and payout.missing_details.

CREATE-IF-MISSING. A template that already exists for (key, channel, country, locale), in any
status or version, is left exactly as it is: this command never modifies, supersedes or
re-activates anything. Run it again and it creates nothing.

India and the United States, in_app + push + sms + email, transactional. All are created ACTIVE
except the India SMS templates, which are created as DRAFT: they cannot be active without a DLT
template id, sender id and flow id (see `servers.notifications.seed_data`). That is the
activation rule working, not a gap.

Like `seed_approval_policies`, a seed writes the starting configuration directly, attributed to
the named operator and audited; every later change goes through maker-checker.

    python manage.py seed_notification_templates --actor-email you@company.example
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from servers.admin_access.models import COUNTRY, GeoNode
from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.services import record_admin_action
from servers.notifications import seed_data
from servers.notifications.models import (
    ACTIVE, DRAFT, NotificationTemplate, TemplateRegistration,
)

COMMAND = 'seed_notification_templates'
SEED_REASON = 'Seed: English templates for the commission notice and the missing-payout notice.'
AUDIT_ACTION = 'notification_templates_seeded'


class Command(BaseCommand):
    help = ('Create the English notification templates that do not exist yet. Never modifies an '
            'existing template. India SMS templates are created as drafts (no DLT ids yet).')

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)
        created, skipped, no_country = [], [], set()
        with attributed(actor, COMMAND, reason=SEED_REASON):
            with transaction.atomic():
                for key, channel, country, copy, active in seed_data.seed_rows():
                    label = f'{key}/{channel}/{country}'
                    if NotificationTemplate.objects.filter(
                            key=key, channel=channel, country=country, locale='en').exists():
                        skipped.append(label)
                        continue
                    node = GeoNode.objects.filter(kind=COUNTRY, code=country).first()
                    if node is None:
                        no_country.add(country)
                        continue
                    template = NotificationTemplate(
                        key=key, channel=channel, country=country, geo_node=node, locale='en',
                        category='transactional', subject=copy.get('subject', ''),
                        title=copy.get('title', ''), body=copy['body'],
                        variables=copy['variables'], personal_variables=[],
                        sample_values=copy['sample'], version=1,
                        status=ACTIVE if active else DRAFT, reason=SEED_REASON,
                        activated_at=timezone.now() if active else None)
                    template.set_created_by(actor)
                    template.save()
                    if channel == 'sms' and country == 'IN':
                        # The text to put in front of DLT, with the ids still to come.
                        TemplateRegistration.objects.create(
                            template=template, provider='msg91',
                            registered_text=seed_data.dlt_text(copy['body']))
                    created.append(template)

                if created:
                    record_admin_action(
                        None, action=AUDIT_ACTION, target_type='notification_template',
                        target_id='seed',
                        after={'created': [f'{t.key}/{t.channel}/{t.country}:{t.status}'
                                           for t in created],
                               'skipped_existing': skipped},
                        reason=SEED_REASON)

        for template in created:
            self.stdout.write(f'  {template.key} {template.channel} {template.country}: '
                              f'{template.status}')
        drafts = [t for t in created if t.status == DRAFT]
        if drafts:
            self.stdout.write(
                f'  {len(drafts)} India SMS template(s) were created as DRAFT: an India SMS '
                f'template cannot be active without its DLT template ID, sender ID and flow ID.')
        if no_country:
            self.stdout.write(self.style.WARNING(
                f'  no country node for {", ".join(sorted(no_country))}: run seed_admin_access '
                f'first.'))
        self.stdout.write(self.style.SUCCESS(
            f'{len(created)} templates created, {len(skipped)} already present (left as is).'))
