"""What runs when a checker approves a Part B finance request.

Three executors, all registered with the generic maker-checker
(servers.approvals); none of them is a per-feature approval:

  * finance.adjust, target type 'driver_balance'  a signed correction on a
    driver's settlement balance (one runner under the shared finance.adjust
    dispatcher, next to the trip fare adjustment);
  * finance.wallet_credit  a closed-loop credit on a rider's wallet;
  * finance.payout_config_change  a new version of a country's payout rules.

Each one re-checks the world under a lock (the target may have changed since
the request was raised), is idempotent on the request id, and writes its own
audit row. The checker is the actor on those rows (the approvals service opens
an audit_context for them).
"""

from django.db import transaction

from base.money import from_minor
from servers.admin_audit.services import record_admin_action
from servers.approvals import adjust
from servers.approvals.actions import FINANCE_PAYOUT_CONFIG, FINANCE_WALLET_CREDIT
from servers.approvals.registry import ExecutionRefused, register

def _jsonable(value):
    import json

    return json.loads(json.dumps(value, default=str))


DRIVER_BALANCE = 'driver_balance'
RIDER_WALLET = 'rider_wallet'
PAYOUT_CONFIG_TARGET = 'payout_config'

ADJUST_DIRECTIONS = ('credit', 'debit')
#: reason_code -> label. `other` needs the note to say what.
ADJUST_REASONS = {
    'correction': 'Correction',
    'goodwill': 'Goodwill',
    'chargeback': 'Chargeback',
    'damage_recovery': 'Damage recovery',
    'other': 'Other',
}
CREDIT_REASONS = {
    'service_recovery': 'Service recovery',
    'goodwill': 'Goodwill',
    'correction': 'Correction',
    'other': 'Other',
}


# ---------------------------------------------------------------------------
# Driver balance adjustment
# ---------------------------------------------------------------------------

@adjust.register(DRIVER_BALANCE)
def execute_driver_adjustment(request_row):
    """Credit or debit a driver's settlement balance; never below zero."""
    from servers.driver.models import Driver
    from servers.rider.models import Wallet, WalletTransaction

    p = request_row.payload or {}
    amount = int(p.get('amount_minor') or 0)
    direction = p.get('direction')
    key = f'adjustment_{request_row.id}'
    if amount <= 0 or direction not in ADJUST_DIRECTIONS:
        raise ExecutionRefused('BAD_ADJUSTMENT', 'The adjustment is not valid.')
    with transaction.atomic():
        driver = Driver.objects.select_related('user_id').filter(pk=p.get('driver_id')).first()
        if driver is None:
            raise ExecutionRefused('DRIVER_NOT_FOUND', 'The driver no longer exists.')
        wallet = (Wallet.objects.select_for_update()
                  .filter(user_id=driver.user_id_id, scope=Wallet.SCOPE_DRIVER).first())
        if wallet is None:
            raise ExecutionRefused('WALLET_NOT_FOUND', 'The driver has no settlement wallet.')
        existing = WalletTransaction.objects.filter(idempotency_key=key).first()
        if existing is not None:
            return {'wallet_transaction_id': existing.id, 'duplicate': True}
        if wallet.currency != p.get('currency'):
            raise ExecutionRefused('CURRENCY_CHANGED', "The wallet's currency changed.")
        if direction == 'debit' and wallet.balance_minor - amount < 0:
            raise ExecutionRefused(
                'BALANCE_WOULD_GO_NEGATIVE',
                'The balance is lower than the debit; a debit cannot take it below zero.')
        before = wallet.balance_minor
        wallet.balance_minor += amount if direction == 'credit' else -amount
        wallet.save(update_fields=['balance_minor'])
        txn = WalletTransaction.objects.create(
            user_id=driver.user_id, amount_minor=amount, currency=wallet.currency,
            txn_type=direction, status='completed', purpose='adjustment',
            reference_id=key, idempotency_key=key)
        resolved = None
        if p.get('withdrawal_id'):
            # Re-checked under the lock: the payout may have been reversed since the request.
            from servers.payments.payout_conflict import check_resolution, resolve_paid

            try:
                check_resolution(driver, p)
            except ValueError as exc:
                raise ExecutionRefused(exc.args[0], exc.args[1])
            resolved = resolve_paid(p['withdrawal_id'], adjustment_txn=txn,
                                    request_id=request_row.id).id
        record_admin_action(
            None, action='driver_balance_adjusted', target_type='driver', target_id=driver.id,
            before={'balance_minor': before},
            after={'balance_minor': wallet.balance_minor, 'direction': direction,
                   'amount_minor': amount, 'currency': wallet.currency,
                   'reason_code': p.get('reason_code'), 'note': p.get('note'),
                   'approval_request_id': request_row.id,
                   'wallet_transaction_id': txn.id, 'resolved_withdrawal_id': resolved},
            reason=request_row.request_reason or p.get('note') or '')
    return {'wallet_transaction_id': txn.id, 'direction': direction,
            'resolved_withdrawal_id': resolved,
            'amount_minor': amount, 'currency': wallet.currency,
            'balance_minor': wallet.balance_minor}


