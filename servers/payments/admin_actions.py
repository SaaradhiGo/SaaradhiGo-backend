"""The Finance write side of the operator console: /payments/admin/...

Part A (admin_finance.py) is reads. Everything here changes something, and
every money write goes through the generic maker-checker
(servers.approvals.require_approval_or_execute) under a registered action key
and a per-country ApprovalPolicy. A country with no policy for an action is
BLOCKED (409 THRESHOLD_NOT_CONFIGURED); nothing here invents one.

  POST payouts/<id>/approve/            finance.payout   finance.payout request
  POST payouts/<id>/reject/             finance.payout   reason required, funds returned
  POST payouts/bulk-action/             finance.payout   one request for the batch
  POST payouts/<id>/retry/              finance.payout   already-approved, failed payout only
  POST payouts/<id>/investigation/      finance.payout   note and flag, no money moves
  POST drivers/<id>/adjust/             finance.adjust   finance.adjust request (driver balance)
  POST riders/<id>/wallet-credit/       finance.adjust   finance.wallet_credit request
  POST payout-config/<country>/         finance.config   finance.payout_config_change request

Scope: payouts and drivers follow the driver's home city
(servers.payments.scope); riders follow the rider rule (servers.rider.scope).
Out of scope is a 404 identical to a missing row; in scope without the
capability at that place is a 403.
"""

import functools
from decimal import Decimal, InvalidOperation

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES
from base.money import from_minor
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_audit.services import record_admin_action
from servers.approvals import service as approvals
from servers.approvals.actions import FINANCE_ADJUST, FINANCE_PAYOUT_CONFIG, FINANCE_WALLET_CREDIT
from servers.driver.models import Driver
from servers.ride.admin_trips import (
    BadInput, _approval_answer, _idempotency_key, _minor_amount, _text,
)

from . import payout_actions
from .finance_executors import (
    ADJUST_DIRECTIONS, ADJUST_REASONS, CREDIT_REASONS, DRIVER_BALANCE, PAYOUT_CONFIG_TARGET,
    RIDER_WALLET,
)
from .models import PayoutCountryConfig
from .payout_actions import ActionError
from .payout_config import driver_payout_country
from .payout_config_write import values_of
from .scope import fetch_one

READ = 'finance.read'


def _serialized(withdrawal):
    from servers.driver.serializers import WithdrawalRequestSerializer

    return WithdrawalRequestSerializer(withdrawal).data


