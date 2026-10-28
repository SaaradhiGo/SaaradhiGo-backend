"""Enrol an operator's second factor, and issue their recovery codes.

Run by a human with shell access to the deployment, once per operator:

    python manage.py enroll_operator_mfa --phone +91XXXXXXXXXX \\
        --actor-email you@saaradhigo.com

It prints a provisioning URI for the operator to add to an authenticator app, waits
for a code to prove the app is in sync, then prints single-use recovery codes.

WHY THERE IS NO HTTP ENDPOINT FOR THIS
--------------------------------------
Self-service enrolment means an endpoint that issues a shared secret for the account
that approves payouts. Whoever can call it, at whatever moment, can enrol their own
device. Getting that right needs its own authorization story, a re-authentication
step, and a rate limit -- all to serve an action that happens four times in the life
of this pilot.

So enrolment requires shell access, which is the same bar as the operator bootstrap
and is not reachable from the internet. If the pilot grows enough that this is
friction, that is the point to build the endpoint properly.

THE SECRET
----------
The provisioning URI contains the TOTP shared secret. It is printed to the terminal
once, deliberately: that is how the operator receives it. It is never logged, never
written to the audit row, and never returned by any HTTP response. Whoever runs this
command is responsible for the terminal they run it in.

WHO ENROLLED THEM
-----------------
A second factor is part of who may operate, so every run that writes -- creating the
device, confirming it, replacing an old one -- requires `--actor-email`, the address
of the operator at the keyboard, validated by `admin_audit.actor_option`. The audit
row names them.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from base import ops_mfa
from base.permissions import is_operator
from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)
from servers.admin_audit.services import record_admin_action

User = get_user_model()

COMMAND = 'enroll_operator_mfa'


class Command(BaseCommand):
    help = "Enrol a TOTP device and issue recovery codes for an operator account."

    def add_arguments(self, parser):
        parser.add_argument(
            '--phone', required=True,
            help='The operator account, in E.164 (+91XXXXXXXXXX).')
        parser.add_argument(
            '--code', default=None,
            help='The 6-digit code from the authenticator app. Omit on the first '
                 'run to be shown the provisioning URI, then re-run with it.')
        parser.add_argument(
            '--replace', action='store_true',
            help='Discard any existing factor on this account first. Required to '
                 're-enrol, so a second run cannot silently orphan a device the '
                 'operator is still using.')
        add_actor_argument(parser)

    def handle(self, *args, **options):
        phone = options['phone'].strip()
        try:
            user = User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            raise CommandError(
                'No account with that phone number. Create the operator first '
                'with create_operator.'
            )

        if not is_operator(user):
            raise CommandError(
                'That account is not an operator (it needs role=admin and '
                'is_staff, or superuser). MFA on a rider or driver account would '
                'protect nothing and block their sign-in.'
            )

        existing = ops_mfa.devices_for(user)
        if existing and not options['replace']:
            raise CommandError(
                f'This account already has {len(existing)} confirmed factor(s). '
                f'Re-run with --replace to discard them and enrol again. Doing '
                f'that invalidates the operator\'s current authenticator entry '
                f'and all their recovery codes.'
            )

        # Every path from here writes: before the first write.
        actor = require_actor(options, COMMAND)
        with attributed(actor, COMMAND,
                        reason='Operator second factor enrolled via management command.'):
            self._enrol(user, existing, options)

    def _enrol(self, user, existing, options):
        if existing and options['replace']:
            removed = ops_mfa.remove_devices(user)
            self.stdout.write(self.style.WARNING(
                f'Removed {removed} existing factor(s).'))

        code = options['code']
        if not code:
            device, uri = ops_mfa.enroll_totp(user)
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'The line below contains a SECRET. Give it to the operator '
                'directly and do not paste it anywhere else.'))
            self.stdout.write('')
            self.stdout.write(uri)
            self.stdout.write('')
            self.stdout.write(
                'Add it to an authenticator app, then run this command again with '
                '--code <the 6 digits it shows> to confirm.')
            self.stdout.write(self.style.NOTICE(
                'Until you do, the device is UNCONFIRMED and will not satisfy a '
                'sign-in.'))
            return

        # Confirming: find the unconfirmed device created by the previous run.
        device = ops_mfa.pending_totp_device(user)
        if device is None:
            raise CommandError(
                'No unconfirmed device to confirm. Run this command without '
                '--code first to create one.'
            )

        if not ops_mfa.confirm_totp(device, code):
            raise CommandError(
                'That code did not verify. The app may be out of sync, or the '
                'code may have expired -- try again with a fresh one. Nothing has '
                'been changed.'
            )

        codes = ops_mfa.issue_recovery_codes(user)

        # No phone number, no secret, no recovery code. The user id is enough
        # to find the account, and audit rows are retained and searchable.
        # The actor comes from the context opened in handle().
        record_admin_action(
            None,
            action='ops_mfa_enrolled',
            target_type='operator',
            target_id=user.pk,
            after={'method': 'totp', 'recovery_codes_issued': len(codes)},
            reason='Operator second factor enrolled via management command.',
        )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Enrolled. Operator (user id {user.pk}) now requires a code to sign '
            f'in.'))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'RECOVERY CODES -- shown once, single-use. Give these to the operator '
            'to store somewhere safe. They are the only way back in if the '
            'authenticator device is lost, short of a reset from this shell.'))
        self.stdout.write('')
        for c in codes:
            self.stdout.write(f'    {c}')
        self.stdout.write('')
