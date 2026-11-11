"""Receipt email goes out through the same gate as everything else.

THE GAP THIS CLOSES
-------------------
`servers/ride/receipts.py` built its own `EmailMultiAlternatives` and called
`send()` on it. So the one message type that goes to *customers* answered to
neither safeguard the auth emails go through:

  * `delivery_is_configured()`. With no mail configuration Django defaults to
    SMTP on localhost:25, which does not exist on Railway. Every receipt send
    raised, the exception was caught, and the reason was written into
    `Receipt.send_failure_reason` -- a column nobody reads. That is why the
    column is full.

  * `AUTH_EMAIL_ALLOWLIST`. "An empty QA allowlist blocks all outbound email"
    was simply false for receipts. QA pointed at real SES, with a real
    customer's address in a restored dump, would have emailed that person a
    GST receipt for somebody's test trip.

A DELIVERING BACKEND THAT IS SAFE TO TEST WITH
----------------------------------------------
The allowlist deliberately exempts console and locmem -- nothing leaves the
machine, and enforcing it there would make local development and this suite
useless. That exemption also makes locmem useless for testing the allowlist
itself. `_DeliveringBackend` below is locmem under a different import path:
captured in `mail.outbox`, but not recognised as exempt, so it is treated
exactly as SES would be. That is what lets these tests assert the real
behaviour of a delivering deployment without one.
"""

import logging

import pytest
from django.core import mail
from django.core.mail.backends import locmem

from servers.ride.receipts import _send_receipt_email

SMTP = 'django.core.mail.backends.smtp.EmailBackend'
LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'
DELIVERING = f'{__name__}._DeliveringBackend'


class _DeliveringBackend(locmem.EmailBackend):
    """Captures like locmem; treated as a real sender by the allowlist."""


class _FakePdf:
    """Just enough of a FieldFile for the attachment path."""

    def __init__(self, content=b'%PDF-1.4 fake'):
        self._content = content
        self.opened = False
        self.closed = False

    def __bool__(self):
        return True

    def open(self, mode='rb'):
        self.opened = True

    def read(self):
        return self._content

    def close(self):
        self.closed = True


class _FakeReceipt:
    """The attributes _send_receipt_email actually touches."""

    def __init__(self, *, email='rider@example.test', pdf=True):
        self.id = 1
        self.receipt_number = 'SG-2026-000123'
        self.sent_to_email = email
        self.html_body = '<html><body>Your receipt</body></html>'
        self.pdf_file = _FakePdf() if pdf else None


@pytest.fixture(autouse=True)
def _outbox():
    mail.outbox = []


def _configure(settings, *, environment, backend, allowlist, sender='no-reply@test'):
    settings.ENVIRONMENT = environment
    settings.EMAIL_BACKEND = backend
    settings.EMAIL_HOST = 'smtp.test'
    settings.DEFAULT_FROM_EMAIL = sender
    settings.AUTH_EMAIL_ALLOWLIST = allowlist


class TestTheAllowlistGovernsReceipts:
    def test_an_empty_qa_allowlist_blocks_a_receipt(self, settings):
        # The headline requirement. This passed silently before, because the
        # allowlist was never consulted on this path.
        _configure(settings, environment='qa', backend=DELIVERING, allowlist=[])

        sent, reason = _send_receipt_email(_FakeReceipt())

        assert sent is False
        assert 'blocked' in reason
        assert mail.outbox == []

    def test_a_non_allowlisted_address_is_blocked(self, settings):
        _configure(settings, environment='qa', backend=DELIVERING,
                   allowlist=['qa@deeptrics.com'])

        sent, reason = _send_receipt_email(
            _FakeReceipt(email='real.customer@gmail.com'))

        assert sent is False
        assert 'blocked' in reason
        assert mail.outbox == []

    def test_an_allowlisted_address_receives_the_receipt(self, settings):
        _configure(settings, environment='qa', backend=DELIVERING,
                   allowlist=['qa@deeptrics.com'])

        sent, reason = _send_receipt_email(
            _FakeReceipt(email='qa@deeptrics.com'))

        assert sent is True
        assert reason == ''
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ['qa@deeptrics.com']

    def test_a_whole_domain_entry_works_for_receipts_too(self, settings):
        _configure(settings, environment='qa', backend=DELIVERING,
                   allowlist=['@deeptrics.com'])

        sent, _ = _send_receipt_email(_FakeReceipt(email='qa2@deeptrics.com'))

        assert sent is True

    def test_production_still_emails_customers(self, settings):
        # The safeguard must not break the thing it is guarding.
        _configure(settings, environment='production', backend=DELIVERING,
                   allowlist=[])

        sent, reason = _send_receipt_email(
            _FakeReceipt(email='real.customer@gmail.com'))

        assert sent is True, reason
        assert len(mail.outbox) == 1


