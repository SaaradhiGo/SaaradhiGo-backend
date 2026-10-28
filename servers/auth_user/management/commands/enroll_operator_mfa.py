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

--replace: THE NEW FACTOR IS PROVED BEFORE THE OLD ONE GOES
-----------------------------------------------------------
Re-enrolling an operator who already has a factor used to work by deleting their
devices first and creating the new one second. Between those two steps -- and for
ever, if the operator never managed to scan the new secret -- the account had no
factor at all, which in an enforced environment is a locked-out operator and in an
unenforced one is an operator account with MFA silently off.

Now the order is the other way round. `--replace` without `--code` creates the new
device ALONGSIDE the old ones and prints its URI; the old factors keep working.
`--replace --code` confirms the new device with the code FIRST, and only then
removes the old devices and reissues recovery codes. If the code does not verify,
nothing is removed. The account therefore never has zero confirmed factors at any
point, and the command checks that invariant before it commits: a path that would
leave an operator who must present a factor with none to present is refused and
rolled back.

Both halves are audited: `ops_mfa_enrolled` for the new factor and
`ops_mfa_factors_replaced` for the removal, each naming the operator who ran this
(`--actor-email`, see `admin_audit.actor_option`).
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

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
ENROLLED_ACTION = 'ops_mfa_enrolled'
REPLACED_ACTION = 'ops_mfa_factors_replaced'


class ZeroFactorsRefused(CommandError):
    """The run would have left an operator who must present a factor with none."""


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
            help='Re-enrol an account that already has a factor. The existing '
                 'factors keep working until the new device is confirmed with '
                 '--code; only then are they removed. Required on BOTH runs, so '
                 'a second run cannot silently orphan a device the operator is '
                 'still using.')
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
                f'Re-run with --replace to enrol a new one; the current factors '
                f'keep working until the new device is confirmed, and are '
                f'removed then.'
            )

        # Every path from here writes: before the first write.
        actor = require_actor(options, COMMAND)
        with attributed(actor, COMMAND,
                        reason='Operator second factor enrolled via management command.'):
            if not options['code']:
                self._start(user, existing)
            else:
                self._confirm(user, existing, options['code'], options['replace'])

    # -- the two runs ------------------------------------------------------

    def _start(self, user, existing):
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
        if existing:
            self.stdout.write(self.style.NOTICE(
                f'The {len(existing)} existing factor(s) still work. They are '
                f'removed only when the new device is confirmed with '
                f'--replace --code.'))

    def _confirm(self, user, existing, code, replace):
        device = ops_mfa.pending_totp_device(user)
        if device is None:
            raise CommandError(
                'No unconfirmed device to confirm. Run this command without '
                '--code first to create one.'
            )

        # Decided BEFORE anything changes: once the old factors are gone,
        # has_device() would answer differently in an unenforced environment.
        must_keep_a_factor = ops_mfa.mfa_required_for(user)

        if not ops_mfa.confirm_totp(device, code):
            raise CommandError(
                'That code did not verify. The app may be out of sync, or the '
                'code may have expired -- try again with a fresh one. Nothing '
                'has been changed'
                + (', and the existing factors are untouched.' if existing else '.')
            )

        with transaction.atomic():
            removed = 0
            if replace and existing:
                removed = ops_mfa.remove_other_devices(user, keep=device)
            codes = ops_mfa.issue_recovery_codes(user)

            remaining = ops_mfa.devices_for(user)
            if must_keep_a_factor and not any(
                    isinstance(d, type(device)) and d.pk == device.pk for d in remaining):
                # Cannot happen by construction -- the new device was confirmed
                # before anything was removed -- but the invariant is the point,
                # so it is checked rather than assumed, and the removal is
                # rolled back if it ever fails.
                raise ZeroFactorsRefused(
                    'Refusing: this would leave an operator who must present a '
                    'second factor with no confirmed factor. Nothing was removed.'
                )

            # No phone number, no secret, no recovery code. The user id is
            # enough to find the account, and audit rows are retained and
            # searchable. The actor comes from the context opened in handle().
            record_admin_action(
                None,
                action=ENROLLED_ACTION,
                target_type='operator',
                target_id=user.pk,
                after={'method': 'totp', 'recovery_codes_issued': len(codes),
                       'replaced_existing': bool(replace and existing)},
                reason='Operator second factor enrolled via management command.',
            )
            if replace and existing:
                record_admin_action(
                    None,
                    action=REPLACED_ACTION,
                    target_type='operator',
                    target_id=user.pk,
                    before={'confirmed_factors': len(existing)},
                    after={'devices_removed': removed,
                           'confirmed_factors': len(remaining)},
                    reason=('Previous factors removed after the replacement '
                            'device was confirmed. The account held at least '
                            'one confirmed factor throughout.'),
                )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Enrolled. Operator (user id {user.pk}) now requires a code to sign '
            f'in.'))
        if removed:
            self.stdout.write(self.style.WARNING(
                f'Removed {removed} previous factor(s); the old authenticator '
                f'entry and the old recovery codes no longer work.'))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'RECOVERY CODES -- shown once, single-use. Give these to the operator '
            'to store somewhere safe. They are the only way back in if the '
            'authenticator device is lost, short of a reset from this shell.'))
        self.stdout.write('')
        for c in codes:
            self.stdout.write(f'    {c}')
        self.stdout.write('')
