"""Payout actions: approve, reject, bulk, retry a failed payout, mark under investigation.

WHERE THIS CAME FROM
--------------------
Approve, reject and bulk used to live in servers/driver/admin_views.py under
/driver/admin/withdrawals/*. They moved here, under /payments/admin/payouts/...,
and the old URLs still answer through the same functions (the views there are
thin wrappers), so there is exactly one implementation.

THE RULES
---------
  * Scope: the driver's HOME CITY, through servers.payments.scope (fetch_one):
    out of scope is a 404 identical to a missing row, in scope without
    finance.payout at that place is a 403.
  * Approve and bulk approve never flip anything here: they go through the
    generic maker-checker (require_approval_or_execute, FINANCE_PAYOUT, one
    request per batch with withdrawal_ids). A country with no policy is
    blocked with THRESHOLD_NOT_CONFIGURED; the console says so plainly.
  * Reject needs a reason, returns the funds exactly once and is audited.
  * Retry sends an already-approved payout that failed back to the existing
    trigger. The money was approved already, so no new approval is raised; it
    is idempotent (row lock plus a status check) and audited.
  * Mark under investigation changes a note and a flag only. No money moves.
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import status

from base.utils import error_response, success_response
from servers.admin_audit.services import record_admin_action
from servers.driver.models import WithdrawalRequest

from .scope import fetch_one

logger = logging.getLogger(__name__)

PAYOUT = 'finance.payout'
BULK_MAX = 50
NOTE_LIMIT = 2000


class ActionError(Exception):
    """A refusal with a stable code; the view turns it into the error envelope."""

    def __init__(self, code, message, *, field='general', http=status.HTTP_400_BAD_REQUEST):
        super().__init__(message)
        self.code, self.message, self.field, self.http = code, message, field, http

    def response(self):
        return error_response(code=self.code, message=self.message, field=self.field,
                              issue=self.message, status=self.http)


def _not_found(withdrawal_id):
    return ActionError('NOT_FOUND', 'Withdrawal request not found', field='withdrawal_id',
                       http=status.HTTP_404_NOT_FOUND)


def reason_from(data, *, required=True):
    """The reason text: `reason`, or the old endpoints' `admin_notes`."""
    raw = data.get('reason') or data.get('admin_notes') or ''
    text = str(raw).strip()
    if required and not text:
        raise ActionError('REASON_REQUIRED', 'A reason is required.', field='reason')
    if len(text) > NOTE_LIMIT:
        raise ActionError('REASON_TOO_LONG', f'The reason is limited to {NOTE_LIMIT} characters.',
                          field='reason')
    return text


def in_scope_row(user, withdrawal_id):
    """The withdrawal the caller may act on, or an ActionError (404 / 403)."""
    row, refusal = fetch_one(user, WithdrawalRequest, PAYOUT, pk=withdrawal_id)
    if refusal == 'not_found':
        raise _not_found(withdrawal_id)
    if refusal:
        raise ActionError('PERMISSION_DENIED', 'You do not have permission to do that here.',
                          field='permission', http=status.HTTP_403_FORBIDDEN)
    return row


def in_scope_rows(user, ids):
    """{id: row} for the ids the caller may act on; the rest are simply absent
    (indistinguishable from "no such withdrawal")."""
    out = {}
    for raw in ids:
        try:
            wid = int(raw)
        except (TypeError, ValueError):
            continue
        row, refusal = fetch_one(user, WithdrawalRequest, PAYOUT, pk=wid)
        if refusal is None:
            out[wid] = row
    return out


def _lock(withdrawal_id):
    return (WithdrawalRequest.objects.select_for_update(of=('self',))
            .select_related('driver').get(pk=withdrawal_id))


# ---------------------------------------------------------------------------
# Approve (maker half of maker-checker)
# ---------------------------------------------------------------------------

