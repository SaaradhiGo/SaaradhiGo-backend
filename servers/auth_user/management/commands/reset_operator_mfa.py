"""Break-glass: replace an operator's lost second factor with a fresh enrolment.

    python manage.py reset_operator_mfa --phone +91XXXXXXXXXX --confirm \\
        --actor-email you@saaradhigo.com \\
        --reason "lost handset, identity verified by voice call"

THE RULING (owner, 2026-10-28)
------------------------------
    Accepted as the break-glass path, with --actor-email and a reason
    required, audited. When ADMIN_MFA_REQUIRED=true, an account with zero
    factors can do nothing except enrol at next login.

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

WHAT A RESET DOES, IN ORDER
---------------------------
1. Removes every confirmed factor on the account: the authenticator entry and
   the recovery codes. Nothing the lost phone held works any more.
2. Creates a fresh UNCONFIRMED TOTP device and prints its provisioning URI
   once -- exactly what `create_operator --enrol-mfa` does. The next sign-in
   asks for the code from the new authenticator, and the first valid code
   confirms the device (`admin_views.admin_login`). The operator completes
   their own re-enrolment at the keyboard; no second shell session is
   needed, and nobody but them ever scans the secret.
3. Revokes every session the account holds (`session_revocation`): a
   refresh token minted before the reset can never be exchanged again.
   An access token already in flight expires within its 15 minutes, the
   documented bound; see `tests/test_credential_change_session_revocation`.

So, with `ADMIN_MFA_REQUIRED=true`, the account can do nothing except
enrol at next login: a password alone is answered `AUTH_MFA_REQUIRED` and
issues no token, the old session cannot be renewed, and the only code the
sign-in accepts is one from the new device. With the switch off (QA
today) the password signs in as before and the pending device is simply
ignored until the switch goes on -- nothing here depends on the switch.

WHAT IT DOES NOT DO
-------------------
It does not sign anybody in, weaken the requirement, or leave the account
exempt. It removes the broken factor; it does not remove the rule.

WHO DID IT, AND WHY
-------------------
Removing a factor is the one thing an account-takeover needs most, so
`--confirm` requires both `--actor-email` -- the operator at the keyboard,
validated by `admin_audit.actor_option` and named on every row -- and
`--reason`, at least 12 characters, the same bar as the break-glass
sign-in (`admin_access.breakglass.MIN_REASON_LENGTH`). "No reason given"
written by the system would look like a record while recording nothing.
Without `--confirm` the command only reports, and needs neither.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from base import ops_mfa
from servers.admin_access.breakglass import MIN_REASON_LENGTH
from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)
from servers.admin_audit.services import record_admin_action
from servers.auth_user.session_revocation import revoke_after_credential_change

User = get_user_model()

COMMAND = 'reset_operator_mfa'
ACTION = 'ops_mfa_reset'


def validate_reason(reason):
    """The cleaned reason, or `CommandError`. Same rule as the break-glass
    sign-in: a reason is a sentence, not a flag."""
    cleaned = (reason or '').strip()
    if len(cleaned) < MIN_REASON_LENGTH:
        raise CommandError(
            f'--reason is required with --confirm and must be at least '
            f'{MIN_REASON_LENGTH} characters: say what happened ("lost '
            f'handset, identity verified by voice call"). Nothing was changed.'
        )
    return cleaned


class Command(BaseCommand):
    help = ("Replace an operator's lost second factor: remove every factor, "
            "start a fresh enrolment, revoke their sessions. Audited.")

    def add_arguments(self, parser):
        parser.add_argument('--phone', required=True)
        parser.add_argument(
            '--confirm', action='store_true',
            help='Required. Without it the command reports what it WOULD remove '
                 'and changes nothing, because an accidental reset signs an '
                 'operator out everywhere until they re-enrol.')
        parser.add_argument(
            '--reason', default='',
            help=f'Required with --confirm; at least {MIN_REASON_LENGTH} '
                 f'characters, recorded on the audit row. Say what happened -- '
                 f'"lost handset, verified by voice call" is the kind of thing '
                 f'an auditor needs six months later.')
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
                f'User id {user.pk} has no confirmed factor. Nothing to remove; '
                f'`enroll_operator_mfa` starts an enrolment from scratch.')
            return

        if not options['confirm']:
            self.stdout.write(self.style.WARNING(
                f'Would remove {len(devices)} factor(s) from user id {user.pk}, '
                f'start a fresh enrolment and revoke their sessions. Re-run '
                f'with --confirm, --actor-email and --reason to do it.'))
            return

        # Both before the first write: a usage error, then the actor.
        reason = validate_reason(options['reason'])
        actor = require_actor(options, COMMAND)

        # Factors, not rows: `remove_devices` counts the recovery-code rows
        # it cascades into, and "removed 12 factors" for one authenticator
        # and ten codes would misreport what happened.
        removed = len(devices)
        with attributed(actor, COMMAND, reason=reason):
            ops_mfa.remove_devices(user)
            _device, uri = ops_mfa.enroll_totp(user)

            # No phone number and no secret: the user id identifies the
            # account and audit rows are retained and searchable. The actor
            # comes from the context.
            record_admin_action(
                None,
                action=ACTION,
                target_type='operator',
                target_id=user.pk,
                before={'devices': len(devices)},
                after={
                    'devices': 0,
                    'factors_removed': removed,
                    'pending_totp_device_created': True,
                    'sessions_revoked': True,
                },
                reason=reason,
            )

            # The factors are gone; nothing here may raise. Leaves its own
            # row (`operator_credential_changed`), named through the context.
            revoke_after_credential_change(
                user, None,
                reason=f'Second factor reset from a shell by {COMMAND}; every '
                       f'session revoked.')

        self.stdout.write(self.style.SUCCESS(
            f'Removed {removed} factor(s) from user id {user.pk} and revoked '
            f'their sessions. A fresh authenticator is pending.'))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'The line below is a SECRET, shown once. Give it to the operator '
            'directly and do not paste it anywhere else.'))
        self.stdout.write('')
        self.stdout.write(f'Authenticator setup: {uri}')
        self.stdout.write('')
        self.stdout.write(
            'Their next sign-in asks for the code from the new authenticator; '
            'the first valid code confirms it. Until then the account can do '
            'nothing else.')
