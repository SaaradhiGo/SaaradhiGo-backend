"""`email_safety_check` must tell the truth, and send nothing.

WHY THIS IS TESTED AT ALL
-------------------------
The command exists so that someone can confirm QA is safe *before* the SES
credentials go in. A diagnostic that reports "safe" when it is not is worse
than no diagnostic, because it is believed.

Its first version had exactly that fault: on a console backend it printed
"YES, it would be SENT" for any address, because `recipient_allowed()`
exempts backends that cannot reach anyone. Read literally, it said the
allowlist was not working. These tests pin the three distinct answers apart.
"""

import pytest
from django.core import mail
from django.core.management import call_command

SMTP = 'django.core.mail.backends.smtp.EmailBackend'
CONSOLE = 'django.core.mail.backends.console.EmailBackend'
LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'


@pytest.fixture(autouse=True)
def _outbox():
    mail.outbox = []


def run(**kwargs):
    """Run the command and return its output."""
    from io import StringIO

    out = StringIO()
    call_command('email_safety_check', stdout=out, **kwargs)
    return out.getvalue()


def configure(settings, *, environment, backend, allowlist, sender='no-reply@test',
              host='email-smtp.ap-south-1.amazonaws.com'):
    settings.ENVIRONMENT = environment
    settings.EMAIL_BACKEND = backend
    settings.EMAIL_HOST = host
    settings.DEFAULT_FROM_EMAIL = sender
    settings.EMAIL_HOST_PASSWORD = 'not-a-real-password'
    settings.AUTH_EMAIL_ALLOWLIST = allowlist


class TestItSendsNothing:
    def test_no_message_is_composed_whatever_is_asked(self, settings):
        configure(settings, environment='qa', backend=LOCMEM,
                  allowlist=['qa@deeptrics.com'])

        run(addresses=['qa@deeptrics.com', 'anyone@example.test'])

        assert mail.outbox == [], 'the check must never send'


class TestItNeverPrintsTheCredential:
    def test_the_smtp_password_is_not_in_the_output(self, settings):
        configure(settings, environment='qa', backend=SMTP, allowlist=[])
        settings.EMAIL_HOST_PASSWORD = 'BAbcdef1234567890SuperSecret'

        output = run()

        assert 'BAbcdef1234567890SuperSecret' not in output
        assert 'EMAIL_HOST_PASSWORD   : set' in output

    def test_an_unset_password_is_reported_as_unset(self, settings):
        configure(settings, environment='qa', backend=SMTP, allowlist=[])
        settings.EMAIL_HOST_PASSWORD = ''

        assert '(unset)' in run()


class TestTheEmptyAllowlistIsReportedAsSafe:
    def test_qa_with_ses_live_and_an_empty_allowlist_blocks_everyone(
            self, settings):
        # The headline requirement: an empty QA allowlist blocks all outbound
        # email, and the command says so in those words.
        configure(settings, environment='qa', backend=SMTP, allowlist=[])

        output = run(addresses=['real.customer@gmail.com'])

        assert 'AUTH_EMAIL_ALLOWLIST  : EMPTY' in output
        assert 'no address may be emailed' in output
        assert 'real.customer@gmail.com: NO' in output
        assert 'allowed to email nobody' in output

    def test_an_unset_environment_is_still_treated_as_non_production(
            self, settings):
        configure(settings, environment='', backend=SMTP, allowlist=[])

        output = run(addresses=['real.customer@gmail.com'])

        assert 'NON-production' in output
        assert 'real.customer@gmail.com: NO' in output


class TestAnAllowlistedAddressIsReportedAsReachable:
    def test_it_says_YES_only_when_the_message_really_would_leave(
            self, settings):
        configure(settings, environment='qa', backend=SMTP,
                  allowlist=['@deeptrics.com'])

        output = run(addresses=['qa.lead@deeptrics.com',
                                'real.customer@gmail.com'])

        assert 'qa.lead@deeptrics.com: YES, it would be SENT' in output
        assert 'real.customer@gmail.com: NO' in output

    def test_a_domain_entry_is_labelled_as_a_whole_domain(self, settings):
        configure(settings, environment='qa', backend=SMTP,
                  allowlist=['@deeptrics.com', 'one@saaradhigo.in'])

        output = run()

        assert '@deeptrics.com   (whole domain)' in output
        assert 'one@saaradhigo.in   (exact address)' in output


class TestABackendThatCannotReachAnyone:
    @pytest.mark.parametrize('backend', [CONSOLE, LOCMEM])
    def test_it_does_not_claim_a_message_would_be_sent(self, settings, backend):
        # The original fault. `recipient_allowed()` exempts these backends
        # because nothing leaves the machine; reporting that as "YES, it would
        # be SENT" read as "the allowlist is broken".
        configure(settings, environment='qa', backend=backend, allowlist=[])

        output = run(addresses=['real.customer@gmail.com'])

        assert 'YES, it would be SENT' not in output
        assert 'not delivered' in output
        assert 'nothing this deployment composes can reach a real inbox' in output

    def test_it_says_the_allowlist_is_exempt_rather_than_enforced(
            self, settings):
        configure(settings, environment='qa', backend=CONSOLE, allowlist=[])

        assert 'protect nobody' in run()


class TestProductionIsReportedAsUnrestricted:
    def test_production_can_email_anyone_and_the_output_is_blunt(self, settings):
        configure(settings, environment='production', backend=SMTP,
                  allowlist=[])

        output = run(addresses=['real.customer@gmail.com'])

        assert 'treated as            : production' in output
        assert 'not enforced' in output
        assert 'can email ANY address' in output


class TestAnUnconfiguredDeployment:
    def test_it_reports_that_nothing_can_be_sent_and_why(self, settings):
        # This is QA today: no mail configuration at all.
        configure(settings, environment='qa', backend=SMTP, allowlist=[],
                  sender='')

        output = run()

        assert 'can this deployment send? NO' in output
        assert 'DEFAULT_FROM_EMAIL' in output

    def test_smtp_on_localhost_is_reported_as_unable_to_send(self, settings):
        # Django's default, and what the stack fell back to for months.
        configure(settings, environment='qa', backend=SMTP, allowlist=[],
                  host='localhost')

        assert 'EMAIL_HOST' in run()