def approve_one(request, row, reason=''):
    """Ask to release one payout (row is already scoped). Returns (Outcome, withdrawal)."""
    from servers.approvals import service as approvals
    from servers.approvals.actions import FINANCE_PAYOUT
    from servers.approvals.executors import payout_country, withdrawal_key

    withdrawal = (WithdrawalRequest.objects
                  .select_related('driver__user_id', 'driver__home_city').get(pk=row.pk))
    if withdrawal.status != 'pending':
        raise ActionError('INVALID_STATUS', 'Only pending withdrawals can be approved',
                          field='status')
    country = payout_country(withdrawal)
    if country is None:
        raise ActionError(
            'COUNTRY_UNKNOWN', 'This withdrawal cannot be placed in a country',
            field='withdrawal_id', http=status.HTTP_409_CONFLICT)
    # Outside any lock: a within-limit execution would call the gateway.
    outcome = approvals.require_approval_or_execute(
        FINANCE_PAYOUT, actor=request.user, country=country,
        amount=withdrawal.amount_minor, currency=withdrawal.currency,
        scope_node=withdrawal.driver.home_city, target=withdrawal,
        target_type='withdrawal_request', payload={'withdrawal_id': withdrawal.id},
        idempotency_key=withdrawal_key(withdrawal.id), reason=reason, http_request=request,
    )
    return outcome, withdrawal


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------

def _reject_locked(request, w, reason):
    """Flip a locked, pending withdrawal to rejected and return the funds once."""
    from servers.rider.models import Wallet, WalletTransaction

    driver = w.driver
    amount = w.amount
    try:
        wallet = Wallet.objects.select_for_update().get(
            user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER)
    except Wallet.DoesNotExist:
        logger.error('Driver wallet not found while rejecting withdrawal %s for driver %s',
                     w.id, driver.id)
        raise ActionError('WALLET_NOT_FOUND', 'Driver wallet not found', field='wallet',
                          http=status.HTTP_500_INTERNAL_SERVER_ERROR)

    ref = f'refund_rejected_withdrawal_{w.id}'
    already = WalletTransaction.objects.filter(
        reference_id=ref, purpose='refund_rejected_withdrawal', txn_type='credit',
        status='completed').exists()
    if not already:
        wallet.balance += amount
        wallet.save(update_fields=['balance_minor'])
        WalletTransaction.objects.create(
            user_id=driver.user_id, amount=amount, txn_type='credit', status='completed',
            purpose='refund_rejected_withdrawal', reference_id=ref, idempotency_key=ref)

    before = {'status': w.status, 'amount': str(amount)}
    w.status = 'rejected'
    w.admin_notes = reason
    w.processed_at = timezone.now()
    w.save(update_fields=['status', 'admin_notes', 'processed_at'])
    record_admin_action(
        request, action='withdrawal_rejected', target_type='withdrawal_request',
        target_id=w.id, before=before,
        after={'status': 'rejected', 'admin_notes': reason, 'wallet_refunded': True,
               'refund_reference': ref, 'refund_amount': str(amount)},
        reason=reason)


def reject_one(request, row, reason):
    """Reject a pending withdrawal (row already scoped); the driver is refunded once."""
    with transaction.atomic():
        try:
            w = _lock(row.pk)
        except WithdrawalRequest.DoesNotExist:
            raise _not_found(row.pk)
        if w.status != 'pending':
            raise ActionError('INVALID_STATUS', 'Only pending withdrawals can be rejected',
                              field='status')
        _reject_locked(request, w, reason)
    return w


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

