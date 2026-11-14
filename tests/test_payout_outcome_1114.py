"""14 Nov (owner): a rejected or failed payout tells the driver why, in one line, and where the
money went ("returned" only when the refund credit is on the wallet)."""
import pytest
from servers.driver.models import Driver, WithdrawalRequest
from servers.rider.models import WalletTransaction


def _make(db_driver, status, **kw):
    return WithdrawalRequest.objects.create(driver=db_driver, amount_minor=60000, currency='INR', status=status, **kw)


@pytest.mark.django_db
def test_outcome_rejected_returned_and_failed_held(auth_client_driver):
    c, user = auth_client_driver
    driver = Driver.objects.get(user_id=user)
    rej = _make(driver, 'rejected', admin_notes='UPI ID did not match the\nname on the account')
    WalletTransaction.objects.create(user_id=driver.user_id, amount=600, txn_type='credit', status='completed',
                                     purpose='refund_rejected_withdrawal', reference_id=f'refund_rejected_withdrawal_{rej.id}',
                                     idempotency_key=f'refund_rejected_withdrawal_{rej.id}')
    failed = _make(driver, 'failed', failure_reason='Bank account closed')
    ok = _make(driver, 'completed')
    res = c.get('/api/v1/driver/withdrawals/history/')
    assert res.status_code == 200
    rows = {r['id']: r for r in res.json()['data']['results']}
    assert rows[rej.id]['outcome'] == {'reason': 'UPI ID did not match the name on the account', 'money': 'returned'}
    assert rows[failed.id]['outcome'] == {'reason': 'Bank account closed', 'money': 'held'}
    assert rows[ok.id]['outcome'] is None