# ---------------------------------------------------------------------------
# Rider wallet credit
# ---------------------------------------------------------------------------

@register(FINANCE_WALLET_CREDIT)
def execute_wallet_credit(request_row):
    """Add the credit to the rider's wallet; the linked ticket is told once."""
    from servers.rider.credits import issue_admin_credit
    from servers.rider.models import Rider, Wallet
    from servers.support.service import post_system_message

    p = request_row.payload or {}
    amount = int(p.get('amount_minor') or 0)
    rider = Rider.objects.select_related('user_id').filter(pk=p.get('rider_id')).first()
    if rider is None:
        raise ExecutionRefused('RIDER_NOT_FOUND', 'The rider no longer exists.')
    wallet = Wallet.objects.filter(user_id=rider.user_id, scope=Wallet.SCOPE_RIDER).first()
    if wallet is None or wallet.currency != p.get('currency'):
        raise ExecutionRefused('CURRENCY_CHANGED', "The wallet's currency changed.")
    with transaction.atomic():
        result = issue_admin_credit(rider.user_id, amount, wallet.currency, request_row.id)
        if not result.ok:
            raise ExecutionRefused(
                f'WALLET_{(result.reason or "refused").upper()}',
                f'The credit was refused ({result.reason}).')
        record_admin_action(
            None, action='rider_wallet_credited', target_type='rider', target_id=rider.id,
            after={'amount_minor': amount, 'currency': wallet.currency,
                   'reason_code': p.get('reason_code'), 'note': p.get('note'),
                   'ticket_id': p.get('ticket_id'), 'approval_request_id': request_row.id,
                   'wallet_transaction_id': result.transaction_id},
            reason=request_row.request_reason or p.get('note') or '')
    ticket_id = p.get('ticket_id')
    if ticket_id:
        # Neutral: nothing about approval, a checker or an internal step.
        post_system_message(
            ticket_id,
            'A credit of %s %s has been added to your wallet.' % (
                from_minor(amount, wallet.currency), wallet.currency),
            key=f'wallet-credit-done:{request_row.id}')
    return {'wallet_transaction_id': result.transaction_id, 'amount_minor': amount,
            'currency': wallet.currency, 'duplicate': result.reason == 'duplicate'}


# ---------------------------------------------------------------------------
# Payout configuration
# ---------------------------------------------------------------------------

@register(FINANCE_PAYOUT_CONFIG)
def execute_payout_config_change(request_row):
    """Write the version the maker proposed and the checker approved."""
    from decimal import Decimal

    from servers.payments.models import PayoutCountryConfig

    from .payout_config_write import write_config_version

    p = request_row.payload or {}
    country = p.get('country')
    values = dict(p.get('values') or {})
    values['fee_percent'] = Decimal(str(values['fee_percent']))
    with transaction.atomic():
        active = (PayoutCountryConfig.objects.select_for_update()
                  .filter(country=country, is_active=True).first())
        seen = p.get('base_version')
        if (active.version if active else None) != seen:
            raise ExecutionRefused(
                'CONFIG_CHANGED',
                'The payout settings changed after this request was raised; raise it again.')
        snap = {'actor_id': request_row.maker_id, 'actor_label': request_row.maker_label,
                'actor_email': request_row.maker_email, 'actor_role': request_row.maker_role}
        row, superseded = write_config_version(
            country, values, reason=request_row.request_reason, snapshot=snap)
        record_admin_action(
            None, action='payout_config_changed', target_type='payout_config', target_id=row.id,
            before=_jsonable(superseded.as_dict()) if superseded else {},
            after=_jsonable(row.as_dict()),
            reason=request_row.decision_reason or request_row.request_reason)
    return {'config_id': row.id, 'version': row.version,
            'superseded_id': superseded.id if superseded else None}
