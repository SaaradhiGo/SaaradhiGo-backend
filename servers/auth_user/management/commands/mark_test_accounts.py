"""Flag QA and demo accounts as test accounts, so their trips never count (22 Nov).

    python manage.py mark_test_accounts --emails a@x,b@y --phones +91...   # dry run
    python manage.py mark_test_accounts --emails ... --confirm --actor-email you@saaradhigo.com
    python manage.py mark_test_accounts --unmark --emails ... --confirm --actor-email ...
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from servers.admin_audit.actor_option import actor_label, add_actor_argument, require_actor
from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action

COMMAND = 'mark_test_accounts'


class Command(BaseCommand):
    help = 'Flag (or unflag) accounts as test accounts; their trips are left out of every figure.'

    def add_arguments(self, parser):
        parser.add_argument('--emails', default='')
        parser.add_argument('--phones', default='')
        parser.add_argument('--email-like', default='', help='Every account whose email contains this')
        parser.add_argument('--unmark', action='store_true')
        parser.add_argument('--confirm', action='store_true')
        add_actor_argument(parser)

    def handle(self, *args, **options):
        emails = [e.strip().lower() for e in options['emails'].split(',') if e.strip()]
        phones = [p.strip() for p in options['phones'].split(',') if p.strip()]
        like = options['email_like'].strip()
        if not (emails or phones or like):
            raise CommandError('Name the accounts: --emails, --phones or --email-like.')
        q = Q(pk__in=[])
        if emails:
            q |= Q(email__in=emails)
        if phones:
            q |= Q(phone_number__in=phones)
        if like:
            q |= Q(email__icontains=like)
        want = not options['unmark']
        users = list(get_user_model().objects.filter(q).exclude(is_test_account=want))
        self.stdout.write(f'{len(users)} account(s) to {"flag" if want else "unflag"}: '
                          + ', '.join(str(u.pk) for u in users[:50]))
        if not options['confirm']:
            self.stdout.write('Dry run. Add --confirm --actor-email to write.')
            return
        actor = require_actor(options, COMMAND)
        reason = 'Test account: its trips are left out of every figure' if want else 'No longer a test account'
        with audit_context(actor=actor, actor_label=actor_label(actor, COMMAND), reason=reason):
            for u in users:
                type(u).objects.filter(pk=u.pk).update(is_test_account=want)
                record_admin_action(None, action='test_account_flagged' if want else 'test_account_unflagged',
                                    target_type='user', target_id=str(u.pk),
                                    before={'is_test_account': not want}, after={'is_test_account': want},
                                    reason=reason)
        self.stdout.write(f'Done: {len(users)} account(s).')
