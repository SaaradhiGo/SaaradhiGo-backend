"""Erase a rider's or driver's identifiers in place, for a DPDP/CCPA request.

    python manage.py anonymise_user --phone +91XXXXXXXXXX \\
        --reason "DPDP erasure request" --reference DSR-2026-0142 --confirm

    python manage.py anonymise_user --email someone@example.com \\
        --reason "..." --reference "..." --actor-email ops@saaradhigo.com --confirm

WHY A SHELL COMMAND AND NOT AN ENDPOINT
---------------------------------------
Erasure is irreversible and the request that justifies it arrives out of
band -- an email, a ticket, a letter. The person handling it should be
reading that request while they type the reference in, and the result should
be visible in the audit log with their name on it. A button in the console
would make it one misclick from a wiped account; this is deliberately slower.
The self-service path for a signed-in rider (`/api/v1/auth/me/delete/`)
calls the same service and names the person themselves as the actor.

WHAT IT REFUSES
---------------
An operator or staff account, because staff are deactivated and never
anonymised (the audit log must keep naming them). A run with no `--reason`
or no `--reference`. Anything without `--confirm`, which reports what it
would do and changes nothing.

Without `--actor-email` the row is attributed to this command by label, with
no actor id -- a visible gap. Give the email of the operator doing it so the
row names a person.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from servers.auth_user.erasure import AnonymisationRefused, anonymise_user

User = get_user_model()
ACTOR_LABEL = 'anonymise_user (management command)'


class Command(BaseCommand):
    help = ("Anonymise a rider or driver account in place for an erasure "
            "request. Never deletes; refuses operators.")

    def add_arguments(self, parser):
        who = parser.add_mutually_exclusive_group(required=True)
        who.add_argument('--phone', help='Phone number of the account.')
        who.add_argument('--email', help='Email of the account.')
        parser.add_argument(
            '--reason', required=True,
            help='Why. Recorded verbatim on the audit row.')
        parser.add_argument(
            '--reference', required=True,
            help='The erasure request reference (ticket id, DSR number). '
                 'Recorded on the audit row so it can be matched to the '
                 'request later.')
        parser.add_argument(
            '--actor-email', default='',
            help='Email of the operator carrying this out. The audit row '
                 'names them; without it the row is attributed to this '
                 'command only.')
        parser.add_argument(
            '--confirm', action='store_true',
            help='Required. Without it the command reports what it WOULD do '
                 'and changes nothing, because this cannot be undone.')

    def handle(self, *args, **options):
        if not options['reason'].strip():
            raise CommandError('--reason must not be blank.')
        if not options['reference'].strip():
            raise CommandError('--reference must not be blank.')

        user = self._find(options)
        actor = self._actor(options['actor_email'])

        from base.permissions import is_operator
        if user.is_staff or user.is_superuser or is_operator(user):
            raise CommandError(
                f'User id {user.pk} is a staff/operator account. Staff are '
                f'deactivated (is_active=False), never anonymised or deleted. '
                f'Nothing has been changed.'
            )

        if not options['confirm']:
            self.stdout.write(self.style.WARNING(
                f'Would anonymise user id {user.pk} (role {user.role}, '
                f'active={user.is_active}) under reference '
                f'{options["reference"].strip()!r}. Re-run with --confirm '
                f'to do it. Nothing has been changed.'
            ))
            return

        try:
            row = anonymise_user(
                user,
                reason=options['reason'],
                reference=options['reference'],
                actor=actor,
                # Names the command when nobody was named; the actor's own
                # label wins when there is one.
                actor_label='' if actor is not None else ACTOR_LABEL,
            )
        except AnonymisationRefused as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f'Anonymised user id {user.pk}: {len(row.after["fields_cleared"])} '
            f'field(s) cleared, {row.after["refresh_tokens_revoked"]} '
            f'session(s) revoked. Audit row {row.pk}.'
        ))
        if row.after['storage_failures']:
            self.stderr.write(self.style.WARNING(
                'Some files could not be removed from storage (the fields '
                'were cleared regardless): '
                + ', '.join(row.after['storage_failures'])
            ))

    def _find(self, options):
        if options['phone']:
            lookup = {'phone_number': options['phone'].strip()}
        else:
            lookup = {'email__iexact': options['email'].strip()}
        try:
            return User.objects.get(**lookup)
        except User.DoesNotExist:
            raise CommandError('No account matches.')
        except User.MultipleObjectsReturned:
            raise CommandError('More than one account matches; use --phone.')

    def _actor(self, email):
        email = (email or '').strip()
        if not email:
            return None
        from base.permissions import is_operator
        actor = User.objects.filter(email__iexact=email).first()
        if actor is None or not is_operator(actor):
            raise CommandError(
                f'--actor-email {email!r} does not name an operator account. '
                f'Nothing has been changed.'
            )
        return actor