def _guard(fn):
    """Turn the domain refusals into the error envelope."""
    def wrapped(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except ActionError as exc:
            return exc.response()
        except BadInput as exc:
            return exc.response()
        except approvals.ApprovalError as exc:
            return exc.response()
    return functools.update_wrapper(wrapped, fn)


# ---------------------------------------------------------------------------
# Payout actions (the old /driver/admin/withdrawals/* URLs call these too)
# ---------------------------------------------------------------------------

@_guard
def approve_core(request, withdrawal_id):
    row = payout_actions.in_scope_row(request.user, withdrawal_id)
    reason = payout_actions.reason_from(request.data, required=False)
    outcome, withdrawal = payout_actions.approve_one(request, row, reason)
    withdrawal.refresh_from_db()
    if outcome.executed:
        return success_response(_serialized(withdrawal), status.HTTP_200_OK)
    return success_response({
        'withdrawal': _serialized(withdrawal),
        'approval_request': outcome.request.as_dict(),
        'duplicate': outcome.duplicate,
    }, status.HTTP_202_ACCEPTED)


@_guard
def reject_core(request, withdrawal_id):
    row = payout_actions.in_scope_row(request.user, withdrawal_id)
    reason = payout_actions.reason_from(request.data)
    withdrawal = payout_actions.reject_one(request, row, reason)
    return success_response(_serialized(withdrawal), status.HTTP_200_OK)


@_guard
def bulk_core(request):
    reason = payout_actions.reason_from(request.data, required=False)
    ids = request.data.get('withdrawal_ids', [])
    visible = payout_actions.in_scope_rows(request.user, ids if isinstance(ids, list) else [])
    return payout_actions.bulk(request, request.data.get('action'), ids, reason, visible)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def payout_approve(request, payout_id):
    """POST /payments/admin/payouts/<id>/approve/  {reason?}  200 | 202 | 404 | 409"""
    return approve_core(request, payout_id)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def payout_reject(request, payout_id):
    """POST /payments/admin/payouts/<id>/reject/  {reason}  Returns the funds once."""
    return reject_core(request, payout_id)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def payout_bulk(request):
    """POST /payments/admin/payouts/bulk-action/  {action, withdrawal_ids, reason}"""
    return bulk_core(request)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def payout_retry(request, payout_id):
    """POST /payments/admin/payouts/<id>/retry/  {reason}

    Only an already-approved withdrawal whose payout failed. No new approval:
    the money was approved already. Idempotent: a second call finds it no
    longer failed (409 NOT_FAILED).
    """
    def core(request, payout_id):
        row = payout_actions.in_scope_row(request.user, payout_id)
        reason = payout_actions.reason_from(request.data)
        withdrawal = payout_actions.retry_failed(request, row, reason)
        return success_response({
            'withdrawal': _serialized(withdrawal),
            'no_new_approval': True,
            'message': 'The money was approved already, so this does not need another approval.',
        }, status.HTTP_200_OK)
    return _guard(core)(request, payout_id)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def payout_investigation(request, payout_id):
    """POST /payments/admin/payouts/<id>/investigation/  {note} or {clear: true}"""
    def core(request, payout_id):
        row = payout_actions.in_scope_row(request.user, payout_id)
        clear = request.data.get('clear') is True
        note = _text(request.data, 'note', required=not clear)
        withdrawal = payout_actions.mark_investigation(request, row, note, clear=clear)
        return success_response({
            'id': withdrawal.id,
            'under_investigation': withdrawal.investigation_since is not None,
            'investigation_note': withdrawal.investigation_note,
            'investigation_since': withdrawal.investigation_since,
        }, status.HTTP_200_OK)
    return _guard(core)(request, payout_id)


# ---------------------------------------------------------------------------
# Driver balance adjustment
# ---------------------------------------------------------------------------

def _currency_of(value):
    code = str(value or '').strip().upper()
    return code


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.adjust')])
def driver_adjust(request, driver_id):
    """POST /payments/admin/drivers/<id>/adjust/

    {direction: credit|debit, amount_minor, currency, reason_code, note, idempotency_key,
     withdrawal_id (optional: the payout in `conflict` this debit resolves, AUDIT 27.72)}

    Always through finance.adjust maker-checker, with amounts per policy. The
    currency must equal the driver's settlement currency. A debit may never
    take the balance below zero. Writes a WalletTransaction (purpose
    'adjustment') when it runs; trip fares are not touched.
    """
    def core(request, driver_id):
        from servers.rider.models import Wallet

        driver, refusal = fetch_one(
            request.user, Driver, 'finance.adjust', pk=driver_id,
            queryset=Driver.objects.select_related('user_id', 'home_city'))
        if refusal == 'not_found':
            raise ActionError('NOT_FOUND', 'Driver not found', field='driver_id',
                              http=status.HTTP_404_NOT_FOUND)
        if refusal:
            raise ActionError('PERMISSION_DENIED', 'You do not have permission to do that here.',
                              field='permission', http=status.HTTP_403_FORBIDDEN)
        data = request.data
        direction = str(data.get('direction') or '').strip()
        if direction not in ADJUST_DIRECTIONS:
            raise BadInput('INVALID_DIRECTION', 'direction must be credit or debit.', 'direction')
        amount = _minor_amount(data.get('amount_minor'))
        if amount <= 0:
            raise BadInput('AMOUNT_NOT_POSITIVE', 'amount_minor must be above zero.',
                           'amount_minor')
        reason_code = str(data.get('reason_code') or '').strip()
        if reason_code not in ADJUST_REASONS:
            raise BadInput('INVALID_REASON_CODE',
                           f'reason_code must be one of: {", ".join(ADJUST_REASONS)}.',
                           'reason_code')
        note = _text(data, 'note', required=True)
        key = _idempotency_key(request)
        wallet = Wallet.objects.filter(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER).first()
        if wallet is None:
            raise ActionError('WALLET_NOT_FOUND', 'This driver has no settlement wallet.',
                              field='driver_id', http=status.HTTP_409_CONFLICT)
        currency = _currency_of(data.get('currency'))
        if currency != wallet.currency:
            raise ActionError(
                'CURRENCY_MISMATCH',
                f"The driver's settlement currency is {wallet.currency}; this amount is in "
                f"{currency or 'no currency'}.", field='currency')
        if direction == 'debit' and wallet.balance_minor - amount < 0:
            raise ActionError(
                'BALANCE_WOULD_GO_NEGATIVE',
                'The balance is lower than this debit; a debit cannot take it below zero.',
                field='amount_minor', http=status.HTTP_409_CONFLICT)
        country = driver_payout_country(driver)
        if country is None:
            raise ActionError('COUNTRY_UNKNOWN', 'This driver cannot be placed in a country.',
                              field='driver_id', http=status.HTTP_409_CONFLICT)
        reason = f'{ADJUST_REASONS[reason_code]}: {note}'[:2000]
        payload = {'driver_id': driver.id, 'direction': direction, 'amount_minor': amount,
                   'currency': wallet.currency, 'reason_code': reason_code, 'note': note}
        # Resolving a payout reported paid after its refund (AUDIT 27.72): a debit of exactly
        # its amount that names it. On approval the payout becomes paid.
        if data.get('withdrawal_id') not in (None, ''):
            from servers.payments.payout_conflict import check_resolution

            payload['withdrawal_id'] = _minor_amount(data.get('withdrawal_id'))
            # Always a second pair of eyes, whatever the maker's limit.
            payload['requires_checker'] = True
            try:
                check_resolution(driver, payload)
            except ValueError as exc:
                raise ActionError(exc.args[0], exc.args[1], field='withdrawal_id',
                                  http=status.HTTP_409_CONFLICT)
        outcome = approvals.require_approval_or_execute(
            FINANCE_ADJUST, actor=request.user, country=country, amount=amount,
            currency=wallet.currency, scope_node=driver.home_city,
            target_type=DRIVER_BALANCE, target_id=driver.id,
            payload=payload,
            idempotency_key=f'driver_adjust:{driver.id}:{key}', reason=reason,
            http_request=request)
        return _approval_answer(outcome, {'driver_id': driver.id})
    return _guard(core)(request, driver_id)


# ---------------------------------------------------------------------------
# Rider wallet credit
# ---------------------------------------------------------------------------

def _country_of_currency(code):
    for country in COUNTRIES.values():
        if country.currency == code:
            return country.code
    return None


def _linked_ticket(request, rider):
    """The ticket id a credit is for, validated like the support refund link:
    the ticket must belong to THIS rider and be visible to the caller."""
    raw = request.data.get('ticket_id')
    if raw in (None, ''):
        return None
    from servers.support.models import SupportTicket
    from servers.support.scope import tickets_visible_to

    try:
        if isinstance(raw, bool):
            raise ValueError
        ticket_id = int(str(raw).strip())
    except (TypeError, ValueError):
        raise BadInput('INVALID_FILTER', 'ticket_id must be an integer.', 'ticket_id')
    ticket = tickets_visible_to(request.user, None, SupportTicket.objects.filter(
        id=ticket_id, user_id=rider.user_id)).first()
    if ticket is None:
        raise BadInput('TICKET_RIDER_MISMATCH',
                       'That ticket does not exist for this rider.', 'ticket_id')
    return ticket.id


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.adjust')])
def rider_wallet_credit(request, rider_id):
    """POST /payments/admin/riders/<id>/wallet-credit/

    {amount_minor, currency, reason_code, note?, ticket_id?, idempotency_key}

    A closed-loop credit (ADR-0003); there is no debit of rider wallets here.
    Always needs a checker (finance.wallet_credit). A linked ticket gets an
    internal note when requested and a neutral line to the rider ONLY when
    the credit has been added; never a word about approval.
    """
    def core(request, rider_id):
        from django.conf import settings

        from servers.rider.admin_riders import _rider_for, node_of_rider
        from servers.rider.models import Wallet
        from servers.support.service import post_internal_note

        rider, node, refusal = _rider_for(request, rider_id, 'finance.adjust')
        if refusal:
            return refusal
        data = request.data
        amount = _minor_amount(data.get('amount_minor'))
        if amount <= 0:
            raise BadInput('AMOUNT_NOT_POSITIVE', 'amount_minor must be above zero.',
                           'amount_minor')
        reason_code = str(data.get('reason_code') or '').strip()
        if reason_code not in CREDIT_REASONS:
            raise BadInput('INVALID_REASON_CODE',
                           f'reason_code must be one of: {", ".join(CREDIT_REASONS)}.',
                           'reason_code')
        note = _text(data, 'note', required=(reason_code == 'other'))
        key = _idempotency_key(request)
        ticket_id = _linked_ticket(request, rider)
        wallet = Wallet.objects.filter(user_id=rider.user_id, scope=Wallet.SCOPE_RIDER).first()
        if wallet is None:
            raise ActionError('WALLET_NOT_FOUND', 'This rider has no wallet.',
                              field='rider_id', http=status.HTTP_409_CONFLICT)
        currency = _currency_of(data.get('currency'))
        if currency != wallet.currency:
            raise ActionError(
                'CURRENCY_MISMATCH',
                f"The rider's wallet is in {wallet.currency}; this amount is in "
                f"{currency or 'no currency'}.", field='currency')
        cap = Decimal(str(getattr(settings, 'RIDER_CREDIT_BALANCE_CAP', '2000.00')))
        if from_minor(wallet.balance_minor + amount, wallet.currency) > cap:
            raise ActionError('WALLET_CAP_EXCEEDED',
                              'The credit would take the wallet above its limit.',
                              field='amount_minor', http=status.HTTP_409_CONFLICT)
        country = _country_of_currency(wallet.currency)
        if country is None:
            raise ActionError('COUNTRY_UNKNOWN', 'This wallet currency names no market.',
                              field='currency', http=status.HTTP_409_CONFLICT)
        reason = f'{CREDIT_REASONS[reason_code]}' + (f': {note}' if note else '')
        if ticket_id:
            reason = f'{reason} [support ticket #{ticket_id}]'
        node = node or node_of_rider(rider)
        outcome = approvals.require_approval_or_execute(
            FINANCE_WALLET_CREDIT, actor=request.user, country=country, amount=amount,
            currency=wallet.currency, scope_node=node, target_type=RIDER_WALLET,
            target_id=rider.user_id_id,
            payload={'rider_id': rider.id, 'user_id': rider.user_id_id, 'amount_minor': amount,
                     'currency': wallet.currency, 'reason_code': reason_code, 'note': note,
                     **({'ticket_id': ticket_id} if ticket_id else {})},
            idempotency_key=f'rider_credit:{rider.id}:{key}', reason=reason[:2000],
            http_request=request)
        if ticket_id and not outcome.duplicate:
            post_internal_note(
                ticket_id,
                f'Wallet credit of {from_minor(amount, wallet.currency)} {wallet.currency} '
                f'requested ({CREDIT_REASONS[reason_code]}); request #{outcome.request.id} '
                f'is {outcome.request.status}.',
                key=f'wallet-credit:{outcome.request.id}', author=request.user)
            record_admin_action(
                request, action='support_ticket_wallet_credit_requested',
                target_type='support_ticket', target_id=ticket_id,
                after={'approval_request_id': outcome.request.id, 'amount_minor': amount,
                       'currency': wallet.currency, 'state': outcome.request.status},
                reason=f'wallet credit requested from ticket #{ticket_id}')
        return _approval_answer(outcome, {'rider_id': rider.user_id_id})
    return _guard(core)(request, rider_id)


# ---------------------------------------------------------------------------
# Payout hold: release early (fraud money actions, AUDIT 27.56)
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def payout_hold_release(request, hold_id):
    """POST /payments/admin/payout-holds/<id>/release/  {note}

    Releases a payout hold on a suspended driver early. No checker: it only lets the driver be
    paid. The hold is found through the driver's home city (the Finance rule); out of scope is a
    404, a missing capability there a 403. Audited."""
    def core(request, hold_id):
        from servers.fraud import money
        from servers.fraud.models import PayoutHold

        hold = PayoutHold.objects.select_related('driver').filter(pk=hold_id).first()
        driver = None
        if hold is not None:
            driver, refusal = fetch_one(request.user, Driver, 'finance.payout',
                                        pk=hold.driver_id,
                                        queryset=Driver.objects.select_related('home_city'))
            if refusal == 'not_found':
                driver = None
            elif refusal:
                raise ActionError('PERMISSION_DENIED',
                                  'You do not have permission to do that here.',
                                  field='permission', http=status.HTTP_403_FORBIDDEN)
        if hold is None or driver is None:
            raise ActionError('NOT_FOUND', 'Payout hold not found', field='hold_id',
                              http=status.HTTP_404_NOT_FOUND)
        try:
            hold = money.release_payout_hold(request.user, hold, request.data.get('note'),
                                             http_request=request)
        except money.MoneyError as exc:
            raise ActionError(exc.code, exc.message, field=exc.field, http=exc.status)
        return success_response(money.hold_dict(hold), status.HTTP_200_OK)
    return _guard(core)(request, hold_id)


# ---------------------------------------------------------------------------
# Payout configuration: propose a change (finance.payout_config_change)
# ---------------------------------------------------------------------------

def _config_values(data, current):
    """The proposed values, validated; fields not sent keep the current value."""
    values = {
        'fee_percent': Decimal(str(current.fee_percent)),
        'minimum_withdrawal_minor': current.minimum_withdrawal_minor,
        'stuck_payout_hours': current.stuck_payout_hours,
        'payout_schedule': current.payout_schedule,
        'payout_weekday': current.payout_weekday,
        'reward_hold_days': current.reward_hold_days,
        'recovery_max_percent': current.recovery_max_percent,
        'payout_hold_max_days': current.payout_hold_max_days,
    }
    for key, (low, high) in {'reward_hold_days': (1, 90), 'recovery_max_percent': (1, 100),
                             'payout_hold_max_days': (1, 90)}.items():
        if key in data:
            number = _minor_amount(data[key], key)
            if number < low or number > high:
                raise BadInput('INVALID_' + key.upper(),
                               f'{key} must be between {low} and {high}.', key)
            values[key] = number
    if 'fee_percent' in data:
        try:
            fee = Decimal(str(data['fee_percent']))
        except (InvalidOperation, ValueError):
            raise BadInput('INVALID_FEE', 'fee_percent must be a number.', 'fee_percent')
        if fee < 0 or fee > 100 or fee != fee.quantize(Decimal('0.01')):
            raise BadInput('INVALID_FEE',
                           'fee_percent must be between 0 and 100 with at most two decimals.',
                           'fee_percent')
        values['fee_percent'] = fee
    if 'minimum_withdrawal_minor' in data:
        minimum = _minor_amount(data['minimum_withdrawal_minor'], 'minimum_withdrawal_minor')
        if minimum < 0:
            raise BadInput('INVALID_MINIMUM', 'The minimum cannot be negative.',
                           'minimum_withdrawal_minor')
        values['minimum_withdrawal_minor'] = minimum
    if 'stuck_payout_hours' in data:
        hours = data['stuck_payout_hours']
        if hours is not None:
            hours = _minor_amount(hours, 'stuck_payout_hours')
            if hours <= 0 or hours > 24 * 90:
                raise BadInput('INVALID_STUCK_HOURS',
                               'stuck_payout_hours must be between 1 and 2160, or null.',
                               'stuck_payout_hours')
        values['stuck_payout_hours'] = hours
    if 'payout_schedule' in data:
        schedule = data['payout_schedule']
        if schedule not in (PayoutCountryConfig.ON_REQUEST, PayoutCountryConfig.WEEKLY):
            raise BadInput('INVALID_SCHEDULE', 'payout_schedule must be on_request or weekly.',
                           'payout_schedule')
        values['payout_schedule'] = schedule
    if 'payout_weekday' in data:
        day = data['payout_weekday']
        if day is not None:
            day = _minor_amount(day, 'payout_weekday')
            if day < 0 or day > 6:
                raise BadInput('INVALID_WEEKDAY', 'payout_weekday is 0 (Monday) to 6 (Sunday).',
                               'payout_weekday')
        values['payout_weekday'] = day
    if values['payout_schedule'] == PayoutCountryConfig.WEEKLY and values['payout_weekday'] is None:
        raise BadInput('WEEKDAY_REQUIRED', 'A weekly schedule needs a weekday.', 'payout_weekday')
    if values['payout_schedule'] == PayoutCountryConfig.ON_REQUEST:
        values['payout_weekday'] = None
    return values


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.config')])
def payout_config_propose(request, country):
    """POST /payments/admin/payout-config/<country>/

    {fee_percent?, minimum_withdrawal_minor?, stuck_payout_hours?, payout_schedule?,
     payout_weekday?, reason, idempotency_key}

    Raises a finance.payout_config_change request (always a checker). The new
    version is written by the executor when a different finance admin approves.
    A country with no payout rows yet cannot be edited here: the owner sets a
    new country's first values.
    """
    def core(request, country):
        from .scope import country_codes_visible

        code = str(country).upper()
        visible = country_codes_visible(request.user, None, 'finance.config')
        if code not in visible:
            raise ActionError('NOT_FOUND', 'Country not found', field='country',
                              http=status.HTTP_404_NOT_FOUND)
        current = PayoutCountryConfig.objects.filter(country=code, is_active=True).first()
        if current is None:
            raise ActionError(
                'NOT_CONFIGURED',
                "This country has no payout settings yet; the owner sets a new market's "
                'first values.', field='country', http=status.HTTP_409_CONFLICT)
        reason = _text(request.data, 'reason', required=True)
        key = _idempotency_key(request)
        values = _config_values(request.data, current)
        if values == values_of(current):
            raise BadInput('NO_CHANGE', 'Nothing differs from the current settings.', 'reason')
        from servers.admin_access.models import GeoNode

        node = GeoNode.objects.filter(kind='country', code=code).first()
        outcome = approvals.require_approval_or_execute(
            FINANCE_PAYOUT_CONFIG, actor=request.user, country=code,
            scope_node=node, target_type=PAYOUT_CONFIG_TARGET, target_id=code,
            payload={'country': code, 'base_version': current.version,
                     'values': {k: (str(v) if isinstance(v, Decimal) else v)
                                for k, v in values.items()},
                     'before': {k: (str(v) if isinstance(v, Decimal) else v)
                                for k, v in values_of(current).items()}},
            idempotency_key=f'payout_config:{code}:{key}', reason=reason,
            http_request=request)
        return _approval_answer(outcome, {'country': code})
    return _guard(core)(request, country)