def bulk(request, action, ids, reason, visible):
    """Bulk approve (ONE payout-run request) or reject. Returns a success Response.

    visible is in_scope_rows(user, ids): the rows the caller may act on."""
    from servers.approvals import service as approvals
    from servers.approvals.actions import FINANCE_PAYOUT
    from servers.approvals.executors import batch_key, payout_country

    if action not in ('approve', 'reject'):
        raise ActionError('INVALID_ACTION', "Action must be 'approve' or 'reject'", field='action')
    if not ids or not isinstance(ids, list):
        raise ActionError('EMPTY_LIST', 'No withdrawal IDs provided', field='withdrawal_ids')
    if len(ids) > BULK_MAX:
        raise ActionError('TOO_MANY', f'At most {BULK_MAX} withdrawals can be processed at once',
                          field='withdrawal_ids')
    if action == 'reject' and not reason:
        raise ActionError('REASON_REQUIRED', 'A reason is required.', field='reason')

    eligible, updated, skipped = [], [], []
    for raw in ids:
        try:
            wid = int(raw)
        except (TypeError, ValueError):
            skipped.append(raw)
            continue
        row = visible.get(wid)
        if row is None:
            skipped.append(wid)  # indistinguishable from "no such withdrawal"
            continue
        if action == 'approve':
            if row.status == 'pending':
                eligible.append(wid)
            else:
                skipped.append(wid)
            continue
        try:
            with transaction.atomic():
                w = _lock(wid)
                if w.status != 'pending':
                    skipped.append(wid)
                    continue
                _reject_locked(request, w, reason)
            updated.append(wid)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
            logger.error('bulk reject: row %s failed: %s', wid, exc, exc_info=True)
            skipped.append(wid)

    if action == 'approve' and eligible:
        rows = list(WithdrawalRequest.objects.select_related('driver__home_city')
                    .filter(id__in=eligible))
        currencies = {w.currency for w in rows}
        countries = {payout_country(w) for w in rows}
        if len(currencies) != 1 or len(countries) != 1 or None in countries:
            raise ActionError(
                'MIXED_MARKETS', 'A bulk payout must be in one currency and one country',
                field='withdrawal_ids')
        cities = {w.driver.home_city_id for w in rows}
        scope_node = rows[0].driver.home_city if len(cities) == 1 else None
        batch = sorted(w.id for w in rows)
        outcome = approvals.require_approval_or_execute(
            FINANCE_PAYOUT, actor=request.user, country=countries.pop(),
            amount=sum(w.amount_minor for w in rows), currency=currencies.pop(),
            scope_node=scope_node, target_type='withdrawal_batch',
            target_id=batch_key(batch).split(':', 1)[1], payload={'withdrawal_ids': batch},
            idempotency_key=batch_key(batch), reason=reason or 'bulk action', http_request=request)
        if outcome.executed:
            result = outcome.request.result or {}
            updated = list(result.get('approved_ids', []))
            skipped = skipped + list(result.get('skipped_ids', []))
        else:
            return success_response({
                'action': action, 'approval_request': outcome.request.as_dict(),
                'duplicate': outcome.duplicate, 'requested_ids': batch,
                'skipped_ids': skipped, 'requested_count': len(batch),
                'skipped_count': len(skipped)}, status.HTTP_202_ACCEPTED)

    return success_response({
        'action': action, 'updated_ids': updated, 'skipped_ids': skipped,
        'updated_count': len(updated), 'skipped_count': len(skipped)}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Retry a failed payout
# ---------------------------------------------------------------------------

def was_approved(withdrawal):
    """Was this withdrawal ever released by a checker (the approval audit row)?"""
    from servers.admin_audit.models import AdminAuditLog

    return AdminAuditLog.objects.filter(
        action='withdrawal_approved', target_type='withdrawal_request',
        target_id=str(withdrawal.id)).exists()


def retry_attempts(withdrawal_id):
    """How many human retries this withdrawal already had."""
    from servers.rider.models import WalletTransaction

    return WalletTransaction.objects.filter(
        idempotency_key__startswith=f'withdrawal_retry_{withdrawal_id}_').count()


def retry_failed(request, row, reason):
    """Send an approved-but-failed payout back through the existing trigger.

    A failed payout was refunded to the driver's wallet when it failed, so a
    retry takes the amount out of the wallet again (an idempotent debit keyed
    on the attempt) and refuses if the money is no longer there. The gateway
    is only reached through trigger_payout_creation, and never for a payout
    that already holds a gateway reference (that could pay twice).
    """
    from servers.driver.services import trigger_payout_creation
    from servers.rider.models import Wallet, WalletTransaction

    with transaction.atomic():
        try:
            w = _lock(row.pk)
        except WithdrawalRequest.DoesNotExist:
            raise _not_found(row.pk)
        if w.status != 'failed':
            raise ActionError('NOT_FAILED', 'Only a failed payout can be retried.',
                              field='status', http=status.HTTP_409_CONFLICT)
        if not was_approved(w):
            raise ActionError('NOT_APPROVED', 'This payout was never approved; approve it instead.',
                              field='status', http=status.HTTP_409_CONFLICT)
        if w.payout_reference_id:
            raise ActionError(
                'PAYOUT_HAS_REFERENCE',
                'The gateway already holds a payout for this withdrawal; check it there '
                'before anything is sent again.', field='status', http=status.HTTP_409_CONFLICT)
        wallet = Wallet.objects.select_for_update().filter(
            user_id=w.driver.user_id, scope=Wallet.SCOPE_DRIVER).first()
        if wallet is None or wallet.balance_minor < w.amount_minor:
            raise ActionError(
                'WALLET_BALANCE_TOO_LOW',
                'The driver no longer has this amount in their balance, so the payout '
                'cannot be sent again.', field='amount', http=status.HTTP_409_CONFLICT)
        attempt = retry_attempts(w.id) + 1
        key = f'withdrawal_retry_{w.id}_{attempt}'
        WalletTransaction.objects.create(
            user_id=w.driver.user_id, amount=w.amount, txn_type='debit', status='pending',
            purpose='withdrawal', reference_id=f'withdrawal_{w.id}', idempotency_key=key)
        wallet.balance_minor -= w.amount_minor
        wallet.save(update_fields=['balance_minor'])
        before = {'status': w.status, 'failure_reason': w.failure_reason}
        w.status = 'approved'
        w.failure_count = 0           # a person decided to try again: a new attempt
        w.last_failure_at = None
        w.failure_reason = None
        w.save(update_fields=['status', 'failure_count', 'last_failure_at', 'failure_reason'])
        record_admin_action(
            request, action='withdrawal_payout_retried', target_type='withdrawal_request',
            target_id=w.id, before=before,
            after={'status': 'approved', 'attempt': attempt, 'debit_key': key,
                   'no_new_approval': 'the money was approved already'},
            reason=reason)
    # Outside the lock, as the approval executor does.
    try:
        trigger_payout_creation(w)
    except Exception as exc:  # noqa: BLE001 - the trigger owns its own failure handling
        logger.error('retry: trigger_payout_creation raised for withdrawal %s: %s', w.id, exc)
    w.refresh_from_db()
    return w


# ---------------------------------------------------------------------------
# Under investigation
# ---------------------------------------------------------------------------

def mark_investigation(request, row, note, *, clear=False):
    """Flag (or clear the flag on) a payout with a note (row already scoped). No money moves."""
    if not clear and not note:
        raise ActionError('NOTE_REQUIRED', 'A note is required.', field='note')
    with transaction.atomic():
        try:
            w = _lock(row.pk)
        except WithdrawalRequest.DoesNotExist:
            raise _not_found(row.pk)
        before = {'under_investigation': w.investigation_since is not None,
                  'note': w.investigation_note}
        if clear:
            w.investigation_since = None
            w.investigation_note = ''
            w.investigation_by_label = ''
        else:
            from servers.admin_audit.models import actor_snapshot

            w.investigation_since = w.investigation_since or timezone.now()
            w.investigation_note = note
            w.investigation_by_label = actor_snapshot(request.user)['actor_label']
        w.save(update_fields=['investigation_since', 'investigation_note',
                              'investigation_by_label'])
        record_admin_action(
            request,
            action='withdrawal_investigation_cleared' if clear else 'withdrawal_marked_under_investigation',
            target_type='withdrawal_request', target_id=w.id, before=before,
            after={'under_investigation': not clear, 'note': w.investigation_note},
            reason=note or 'investigation cleared')
    return w