class TestTheReceiptItself:
    def test_the_html_and_the_pdf_both_survive_the_move(self, settings):
        _configure(settings, environment='production', backend=DELIVERING,
                   allowlist=[])
        receipt = _FakeReceipt()

        sent, _ = _send_receipt_email(receipt)

        assert sent is True
        message = mail.outbox[0]
        assert message.subject.endswith(receipt.receipt_number)
        assert message.alternatives == [
            (receipt.html_body, 'text/html'),
        ]
        assert message.attachments == [
            (f'{receipt.receipt_number}.pdf', b'%PDF-1.4 fake',
             'application/pdf'),
        ]
        assert receipt.pdf_file.closed, 'the PDF file handle was left open'

    def test_a_missing_pdf_still_sends_the_html_receipt(self, settings):
        # No PDF is not a reason to send no receipt.
        _configure(settings, environment='production', backend=DELIVERING,
                   allowlist=[])

        sent, _ = _send_receipt_email(_FakeReceipt(pdf=False))

        assert sent is True
        assert mail.outbox[0].attachments == []
        assert mail.outbox[0].alternatives

    def test_no_address_is_reported_unchanged(self, settings):
        # issue_receipt writes this string into Receipt.send_failure_reason;
        # support searches for it.
        _configure(settings, environment='production', backend=DELIVERING,
                   allowlist=[])

        assert _send_receipt_email(_FakeReceipt(email='')) == (False, 'no_email')


class TestAnUnconfiguredDeployment:
    def test_a_deployment_with_no_sender_reports_why(self, settings, caplog):
        # This is QA today: no DEFAULT_FROM_EMAIL. It used to attempt a send
        # to localhost:25 and record the socket error.
        _configure(settings, environment='production', backend=SMTP,
                   allowlist=[], sender='')

        with caplog.at_level(logging.ERROR):
            sent, reason = _send_receipt_email(_FakeReceipt())

        assert sent is False
        assert 'undeliverable' in reason
        assert 'DEFAULT_FROM_EMAIL' in reason
        assert mail.outbox == []
        assert 'auth_email_undeliverable' in caplog.text, \
            'the failure must reach the log stream, not just a DB column'

    def test_smtp_pointed_at_localhost_is_refused_before_connecting(
            self, settings):
        # Django's default. Refused explicitly rather than discovered through
        # a swallowed socket error 30 seconds later.
        _configure(settings, environment='production', backend=SMTP,
                   allowlist=[])
        settings.EMAIL_HOST = 'localhost'

        sent, reason = _send_receipt_email(_FakeReceipt())

        assert sent is False
        assert 'EMAIL_HOST' in reason

    def test_the_failure_reason_fits_the_column(self, settings):
        # Receipt.send_failure_reason is bounded; a 512-char cap is asserted
        # here so a verbose backend error cannot break the save.
        _configure(settings, environment='qa', backend=DELIVERING,
                   allowlist=[])

        _sent, reason = _send_receipt_email(_FakeReceipt())

        assert len(reason) <= 512


class TestThereIsOnlyOneWayOut:
    def test_no_module_outside_the_gate_sends_mail(self):
        """A structural guard, because this is a rule about the whole repo.

        The defect was not that the receipt message was wrong -- it was that
        it had its own way out. Any new sender that bypasses
        `base.auth_email.send_message` reintroduces exactly this bug, and a
        reviewer would have to notice. This notices instead.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        senders = re.compile(
            r'EmailMultiAlternatives\s*\(|EmailMessage\s*\(|send_mail\s*\(')
        skip_dirs = {'.venv-local', '.venv', 'tests', 'qa', 'node_modules',
                     '.git', 'staticfiles', 'media'}

        offenders = []
        for path in root.rglob('*.py'):
            if any(part in skip_dirs for part in path.parts):
                continue
            if path == root / 'base' / 'auth_email.py':
                continue  # the gate itself
            if 'migrations' in path.parts:
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            # Ignore the words inside comments and docstrings: what matters is
            # a call, and a call is not indented behind a `#`.
            for line_number, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith('#') or stripped.startswith('*'):
                    continue
                if senders.search(line):
                    offenders.append(
                        f'{path.relative_to(root)}:{line_number}: {stripped}')

        assert offenders == [], (
            'these modules send email without going through '
            'base.auth_email.send_message, so AUTH_EMAIL_ALLOWLIST and the '
            'delivery check do not apply to them:\n  '
            + '\n  '.join(offenders)
        )
