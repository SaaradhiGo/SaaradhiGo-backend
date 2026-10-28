"""Break-glass: remove an operator's second factor so they can enrol a new one.

    python manage.py reset_operator_mfa --phone +91XXXXXXXXXX --confirm \\
        --actor-email you@saaradhigo.com --reason "lost handset, verified by voice"

WHEN THIS IS THE RIGHT ANSWER
-----------------------------
A lost, wiped or replaced phone, with the recovery codes also gone. In a pilot with
one or two operators that is an outage of the entire operations function -- nobody can
approve a driver, resolve a stale ride, or release a payout -- so there has to be a
way back that does not involve a code deploy.

WHY IT IS A SHELL COMMAND AND NOT A LINK IN AN EMAIL
----------------------------------------------------
Any self-service MFA reset is, by construction, a way to take over the account it
protects. A reset link sent to an operator's email moves the whole security boundary
onto that mailbox. A reset that only runs from a shell on the deployment is
unreachable from the internet, and whoever can run it can already read the database.

That does mean the recovery path depends on someone having deployment access. That is
a real operational dependency and it is recorded as such rather than hidden: see the
release-gate runbook.

WHAT IT DOES NOT DO
-------------------
It does not sign anybody in, weaken the requirement, or leave the account exempt. In
an enforced environment the operator still cannot sign in until they enrol again --
this removes the broken factor, it does not remove the rule.

WHO DID IT
----------
Removing a factor is the one thing an account-takeover needs most, so `--confirm`
requires `--actor-email`: the operator at the keyboard, validated by
`admin_audit.actor_option`, named on the audit row. Without `--confirm` the command
only reports, and needs no name.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from base import ops_mfa
from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)
from servers.admin_audit.services import record_admin_action

User = get_user_model()

COMMAND = 'reset_operator_mfa'


class Command(BaseCommand):
    help = "Remove an operator's MFA devices so a new one can be enrolled."

    def add_arguments(self, parser):
        parser.add_argument('--phone', required=True)
        parser.add_argument(
            '--confirm', action='store_true',
            help='Required. Without it the command reports what it WOULD remove '
                 'and changes nothing, because an accidental reset locks an '
                 'operator out until somebody re-enrols them.')
        parser.add_argument(
            '--reason', default='',
            help='Recorded in the audit row. Say what happened -- "lost handset, '
                 'verified by voice call" is the kind of thing an auditor needs '
                 'six months later.')
        add_actor_argument(parser)

    def handle(self, *args, **options):
        phone = options['phone'].strip()
        try:
            user = User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            raise CommandError('No account with that phone number.')

        devices = ops_mfa.devices_for(user)
        if not devices:
            self.stdout.write(
                f'User id {user.pk} has no confirmed factor. Nothing to remove.')
            return

        if not options['confirm']:
            self.stdout.write(self.style.WARNING(
                f'Would remove {len(devices)} factor(s) from user id {user.pk}. '
                f'Re-run with --confirm to do it.'))
            self.stdout.write(
                'The operator will not be able to sign in until '
                '`enroll_operator_mfa` has been run for them again.')
            return

        reason = (options['reason'] or
                  'Operator MFA reset via management command (no reason given).')

        # Before the first write.
        actor = require_actor(options, COMMAND)
        with attributed(actor, COMMAND, reason=reason):
            removed = ops_mfa.remove_devices(user)

            # No phone number: the user id identifies the account and audit
            # rows are retained and searchable. The actor comes from the
            # context.
            record_admin_action(
                None,
                action='ops_mfa_reset',
                target_type='operator',
                target_id=user.pk,
                before={'devices': len(devices)},
                after={'devices': 0},
                reason=reason,
            )

        self.stdout.write(self.style.SUCCESS(
            f'Removed {removed} factor(s) from user id {user.pk}.'))
        self.stdout.write(self.style.WARNING(
            'They cannot sign in until you run `enroll_operator_mfa --phone ...` '
            'for them.'))
