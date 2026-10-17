"""Authentication emails: reset a password, verify an address.

WHY THIS IS NOT `send_mail` AT THE CALL SITE
--------------------------------------------
Email delivery is **not configured in this deployment**. There is no
`EMAIL_BACKEND`, `EMAIL_HOST` or `DEFAULT_FROM_EMAIL` in settings and none in
the QA environment's variables, so Django falls back to its default: SMTP on
`localhost:25`, which does not exist on Railway. Receipt email has the same
gap and fails silently today — the Receipt row is written, `send_failure_reason`
is populated, and nobody notices because nobody is watching that column.

A password reset cannot be allowed to fail that quietly. If the mail does not
leave, the user is waiting for a link that will never arrive, and the only
clue is a field in a database row.

So this module:

  * returns a **boolean** that says whether the message was handed to a
    working backend, which the views log;
  * logs at **ERROR** with a named event when delivery is impossible, so it
    appears in the Railway log stream rather than only in a column;
  * **refuses to report success** for a send that cannot have happened.

It deliberately does NOT raise. A failed reset email must not 500 the
endpoint, because the response is identical for every outcome by design (see
`password_views.FORGOT_MESSAGE`) and a 500 would itself be an enumeration
signal.

LINKS POINT AT THE FRONTEND, NOT THE API
----------------------------------------
`FRONTEND_URL` is already set in the QA environment and is where a human
should land. The token is carried in the query string for the page to post
back to `/auth/password/reset/`. If `FRONTEND_URL` is unset the mail still
goes out with a bare token and an instruction, which is ugly but recoverable
— better than no mail.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection

logger = logging.getLogger(__name__)

#: Backends that genuinely discard the message.
#:
#: Only `dummy` qualifies. `locmem` was here too at first, which was wrong on
#: reflection: locmem *captures* the message in `mail.outbox`, it is the
#: standard Django test backend, and it cannot be reached by accident — a
#: deployment has to set EMAIL_BACKEND to it explicitly AND set
#: DEFAULT_FROM_EMAIL. Treating it as undeliverable made every test that
#: asserts a message was composed fail, which is a sign the rule was drawn in
#: the wrong place.
#:
#: The guard's real teeth are the two checks below — SMTP with no host, and no
#: From address — because those are the live QA situation: no mail
#: configuration at all, Django defaulting to localhost:25, and receipts
#: failing silently.
_NON_DELIVERING_BACKENDS = (
    'django.core.mail.backends.dummy.EmailBackend',
)


def _from_address():
    """The From: address, or '' when nothing is explicitly configured.

    Deliberately does NOT fall back to `SERVER_EMAIL`. Django defaults that
    to 'root@localhost', which is non-empty, so falling back made
    `delivery_is_configured()` answer True on a deployment with no mail
    configuration at all -- the precise false reassurance this module exists
    to prevent.
    """
    return getattr(settings, 'DEFAULT_FROM_EMAIL', '') or ''


def delivery_is_configured():
    """Can this deployment actually send a message?

    Checked explicitly rather than discovered through a swallowed exception,
    so the reason appears in the log once and clearly.
    """
    backend = getattr(settings, 'EMAIL_BACKEND', '')
    if not backend:
        return False, 'EMAIL_BACKEND is not set'
    if backend in _NON_DELIVERING_BACKENDS:
        # Correct for tests and local development. Reported as "not
        # delivering" so nothing upstream claims a message was sent.
        return False, f'EMAIL_BACKEND is {backend.rsplit(".", 2)[-2]}'
    if backend.endswith('console.EmailBackend'):
        # A developer reading stdout IS delivery, for their purposes. Reported
        # as configured so local flows work end to end, and named in the log
        # so nobody mistakes a console deployment for a mailing one.
        logger.info('auth_email_console_backend — messages print to stdout')
        return True, ''
    if backend.endswith('smtp.EmailBackend'):
        host = getattr(settings, 'EMAIL_HOST', '') or ''
        if not host or host in ('localhost', '127.0.0.1'):
            return False, 'EMAIL_HOST is unset or localhost'
    if not _from_address():
        return False, 'DEFAULT_FROM_EMAIL is not set'
    return True, ''


def recipient_allowed(address):
    """May this deployment write to this address? Returns (ok, reason).

    Production is unrestricted -- it is supposed to email customers.

    Everywhere else, a delivering backend may only write to
    `settings.AUTH_EMAIL_ALLOWLIST`. The accident being prevented is concrete:
    QA pointed at real SES, a real customer's address in QA's database, and a
    password-reset link for a live account landing in that person's inbox.

    An EMPTY allowlist on a delivering non-production deployment blocks
    everything. That is the deliberate direction: the safe default for "we do
    not yet know who we may write to" is nobody, and the refusal is logged
    with the reason so it cannot be mistaken for a broken mailer.

    Console and locmem backends are exempt -- nothing leaves the machine, and
    blocking them would make local development and the test suite useless.
    """
    environment = str(getattr(settings, 'ENVIRONMENT', '') or '').lower()
    if environment == 'production':
        return True, ''

    backend = getattr(settings, 'EMAIL_BACKEND', '') or ''
    if (
        backend.endswith('console.EmailBackend')
        or backend.endswith('locmem.EmailBackend')
        or backend in _NON_DELIVERING_BACKENDS
    ):
        return True, ''

    allowlist = [a.lower() for a in getattr(settings, 'AUTH_EMAIL_ALLOWLIST', [])]
    if not allowlist:
        return False, (
            f'environment={environment or "unset"} is not production and '
            'AUTH_EMAIL_ALLOWLIST is empty, so no address may be emailed'
        )

    target = str(address or '').strip().lower()
    if not target:
        return False, 'no recipient'

    for entry in allowlist:
        if entry.startswith('@'):
            if target.endswith(entry):
                return True, ''
        elif target == entry:
            return True, ''

    return False, (
        f'recipient is not in AUTH_EMAIL_ALLOWLIST (environment='
        f'{environment or "unset"})'
    )


def _frontend_link(path, uid, token):
    base = (getattr(settings, 'FRONTEND_URL', '') or '').rstrip('/')
    if not base:
        return ''
    return f'{base}{path}?uid={uid}&token={token}'


def send_message(*, to, subject, body, html=None, attachments=None):
    """Hand one message to the configured backend. Returns (sent, reason).

    THE ONE WAY OUT
    ---------------
    This is the only place in the codebase that may call `message.send()`.
    Receipt email used to build its own `EmailMultiAlternatives` in
    `servers/ride/receipts.py` and send it directly, which meant it was
    governed by neither `delivery_is_configured()` nor
    `AUTH_EMAIL_ALLOWLIST`. The consequence was not theoretical: QA pointed
    at real SES, with a real customer address in a restored dump, would have
    emailed that person a GST receipt for a test trip. "An empty QA allowlist
    blocks all outbound email" is only true if every sender comes through
    here.

    `attachments` is a list of (filename, content, mimetype) tuples, which is
    what a receipt PDF needs. Never raises; the reason is returned so a caller
    with somewhere to record it (Receipt.send_failure_reason) can.
    """
    allowed, why = recipient_allowed(to)
    if not allowed:
        # The address is NOT logged, for the same reason as below.
        logger.warning(
            'auth_email_blocked reason=%s subject=%r — the message was NOT '
            'sent. Set AUTH_EMAIL_ALLOWLIST to permit a test recipient.',
            why, subject,
        )
        return False, f'blocked: {why}'

    ok, reason = delivery_is_configured()
    if not ok:
        # The address is NOT logged. An auth log that lists the addresses it
        # failed to reach is an account list.
        logger.error(
            'auth_email_undeliverable reason=%s subject=%r — the message was '
            'NOT sent. Configure EMAIL_BACKEND/EMAIL_HOST/DEFAULT_FROM_EMAIL.',
            reason, subject,
        )
        return False, f'undeliverable: {reason}'

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=_from_address(),
            to=[to],
            connection=get_connection(fail_silently=False),
        )
        if html:
            message.attach_alternative(html, 'text/html')
        for attachment in attachments or ():
            message.attach(*attachment)
        message.send(fail_silently=False)
        return True, ''
    except Exception as exc:  # noqa: BLE001
        logger.error(
            'auth_email_send_failed subject=%r error=%s: %s',
            subject, type(exc).__name__, exc,
        )
        return False, f'{type(exc).__name__}: {exc}'[:512]


def _send(*, to, subject, body, html=None):
    """Boolean form, for the auth flows whose callers only log success."""
    sent, _reason = send_message(to=to, subject=subject, body=body, html=html)
    return sent


def send_password_reset(*, user, uid, token):
    """A link to set a new password. Returns True if it was sent."""
    brand = getattr(settings, 'PLATFORM_BRAND_NAME', 'SaaradhiGo')
    link = _frontend_link('/reset-password', uid, token)
    minutes = int(getattr(settings, 'PASSWORD_RESET_TIMEOUT', 7200)) // 60

    body = [
        f'Someone asked to reset the password for your {brand} account.',
        '',
    ]
    if link:
        body += [f'Set a new password here:\n{link}', '']
    else:
        # No FRONTEND_URL. Ugly but usable, and far better than no mail.
        body += [
            'Use these values in the app to set a new password:',
            f'  uid:   {uid}',
            f'  token: {token}',
            '',
        ]
    body += [
        f'The link expires in about {minutes} minutes and can be used once.',
        'If you did not ask for this, you can ignore this email — your '
        'password has not changed.',
    ]

    return _send(
        to=user.email,
        subject=f'Reset your {brand} password',
        body='\n'.join(body),
    )


def send_email_verification(*, user, uid, token):
    """A link that proves the recipient controls the address."""
    brand = getattr(settings, 'PLATFORM_BRAND_NAME', 'SaaradhiGo')
    link = _frontend_link('/verify-email', uid, token)

    body = [
        f'Confirm this email address for your {brand} account.',
        '',
    ]
    if link:
        body += [f'Confirm here:\n{link}', '']
    else:
        body += [
            'Use these values in the app to confirm:',
            f'  uid:   {uid}',
            f'  token: {token}',
            '',
        ]
    body += [
        'Until it is confirmed, this address cannot be used to reset your '
        'password.',
    ]

    return _send(
        to=user.email,
        subject=f'Confirm your email for {brand}',
        body='\n'.join(body),
    )


def send_account_invite(*, user, uid, token, role_label, invited_by):
    """An operator-created account's first set-password link.

    No password is generated, transmitted or stored anywhere. The recipient
    chooses one through the reset flow, which is also what verifies their
    address — so an invited account is never left with a password someone
    else has seen.
    """
    brand = getattr(settings, 'PLATFORM_BRAND_NAME', 'SaaradhiGo')
    link = _frontend_link('/reset-password', uid, token)

    body = [
        f'An account has been created for you on {brand} as {role_label}.',
        f'Invited by: {invited_by}',
        '',
    ]
    if link:
        body += [f'Choose your password here:\n{link}', '']
    else:
        body += [
            'Use these values in the app to choose a password:',
            f'  uid:   {uid}',
            f'  token: {token}',
            '',
        ]
    body += ['This link can be used once.']

    return _send(
        to=user.email,
        subject=f'Your {brand} account',
        body='\n'.join(body),
    )
