"""Has this driver told us where to send their money?

A withdrawal or payout with nowhere to go is a request nobody can complete:
it sits in the queue, a checker approves it, and the gateway step fails. The
owner's rule (2026-11-03): refuse it at the start and tell the driver why.

"Somewhere" is the payout run's own rule (`weekly_payout.has_payout_handle`:
an active UPI contact or a bank account) plus the UPI ID the driver typed into
their profile, which is what the payout step turns into a contact.
"""

MISSING_CODE = 'PAYOUT_DETAILS_MISSING'
MISSING_MESSAGE = 'Add your UPI ID or bank account in Payout details before you withdraw.'
#: What an operator sees on a payout that cannot be sent.
MISSING_FOR_OPERATORS = 'No payout details on file'


def has_payout_destination(driver):
    from servers.payments.weekly_payout import has_payout_handle

    if (getattr(driver, 'upi_id', None) or '').strip():
        return True
    return has_payout_handle(driver)


def destination_label(driver):
    """Where a payout goes, masked for the confirmation: "HDFC Bank ••1234" or "UPI ••kaxis".
    None when nothing is on file. Never the full account number or UPI ID."""
    from servers.driver.models import DriverBankAccount

    bank = DriverBankAccount.objects.filter(driver=driver).order_by('-id').first()
    if bank is not None and bank.account_number:
        return f'{(bank.bank_name or "Bank").strip()} \u2022\u2022{bank.account_number[-4:]}'
    upi = (getattr(driver, 'upi_id', None) or '').strip()
    if upi:
        return f'UPI \u2022\u2022{upi[-5:]}'
    return None
