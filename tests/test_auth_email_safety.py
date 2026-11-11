"""Test email must never reach a real customer.

THE ACCIDENT THIS PREVENTS
--------------------------
QA gets pointed at real SES. QA's database holds a real address — restored
from a dump, or typed into a test account by someone being helpful. Somebody
exercises password reset. A reset link for a live account lands in that
person's inbox.

SES sandbox mode restricts recipients too, but that is an AWS account state
nobody in this repo controls or can assert from a test. `AUTH_EMAIL_ALLOWLIST`
is the copy we own.

The rule: production is unrestricted; anywhere else, a *delivering* backend
may only write to the allowlist, and an empty allowlist blocks everything.
Console and locmem are exempt because nothing leaves the machine.
"""

import pytest
from django.core import mail

from base.auth_email import recipient_allowed, send_password_reset

pytestmark = pytest.mark.django_db

SMTP = 'django.core.mail.backends.smtp.EmailBackend'
LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'
CONSOLE = 'django.core.mail.backends.console.EmailBackend'


class _User:
    """Just enough user for the sender."""

    def __init__(self, email):
        self.email = email


def _configure(settings, *, environment, backend, allowlist):
    settings.ENVIRONMENT = environment
    settings.EMAIL_BACKEND = backend
    settings.EMAIL_HOST = 'smtp.test'
    settings.DEFAULT_FROM_EMAIL = 'no-reply@test'
    settings.AUTH_EMAIL_ALLOWLIST = allowlist


class TestRecipientAllowed:
    def test_production_may_email_anyone(self, settings):
        _configure(settings, environment='production', backend=SMTP,
                   allowlist=[])
        ok, _ = recipient_allowed('a-real-customer@gmail.com')
        assert ok is True

    def test_qa_with_an_empty_allowlist_may_email_nobody(self, settings):
        # The important default. "We do not yet know who we may write to"
        # resolves to nobody, not to everybody.
        _configure(settings, environment='qa', backend=SMTP, allowlist=[])
        ok, reason = recipient_allowed('a-real-customer@gmail.com')
        assert ok is False
        assert 'AUTH_EMAIL_ALLOWLIST is empty' in reason

    def test_qa_allows_an_exact_address(self, settings):
        _configure(settings, environment='qa', backend=SMTP,
                   allowlist=['qa@deeptrics.com'])
        assert recipient_allowed('qa@deeptrics.com')[0] is True
        assert recipient_allowed('QA@Deeptrics.COM')[0] is True

    def test_qa_allows_a_whole_domain_with_an_at_prefix(self, settings):
        _configure(settings, environment='qa', backend=SMTP,
                   allowlist=['@saaradhigo.com'])
        assert recipient_allowed('anyone@saaradhigo.com')[0] is True
        assert recipient_allowed('qa.rider@saaradhigo.com')[0] is True

    def test_a_domain_entry_does_not_match_a_lookalike(self, settings):
        # '@saaradhigo.com' must not match 'evil-saaradhigo.com' or
        # 'saaradhigo.com.example.com'.
        _configure(settings, environment='qa', backend=SMTP,
                   allowlist=['@saaradhigo.com'])
        assert recipient_allowed('x@evilsaaradhigo.com')[0] is False
        assert recipient_allowed('x@saaradhigo.com.example.com')[0] is False

    def test_a_non_allowlisted_address_is_refused_with_a_reason(self, settings):
        _configure(settings, environment='qa', backend=SMTP,
                   allowlist=['qa@deeptrics.com'])
        ok, reason = recipient_allowed('a-real-customer@gmail.com')
        assert ok is False
        assert 'not in AUTH_EMAIL_ALLOWLIST' in reason

    @pytest.mark.parametrize('backend', [CONSOLE, LOCMEM])
    def test_non_delivering_backends_are_exempt(self, settings, backend):
        # Blocking these would make local development and the whole test
        # suite useless, and nothing leaves the machine either way.
        _configure(settings, environment='qa', backend=backend, allowlist=[])
        assert recipient_allowed('anyone@example.test')[0] is True

    def test_an_unset_environment_is_treated_as_not_production(self, settings):
        # A deployment that forgot to set ENVIRONMENT must not inherit
        # production's freedom to email anyone.
        _configure(settings, environment='', backend=SMTP, allowlist=[])
        assert recipient_allowed('a-real-customer@gmail.com')[0] is False


class TestSendIsBlocked:
    @pytest.fixture(autouse=True)
    def _outbox(self):
        mail.outbox = []

    def test_a_reset_to_a_non_allowlisted_address_is_not_sent(self, settings):
        # End to end through the real sender, not just the predicate.
        _configure(settings, environment='qa', backend=LOCMEM,
                   allowlist=['qa@deeptrics.com'])
        # locmem is exempt, so force the delivering path for this assertion.
        settings.EMAIL_BACKEND = SMTP

        sent = send_password_reset(
            user=_User('a-real-customer@gmail.com'), uid='u', token='t')

        assert sent is False
        assert mail.outbox == []

    def test_a_reset_to_an_allowlisted_address_is_composed(self, settings):
        _configure(settings, environment='qa', backend=LOCMEM,
                   allowlist=['qa@deeptrics.com'])

        sent = send_password_reset(
            user=_User('qa@deeptrics.com'), uid='u', token='t')

        assert sent is True
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ['qa@deeptrics.com']

    def test_production_is_unaffected(self, settings):
        _configure(settings, environment='production', backend=LOCMEM,
                   allowlist=[])

        sent = send_password_reset(
            user=_User('a-real-customer@gmail.com'), uid='u', token='t')

        assert sent is True
        assert len(mail.outbox) == 1
