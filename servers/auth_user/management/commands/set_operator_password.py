"""Give an existing operator a new generated password, from a shell, audited.

    python manage.py set_operator_password --email srini@saaradhigo.com \\
        --actor-email you@saaradhigo.com \\
        [--remove-pending-mfa] [--clear-forced-change]

WHY THIS EXISTS
---------------
`create_operator` hands over a TEMPORARY password that is only usable
together with a new one at first sign-in, and with `--enrol-mfa` a pending
authenticator that the first valid code confirms. Both are right for a
person completing their own enrolment. They are the wrong shape when the
deployment's first sign-in is being done on the owner's behalf to prove the
console works before anyone enrols: there is no authenticator in hand, and
the forced change has no screen to happen on. The owner's ruling for QA
(2026-10-28): remove the pending device, clear the forced change, set a
strong generated password, every one of those as an audited change.

WHAT IT DOES, AND WHAT IT REFUSES
---------------------------------
It never invents a person and never touches a non-operator: the address
must belong to an existing operator account. It never takes a password as
an argument -- one is generated (24 URL-safe characters, ~143 bits) and
printed ONCE; a password on a command line ends up in shell history. It
revokes the account's other sessions, because a credential that just
changed must not leave old ones alive. It removes only UNCONFIRMED
authenticators, and only when asked: a confirmed factor is the operator's
own proof and `reset_operator_mfa` is the break-glass for that.

Every run leaves one audit row (`ops_password_set`) naming what changed,
and never the password; the session revocation leaves its own
(`operator_credential_changed`), as it does everywhere else.

WHO DID IT
----------
A new password on an operator account is the credential an access review
asks about first, so every run requires `--actor-email`: the operator at
the keyboard, validated by `admin_audit.actor_option` (active, an
operator, not the break-glass account), named on both rows. This command
never creates an account, so it has no genesis path: the first operator is
`create_operator`'s to make.
"""

import secrets

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
from servers.auth_user.identity import InvalidEmail, normalize_email
from servers.auth_user.session_revocation import revoke_after_credential_change

User = get_user_model()

COMMAND = 'set_operator_password'

PASSWORD_BYTES = 18  # 24 characters once URL-safe base64 encoded


class Command(BaseCommand):
    help = ('Set a new GENERATED password on an existing operator account, '
            'printed once. Optionally drops unconfirmed authenticators and '
            'the forced-change flag. Audited.')

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument(
            '--remove-pending-mfa', action='store_true',
            help='Delete UNCONFIRMED TOTP devices on the account. Confirmed '
                 'factors are never touched here (see reset_operator_mfa).')
        parser.add_argument(
            '--clear-forced-change', action='store_true',
            help='Clear must_change_password so the new password signs in '
                 'directly.')
        add_actor_argument(parser)

    def handle(self, *args, **options):
        try:
            email = normalize_email(options['email'])
        except InvalidEmail as exc:
            raise CommandError(f'That is not a usable email address: {exc}') from exc

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            raise CommandError(f'No account uses {email}. This command changes '
                               f'operators; it does not create them.')
        if not is_operator(user):
            raise CommandError(f'User id {user.pk} is not an operator. Refusing: '
                               f'this command is for operator accounts only.')

        # Before the first write.
        actor = require_actor(options, COMMAND)

        password = secrets.token_urlsafe(PASSWORD_BYTES)
        before = {
            'must_change_password': bool(getattr(user, 'must_change_password', False)),
            'pending_totp_devices': self._pending_count(user),
        }
        reason = 'Operator password set from a shell.'

        # Both rows -- this command's and the revocation's -- name the actor
        # through the context; neither names the password.
        with attributed(actor, COMMAND, reason=reason):
            with transaction.atomic():
                user.set_password(password)
                update_fields = ['password']
                if options['clear_forced_change'] and getattr(user, 'must_change_password', False):
                    user.must_change_password = False
                    update_fields.append('must_change_password')
                user.save(update_fields=update_fields)

                removed = 0
                if options['remove_pending_mfa']:
                    removed = self._remove_pending(user)

                record_admin_action(
                    None,
                    action='ops_password_set',
                    target_type='operator',
                    target_id=user.pk,
                    before=before,
                    after={
                        'password': 'generated, shown once in the shell',
                        'must_change_password': bool(getattr(user, 'must_change_password', False)),
                        'pending_totp_devices_removed': removed,
                        'sessions_revoked': True,
                    },
                    reason=reason,
                )

            # After the commit: the password is saved, so nothing here may
            # raise.
            revoke_after_credential_change(
                user, None,
                reason='Password set from a shell by set_operator_password.')

        self.stdout.write(self.style.SUCCESS(
            f'Password set for operator id {user.pk} ({email}). '
            f'Forced change: {"cleared" if "must_change_password" in update_fields else "unchanged"}. '
            f'Pending authenticators removed: {removed}.'
        ))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'The line below is a SECRET, shown once. Hand it over directly.'))
        self.stdout.write('')
        self.stdout.write(f'Password: {password}')
        self.stdout.write('')

    @staticmethod
    def _pending_count(user) -> int:
        return ops_mfa._totp_model().objects.filter(user=user, confirmed=False).count()

    @staticmethod
    def _remove_pending(user) -> int:
        deleted, _ = ops_mfa._totp_model().objects.filter(user=user, confirmed=False).delete()
        return deleted or 0
