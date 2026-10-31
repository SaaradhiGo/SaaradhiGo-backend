"""What runs when a checker approves. Two executors ship: payouts and policy.

THE PAYOUT EXECUTOR IS THE OLD ENDPOINT BODY, MOVED
---------------------------------------------------
`approve_withdrawal_admin` used to flip the withdrawal to `approved` under
a row lock, write the `withdrawal_approved` audit row, and then call
`trigger_payout_creation` outside the lock so the Cashfree round-trip did
not hold it. That is exactly what `execute_payout` does, in that order,
with the same lock and the same audit row -- the only change is WHO it runs
as: the checker, after a different finance admin asked. The external payout
call has not moved: it is still made once, here, only on approval.

The executor re-checks `status == 'pending'` under the lock. Between the
request and the approval the withdrawal may have been rejected, or paid
through some other path; the request then fails with
`WITHDRAWAL_NOT_PENDING` and no money moves, rather than approving a row
that is no longer waiting.

A batch (the bulk endpoint) is the same executor over several ids: each is
locked, checked and flipped on its own, the ones that are no longer pending
are reported as skipped, and the payouts are dispatched after all the
flips, as the bulk endpoint always did.
"""

import hashlib
import logging

from django.db import transaction
from django.utils import timezone

from base.countries import COUNTRIES
from servers.admin_audit.services import record_admin_action

from .actions import FINANCE_PAYOUT, POLICY_CHANGE
from .registry import ExecutionRefused, register

logger = logging.getLogger(__name__)

#: ISO currency -> the country whose local currency it is. A withdrawal with
#: no home city is placed by the currency of the wallet it is paid from.
_COUNTRY_OF_CURRENCY = {c.currency: c.code for c in COUNTRIES.values()}


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------

def payout_country(withdrawal):
    """The country a withdrawal's threshold is read from.

    The driver's home city decides when there is one (money follows the
    driver, as `scoping` says); otherwise the currency the wallet holds.
    """
    home = getattr(withdrawal.driver, 'home_city', None)
    if home is not None:
        country = home.country
        if country is not None:
            return country.code
    return _COUNTRY_OF_CURRENCY.get(str(withdrawal.currency or '').upper())


def withdrawal_key(withdrawal_id):
    return f'withdrawal:{withdrawal_id}:approve'


def batch_key(withdrawal_ids):
    ids = ','.join(str(i) for i in sorted(int(i) for i in withdrawal_ids))
    digest = hashlib.sha256(ids.encode()).hexdigest()[:32]
    return f'withdrawal_batch:{digest}'


def _approve_one(withdrawal_id, reason):
    """Lock, re-check, flip, audit. Returns the row, or None if not pending."""
    from servers.driver.models import WithdrawalRequest

    with transaction.atomic():
        withdrawal = (
            WithdrawalRequest.objects.select_for_update()
            .select_related('driver', 'driver__user_id')
            .filter(id=withdrawal_id).first()
        )
        if withdrawal is None or withdrawal.status != 'pending':
            return None
        before = {'status': withdrawal.status, 'amount': str(withdrawal.amount)}
        withdrawal.status = 'approved'
        withdrawal.processed_at = timezone.now()
        withdrawal.save(update_fields=['status', 'processed_at'])
        # Actor comes from the audit_context the service opened: the checker.
        record_admin_action(
            None,
            action='withdrawal_approved',
            target_type='withdrawal_request',
            target_id=withdrawal.id,
            before=before,
            after={'status': 'approved', 'amount': str(withdrawal.amount)},
            reason=reason,
        )
    return withdrawal


@register(FINANCE_PAYOUT)
def execute_payout(request):
    """Release the withdrawal(s) named in the request's payload."""
    from servers.driver.services import trigger_payout_creation

    payload = request.payload or {}
    ids = payload.get('withdrawal_ids')
    single = ids is None
    if single:
        ids = [payload.get('withdrawal_id')]
    reason = request.decision_reason or request.request_reason or 'maker-checker approval'

    approved, skipped = [], []
    for wid in ids:
        row = _approve_one(wid, reason)
        (approved if row is not None else skipped).append(row if row is not None else wid)

    if single and not approved:
        raise ExecutionRefused(
            'WITHDRAWAL_NOT_PENDING',
            f'Withdrawal {ids[0]} is no longer pending; nothing was released.',
        )

    # Outside every lock, exactly as the endpoint did before.
    triggered, failed, held = [], [], []
    for withdrawal in approved:
        try:
            trigger_payout_creation(withdrawal)
            # No rail for the currency: the trigger parked it (payout_status
            # awaiting_rail) without a gateway call. Held, not failed.
            if withdrawal.payout_status == 'awaiting_rail':
                held.append(withdrawal.id)
            else:
                triggered.append(withdrawal.id)
        except Exception as exc:  # noqa: BLE001 - the gateway hook owns retries
            logger.error('approvals: trigger_payout_creation failed for withdrawal %s: %s',
                         withdrawal.id, exc)
            failed.append(withdrawal.id)

    return {
        'approved_ids': [w.id for w in approved],
        'skipped_ids': skipped,
        'payout_triggered_ids': triggered,
        'payout_trigger_failed_ids': failed,
        'payout_held_awaiting_rail_ids': held,
    }


# ---------------------------------------------------------------------------
# Policy change
# ---------------------------------------------------------------------------

@register(POLICY_CHANGE)
def execute_policy_change(request):
    """Write the policy version the maker proposed and the checker approved."""
    from .service import write_policy_version

    p = request.payload or {}

    # `created_by` is the MAKER as snapshotted on the request: the person
    # whose proposal this is. The checker is reachable through
    # `changed_via`, which is the request itself.
    policy, superseded = write_policy_version(
        country=p['country'], action_key=p['action_key'],
        limit_kind=p['limit_kind'], currency=p.get('currency') or '',
        maker_limit=p.get('maker_limit'),
        created_by_snapshot={
            'actor_id': request.maker_id, 'actor_label': request.maker_label,
            'actor_email': request.maker_email, 'actor_role': request.maker_role,
        },
        reason=request.request_reason, changed_via=request,
    )
    record_admin_action(
        None,
        action='approval_policy_changed',
        target_type='approval_policy',
        target_id=policy.id,
        before=superseded.as_dict() if superseded else {},
        after=policy.as_dict(),
        reason=request.decision_reason or request.request_reason,
    )
    return {
        'policy_id': policy.id,
        'version': policy.version,
        'superseded_policy_id': superseded.id if superseded else None,
    }
