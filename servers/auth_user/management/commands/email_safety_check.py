"""Report what a deployment would do with outbound email. Sends nothing.

WHY
---
QA is about to be pointed at real AWS SES. The safeguard that stops a test
deployment emailing a real customer is `AUTH_EMAIL_ALLOWLIST`, and its safe
default is the empty list -- which blocks everything. That default is only
reassuring if someone can confirm it is in force *before* the SMTP
credentials go in, and until now the only way to find out what a deployment
would do was to make it try.

The QA backend service currently has no email variables at all: no
EMAIL_BACKEND, no EMAIL_HOST, no DEFAULT_FROM_EMAIL, no
AUTH_EMAIL_ALLOWLIST. So every receipt and every password-reset email has
been failing into `Receipt.send_failure_reason` and the log. Turning that on
is a sequence of four or five variables, and getting the order wrong -- SMTP
credentials before the allowlist -- is the one mistake that cannot be undone,
because the mail has already left.

WHAT IT DOES
------------
Prints the effective configuration and the decision the gate in
`base/auth_email.py` would make, for the deployment it is run on. With
`--to`, it also answers the only question an operator actually has: would a
message to this address leave the building?

SAFETY
------
  * Sends no mail. It calls `recipient_allowed()` and
    `delivery_is_configured()`, which are pure checks, and never `_send()`.
  * Never prints EMAIL_HOST_PASSWORD or any other credential. The SMTP user
    and host are printed because an operator needs to confirm which SES
    endpoint is configured, and those are not secrets.
  * Read-only: touches no database row and no cache key.

USAGE
    python manage.py email_safety_check
    python manage.py email_safety_check --to qa.driver@deeptrics.com
    python manage.py email_safety_check --to real.customer@gmail.com
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from base.auth_email import (
    backend_leaves_the_machine,
    delivery_is_configured,
    recipient_allowed,
)


class Command(BaseCommand):
    help = 'Report what this deployment would do with outbound email. Sends nothing.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--to',
            action='append',
            default=[],
            dest='addresses',
            help='An address to test the allowlist against. Repeatable.',
        )

    def handle(self, *args, **options):
        out = self.stdout
        ok = self.style.SUCCESS
        warn = self.style.WARNING
        bad = self.style.ERROR

        environment = str(getattr(settings, 'ENVIRONMENT', '') or '')
        backend = str(getattr(settings, 'EMAIL_BACKEND', '') or '')
        host = str(getattr(settings, 'EMAIL_HOST', '') or '')
        port = getattr(settings, 'EMAIL_PORT', '')
        user = str(getattr(settings, 'EMAIL_HOST_USER', '') or '')
        sender = str(getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '')
        has_password = bool(getattr(settings, 'EMAIL_HOST_PASSWORD', ''))
        allowlist = list(getattr(settings, 'AUTH_EMAIL_ALLOWLIST', []) or [])

        out.write('')
        out.write(self.style.MIGRATE_HEADING('Deployment'))
        out.write(f'  ENVIRONMENT           : {environment or "(unset)"}')
        treated_as = 'production' if environment.lower() == 'production' else 'NON-production'
        out.write(f'  treated as            : {treated_as}')
        if environment.lower() != 'production':
            out.write('                          (anything that is not exactly '
                      '"production" is non-production, so an unset or '
                      'misspelled value fails safe)')

        out.write('')
        out.write(self.style.MIGRATE_HEADING('Delivery'))
        out.write(f'  EMAIL_BACKEND         : {backend or "(unset)"}')
        out.write(f'  EMAIL_HOST            : {host or "(unset)"}')
        out.write(f'  EMAIL_PORT            : {port}')
        out.write(f'  EMAIL_HOST_USER       : {user or "(unset)"}')
        out.write(f'  EMAIL_HOST_PASSWORD   : {"set" if has_password else "(unset)"}'
                  '   <- never printed')
        out.write(f'  DEFAULT_FROM_EMAIL    : {sender or "(unset)"}')

        configured, reason = delivery_is_configured()
        external = backend_leaves_the_machine()
        if configured and external:
            out.write(ok('  can this deployment send? YES, to the outside world'))
        elif configured and not external:
            out.write(ok('  can this deployment send? only to its own log '
                         '(console/locmem) -- nothing reaches a real inbox'))
        else:
            out.write(warn(f'  can this deployment send? NO -- {reason}'))
            out.write('                          (every send is logged at ERROR '
                      'as auth_email_undeliverable and reports failure rather '
                      'than claiming success)')

        out.write('')
        out.write(self.style.MIGRATE_HEADING('Allowlist'))
        if environment.lower() == 'production':
            out.write('  not enforced: production is supposed to email customers.')
        elif not external:
            out.write('  exempt: this backend prints or discards, so an '
                      'allowlist would protect nobody.')
        elif not configured:
            out.write('  moot while nothing can be sent, but it would be enforced.')
        if not allowlist:
            out.write(ok('  AUTH_EMAIL_ALLOWLIST  : EMPTY'))
            if environment.lower() != 'production' and external:
                out.write(ok('  -> no address may be emailed. This is the safe '
                             'default, not a misconfiguration.'))
        else:
            out.write(f'  AUTH_EMAIL_ALLOWLIST  : {len(allowlist)} entr'
                      f'{"y" if len(allowlist) == 1 else "ies"}')
            for entry in allowlist:
                kind = 'whole domain' if entry.startswith('@') else 'exact address'
                out.write(f'      {entry}   ({kind})')

        addresses = options['addresses']
        if addresses:
            out.write('')
            out.write(self.style.MIGRATE_HEADING('Would a message to these leave?'))
            for address in addresses:
                allowed, why = recipient_allowed(address)
                if not allowed:
                    out.write(ok(f'  {address}: NO -- {why}'))
                elif not external:
                    # The allowlist exempts console/locmem precisely because
                    # they cannot reach anyone. Saying "YES, it would be SENT"
                    # here was the opposite of reassuring.
                    out.write(ok(f'  {address}: no -- the message would be '
                                 'printed to the service log, not delivered'))
                elif not configured:
                    out.write(warn(f'  {address}: allowed, but nothing can be '
                                   f'sent -- {reason}'))
                else:
                    out.write(bad(f'  {address}: YES, it would be SENT'))

        out.write('')
        if not external:
            out.write(ok('Verdict: nothing this deployment composes can reach '
                         'a real inbox -- the backend prints or discards. Safe '
                         'by construction, and NOT a configuration to ship.'))
        elif environment.lower() != 'production' and configured and not allowlist:
            out.write(ok('Verdict: this deployment can send, and is allowed to '
                         'email nobody. Safe to hold SMTP credentials.'))
        elif environment.lower() != 'production' and configured and allowlist:
            out.write(warn('Verdict: this deployment can send, to the '
                           'allowlisted addresses above and no others. Check '
                           'that list is what you intended.'))
        elif environment.lower() == 'production' and configured:
            out.write(bad('Verdict: this deployment can email ANY address. '
                          'That is correct for production and wrong for '
                          'anything else.'))
        else:
            out.write('Verdict: this deployment cannot send email at all.')
        out.write('')
