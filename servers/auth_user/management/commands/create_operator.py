"""Create a NAMED operator account, from a shell, with nothing left behind.

    python manage.py create_operator --email srini@saaradhigo.com \\
        --phone +91XXXXXXXXXX --name "Srinivas" [--enrol-mfa]

WHY THIS EXISTS
---------------
`migrate_operators` refuses to invent a person: an account named in its plan
that does not exist is reported, not created. That is the right rule for a
migration and it means somebody has to create the account deliberately
first. `bootstrap_qa_operator` cannot do it -- it builds the shared QA
account from fixed environment variables and makes it a superuser -- and
the HTTP invite flow sends an email this deployment cannot send yet.

So this: one named person, created by somebody with shell access, which is
the same bar as every other operator-shaping command here.

WHAT THE PERSON RECEIVES, AND WHAT NOBODY KEEPS
-----------------------------------------------
A generated temporary password, printed ONCE to this terminal. It is never
logged, never written to the audit row, and never usable on its own:
`must_change_password` is set, so the first sign-in demands a new password
and issues no token until one is chosen (`AUTH_PASSWORD_CHANGE_REQUIRED`).
After that the operator is the only person who has ever known their
password.

With `--enrol-mfa`, a TOTP provisioning URI is printed as well -- the
shared secret, once, the same way `enroll_operator_mfa` prints it. The
device is created UNCONFIRMED and is confirmed by the first valid code at
sign-in, so the operator completes their own enrolment without a second
shell session. No recovery codes are issued on that path;
`enroll_operator_mfa --code` issues them, and `reset_operator_mfa` is the
way back in if the device is lost.

WHAT IT DOES NOT DO
-------------------
It grants no authority. The account is an operator (`role=admin`,
`is_staff`), never a superuser, and holds no assignment: that is
`migrate_operators`' decision, with a name attached. It never touches an
existing account -- an address or number already in use is refused, because
a silent change to an existing account is the escalation every other guard
here exists to prevent. It does not refuse in production: a production
operator is created exactly this way, deliberately, by a person.

The email address is marked verified. Whoever runs this has shell access to
the deployment, which is a stronger proof of control than a link clicked in
an inbox -- and the reset email that link would come from cannot be sent
from this deployment yet.
"""

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from base import ops_mfa
from base.countries import normalize_phone
from servers.admin_audit.context import audit_context
from servers.auth_user.identity import InvalidEmail, normalize_email

User = get_user_model()

#: 18 random bytes, URL-safe: 24 characters, ~143 bits. Comfortably past
#: every validator and not something a person is expected to keep.
TEMP_PASSWORD_BYTES = 18


class Command(BaseCommand):
    help = ('Create a named operator account with a one-time temporary '
            'password. Grants no authority; never prints anything twice.')

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True,
                            help='The operator\'s own address. Becomes their sign-in.')
        parser.add_argument('--phone', required=True,
                            help='E.164, e.g. +919876543210. Required by the user model.')
        parser.add_argument('--name', required=True,
                            help='Full name, as it should appear in the audit log.')
        parser.add_argument(
            '--enrol-mfa', action='store_true',
            help='Also create a TOTP device and print its provisioning URI. '
                 'The device is confirmed by the first valid code at sign-in.')

    def handle(self, *args, **options):
        try:
            email = normalize_email(options['email'])
        except InvalidEmail as exc:
            raise CommandError(f'That is not a usable email address: {exc}') from exc

        phone = normalize_phone(str(options['phone']).strip())
        if not phone:
            raise CommandError(
                'That phone number could not be normalised to E.164. Give it '
                'with its country code, e.g. +919876543210.'
            )

        name = (options['name'] or '').strip()
        if not name:
            raise CommandError('A name is required: this account is a person.')

        # Never touch an existing account. Reported, not updated.
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(
                f'An account already uses {email}. This command creates '
                f'accounts; it does not change them.'
            )
        if User.objects.filter(phone_number=phone).exists():
            raise CommandError(
                'An account already uses that phone number. This command '
                'creates accounts; it does not change them.'
            )

        temporary = secrets.token_urlsafe(TEMP_PASSWORD_BYTES)

        with transaction.atomic():
            user = User(
                phone_number=phone,
                username=phone,
                email=email,
                email_verified_at=timezone.now(),
                full_name=name,
                role='admin',
                is_staff=True,
                is_superuser=False,
                must_change_password=True,
            )
            user.set_password(temporary)
            # Creating an operator-shaped account appends an audit row by
            # itself (`admin_audit.access_audit`). The context names that
            # row `operator_created` and adds the facts the model cannot
            # see, so there is ONE row for this creation rather than a
            # structural one and a hand-written one saying the same thing.
            # No credential, no secret, no phone number goes in it: the id
            # finds the account, and audit rows are retained and
            # searchable.
            with audit_context(
                actor_label='create_operator (management command)',
                action='operator_created',
                reason='Named operator account created from a shell.',
                extra_after={
                    'must_change_password': True,
                    'email_verified': True,
                    'mfa_enrolment_started': bool(options['enrol_mfa']),
                },
            ):
                user.save()

            uri = None
            if options['enrol_mfa']:
                _device, uri = ops_mfa.enroll_totp(user)

        self.stdout.write(self.style.SUCCESS(
            f'Created operator account (user id {user.pk}) for {email}. '
            f'No authority is attached; assign a role with migrate_operators.'
        ))
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'The lines below are SECRETS, shown once. Give them to the '
            'operator directly and do not paste them anywhere else.'
        ))
        self.stdout.write('')
        self.stdout.write(f'Temporary password: {temporary}')
        if uri:
            self.stdout.write(f'Authenticator setup: {uri}')
        self.stdout.write('')
        self.stdout.write(
            'First sign-in asks for a NEW password before any session exists'
            + (', and for the authenticator code, which confirms the device.'
               if uri else '.')
        )
