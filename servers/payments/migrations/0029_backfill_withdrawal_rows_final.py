"""Backfill (AUDIT §35, item 18 reversed): withdrawal debit rows left `pending` although their
payout is final. Paid -> completed; failed, rejected or reversed -> failed (their reversal row
already returned the money). The balance does not move: `ledger.counted` counts a withdrawal
debit in all three states. Rows of withdrawals still in flight are left as they are."""
from django.db import migrations

PAID = 'completed'
FAILED = ('failed', 'rejected', 'reversed')


def forwards(apps, schema_editor):
    WalletTransaction = apps.get_model('rider', 'WalletTransaction')
    WithdrawalRequest = apps.get_model('driver', 'WithdrawalRequest')
    pending = WalletTransaction.objects.filter(purpose='withdrawal', txn_type='debit', status='pending')
    ids = {}
    for row_id, ref in pending.values_list('id', 'reference_id'):
        parts = (ref or '').split('_')
        if len(parts) == 2 and parts[1].isdigit():
            ids.setdefault(int(parts[1]), []).append(row_id)
    for w_id, status in WithdrawalRequest.objects.filter(pk__in=list(ids)).values_list('id', 'status'):
        if status == PAID:
            WalletTransaction.objects.filter(pk__in=ids[w_id]).update(status='completed')
        elif status in FAILED:
            WalletTransaction.objects.filter(pk__in=ids[w_id]).update(status='failed')


class Migration(migrations.Migration):
    dependencies = [
        ('payments', '0028_withdrawal_row_pending_kind'),
        ('rider', '0021_favoriteplace_name'),
        ('driver', '0040_mvag_2025_driver_records'),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
