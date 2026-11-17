"""The FIRST Super Admin of an empty database, by invitation (17 Nov, PROD build).

    python manage.py invite_super_admin --email srini@example.com --name "Srini"
    python manage.py invite_super_admin --email srini@example.com --resend

Owner's brief: one Super Admin for Srini, invited; his password is never set by anyone else. So the
account gets an unusable password and the set-password link the console invite sends (using it also
verifies the address). It holds Super Admin at Global, which only Super Admin may hold.

Refuses when any active Super Admin assignment exists: every later operator is invited from the
console by a person. Where email is not configured yet (PROD before SES) the account is created and
the command says plainly that NO link went out; `--resend` sends it once email works. Nothing secret
is printed.
"""

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from base.auth_email import delivery_is_configured, send_account_invite
from servers.admin_access.models import GLOBAL, AdminAssignment, AdminRole, GeoNode
from servers.admin_access.roles import SUPER_ADMIN
from servers.auth_user.identity import InvalidEmail, normalize_email
from servers.auth_user.password_views import _uid, reset_tokens

User = get_user_model()


class Command(BaseCommand):
    help = 'Invite the first Super Admin of an empty database (unusable password, emailed link).'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--name', default='')
        parser.add_argument('--resend', action='store_true',
                            help='Send the link again to that account, if it has not set a password.')

    def handle(self, *args, **options):
        try:
            email = normalize_email(options['email'])
        except InvalidEmail as exc:
            raise CommandError(f'Not a usable email address: {exc}') from exc
        if options['resend']:
            user = User.objects.filter(email__iexact=email, is_staff=True).first()
            if user is None or user.has_usable_password():
                raise CommandError('No invited operator without a password has that address.')
            return self._send(user)

        if AdminAssignment.objects.filter(role__code=SUPER_ADMIN, is_active=True).exists():
            raise CommandError('A Super Admin already exists. Invite operators from the console.')
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError('An account already uses that address; this command never changes one.')
        role = AdminRole.objects.filter(code=SUPER_ADMIN).first()
        root = GeoNode.objects.filter(kind=GLOBAL).first()
        if role is None or root is None:
            raise CommandError('Run migrations and seed_admin_access first (no Super Admin role or Global node).')

        with transaction.atomic():
            user = User(phone_number=f'inv-{secrets.token_hex(8)}', username=f'u-{secrets.token_hex(8)}',
                        email=email, full_name=(options['name'] or '').strip() or None,
                        role='admin', is_staff=True, is_superuser=False, is_active=True)
            user.set_unusable_password()
            user.save()
            AdminAssignment.objects.create(admin=user, role=role, scope=root, granted_by=None,
                                           reason='First Super Admin, invited at the PROD build (owner brief, 17 Nov).')
        self.stdout.write(self.style.SUCCESS(f'Super Admin account created (user id {user.pk}), Global scope.'))
        self._send(user)

    def _send(self, user):
        ok, why = delivery_is_configured()
        if not ok:
            self.stdout.write(self.style.WARNING(
                f'Invite NOT sent: email is not configured ({why}). Once it is, run this command '
                'again with --resend.'))
            return
        sent = send_account_invite(user=user, uid=_uid(user),
                                   token=reset_tokens.make_token(user), role_label='Super Admin',
                                   invited_by='SaaradhiGo')
        self.stdout.write(self.style.SUCCESS('Invite sent.') if sent else self.style.ERROR('Invite could not be sent.'))
