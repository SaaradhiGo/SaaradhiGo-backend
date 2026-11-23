"""Reset every account's saved theme to light (owner, 22 Nov: "Reset existing accounts to light").

    python manage.py reset_themes_to_light                                   # dry run: how many
    python manage.py reset_themes_to_light --confirm --actor-email you@...   # write, one audit row
"""

from django.core.management.base import BaseCommand

from servers.admin_audit.actor_option import actor_label, add_actor_argument, require_actor
from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action

COMMAND = 'reset_themes_to_light'


class Command(BaseCommand):
    help = "Set every account's saved theme to light."

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true')
        add_actor_argument(parser)

    def handle(self, *args, **options):
        from servers.auth_user.models import UserPreference

        rows = UserPreference.objects.exclude(preferred_theme='light').exclude(preferred_theme__isnull=True)
        n = rows.count()
        self.stdout.write(f'{n} account(s) with a dark or automatic theme.')
        if not options['confirm']:
            self.stdout.write('Dry run. Add --confirm --actor-email to write.')
            return
        actor = require_actor(options, COMMAND)
        reason = 'Owner, 22 Nov: light is the default; every account starts again from light.'
        with audit_context(actor=actor, actor_label=actor_label(actor, COMMAND), reason=reason):
            changed = rows.update(preferred_theme='light')
            record_admin_action(None, action='themes_reset_to_light', target_type='user_preference',
                                target_id='all', before={'not_light': n}, after={'reset': changed}, reason=reason)
        self.stdout.write(f'Reset {changed} account(s) to light.')
