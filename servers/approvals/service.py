"""Request -> approve or reject -> execute. Once, by two people, on the record.

THE ONE ENTRY POINT FOR A MODULE
--------------------------------
    outcome = require_approval_or_execute(
        'finance.payout', actor=request.user, country='IN',
        amount=withdrawal.amount_minor, currency=withdrawal.currency,
        scope_node=withdrawal.driver.home_city, target=withdrawal,
        payload={'withdrawal_id': withdrawal.id},
        idempotency_key=f'withdrawal:{withdrawal.id}:approve',
        http_request=request,
    )
    if outcome.executed: ...the thing has happened...
    else:               ...a request is waiting; tell the maker its id...

It reads the active `ApprovalPolicy` for (country, action). No policy means
the country has not been configured and the action is REFUSED
(`ThresholdNotConfigured`), never defaulted. Within the maker limit the
registered executor runs now and the row is written already executed, with
an audit row that says "within maker limit" and names the limit. Above it,
the row waits for a checker.

WHO MAY CHECK
-------------
A different admin, holding `approval.check` AND the action's own capability
(`actions.Action.capability`), both in the scope the request sits in. The
"different" is a database constraint as well as a check here; the
capabilities are asked of `admin_access.bridge`, which is the same authority
every other admin endpoint uses. Missing capability is 403; an in-scope
question answered "no" is 404, the way every out-of-scope object answers,
so a checker cannot learn what another country is approving by walking ids.

EXACTLY ONCE
------------
Two checkers clicking Approve at the same moment must not release two
payouts. The transition `requested -> approved` is made under
`select_for_update`, and ONLY the caller that made the transition runs the
executor; a caller that finds the row already approved by somebody else is
told so (409), and a caller that finds its own earlier decision gets that
decision back unchanged. The executor itself runs OUTSIDE the lock, as the
withdrawal endpoint always has, so a provider round-trip does not hold a
row lock; the row is `approved` for that window, which is the state an
operator looks for after a crash.

The idempotency key does the same for makers: the same key while a request
is open or executed returns that request rather than raising a second one.
A rejected, cancelled, expired or failed request releases its key, because
raising the same thing again after a rejection is a new decision.

EVERY TRANSITION IS AUDITED
---------------------------
Through `record_admin_action` with the acting user as the actor, so the
audit log can replay who asked, who decided and what happened, with the
actor snapshots it keeps for everything else. The executor runs inside an
`audit_context` naming the checker, so the rows IT writes (a withdrawal
flipped to approved) carry the checker, not nobody.
"""

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status as http

from base.countries import COUNTRIES, get_country
from base.money import is_supported
from base.permissions import is_operator
from base.utils import error_response
from servers.admin_access.bridge import granted
from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action

from . import registry
from .actions import POLICY_CHANGE, action_for
from .models import (
    ALWAYS, APPROVED, CANCELLED, COUNT, EXECUTED, EXPIRED, FAILED,
    KEY_HELD_STATUSES, MONEY, REJECTED, REQUESTED, ApprovalPolicy,
    ApprovalRequest,
)
from .scope import anchor_for, country_node, may_act

logger = logging.getLogger(__name__)

TARGET_TYPE = 'approval_request'
EXPIRY_LABEL = 'system:approval_expiry'


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

class ApprovalError(Exception):
    """A refusal with a stable code; views turn it into the error envelope."""

    code = 'APPROVAL_ERROR'
    status = http.HTTP_400_BAD_REQUEST
    field = 'general'

    def __init__(self, message, *, code=None, status=None, field=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        if field:
            self.field = field

    def response(self):
        return error_response(
            code=self.code, message=self.message, field=self.field,
            issue=self.message, status=self.status,
        )


class ThresholdNotConfigured(ApprovalError):
    """The country has no policy for this action. Blocked, by ruling."""

    code = 'THRESHOLD_NOT_CONFIGURED'
    status = http.HTTP_409_CONFLICT
    field = 'country'


class InvalidRequest(ApprovalError):
    """Bad input from the caller: unknown action or country, wrong currency."""

    status = http.HTTP_400_BAD_REQUEST


class NotPermitted(ApprovalError):
    """The caller holds the wrong authority for this step (self-approval,
    a checker without the action's capability, a non-maker cancelling)."""

    status = http.HTTP_403_FORBIDDEN


class NotInScope(ApprovalError):
    """Out of the caller's scope: answered as not found, like every object."""

    code = 'NOT_FOUND'
    status = http.HTTP_404_NOT_FOUND
    field = 'request_id'


class WrongState(ApprovalError):
    """The request is not in a state where this step applies."""

    status = http.HTTP_409_CONFLICT
    field = 'status'


class NoExecutor(ApprovalError):
    """Nothing is registered to carry the action out. A deployment defect."""

    code = 'NO_EXECUTOR'
    status = http.HTTP_500_INTERNAL_SERVER_ERROR


# ---------------------------------------------------------------------------
# Policy lookup and the decision
# ---------------------------------------------------------------------------

def active_policy(country, action_key):
    return ApprovalPolicy.objects.filter(
        country=country, action_key=action_key, is_active=True,
    ).first()


def _check_country(country):
    code = str(country or '').upper().strip()
    if code not in COUNTRIES:
        raise InvalidRequest(f'Unknown country {country!r}.',
                             code='UNKNOWN_COUNTRY', field='country')
    return code


def _check_amount(amount):
    # bool is an int in Python; a `True` amount is a bug, not one paisa.
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidRequest(
            f'amount must be an integer in minor units, got {type(amount).__name__}.',
            code='AMOUNT_NOT_INTEGER', field='amount',
        )
    if amount < 0:
        raise InvalidRequest('amount must not be negative.',
                             code='AMOUNT_NEGATIVE', field='amount')
    return amount


def decide(action_key, country, amount=None, currency=None):
    """`(needs_checker, policy)` for this action in this country.

    Raises `ThresholdNotConfigured` when the country has no active policy
    for the action, unless the action's rule is built in
    (`Action.always_checker`), in which case the answer is always "yes,
    a checker" and no row is consulted.
    """
    try:
        action = action_for(action_key)
    except ValueError as exc:
        raise InvalidRequest(str(exc), code='UNKNOWN_ACTION', field='action_key')
    country = _check_country(country)

    if action.always_checker:
        return True, None

    policy = active_policy(country, action_key)
    if policy is None:
        raise ThresholdNotConfigured(
            f'No maker-checker threshold is configured for {action_key} in '
            f'{country}. The action is blocked until a threshold is set '
            f'(a maker-checker action of its own: approvals.policy_change).'
        )

    if policy.limit_kind == ALWAYS or policy.maker_limit is None:
        return True, policy

    if amount is None:
        raise InvalidRequest(
            f'{action_key} in {country} is decided by amount; none was given.',
            code='AMOUNT_REQUIRED', field='amount',
        )
    amount = _check_amount(amount)

    if policy.limit_kind == MONEY:
        code = str(currency or '').upper().strip()
        if code != policy.currency:
            # Never convert. A rupee limit says nothing about dollars.
            raise InvalidRequest(
                f'{action_key} in {country} is limited in {policy.currency}; '
                f'this amount is in {currency or "no currency"}.',
                code='CURRENCY_MISMATCH', field='currency',
            )

    return amount > policy.maker_limit, policy


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _facts(row):
    """What an audit row should carry about a request, in one shape."""
    return {
        'request_id': row.id,
        'action_key': row.action_key,
        'country': row.country,
        'scope': row.scope_node.code if row.scope_node_id else None,
        'amount': row.amount,
        'currency': row.currency,
        'status': row.status,
        'target_type': row.target_type,
        'target_id': row.target_id,
        'maker_id': row.maker_id,
        'checker_id': row.checker_id,
        'policy_id': row.policy_id,
        'policy_version': row.policy.version if row.policy_id else None,
        'maker_limit': row.policy.maker_limit if row.policy_id else None,
        'idempotency_key': row.idempotency_key,
    }


def _audit(row, action, *, actor, http_request, before=None, after=None,
           reason='', actor_label=''):
    with audit_context(actor=actor, actor_label=actor_label,
                       request=http_request, reason=reason):
        record_admin_action(
            http_request if actor is not None else None,
            action=action, target_type=TARGET_TYPE, target_id=row.id,
            before=before or {}, after=after or _facts(row), reason=reason,
        )


# ---------------------------------------------------------------------------
# Raising a request
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    """What `require_approval_or_execute` hands back."""

    request: ApprovalRequest
    #: True when the action ran now, within the maker's limit.
    executed: bool
    #: True when an existing request was returned for the idempotency key
    #: rather than a new one raised.
    duplicate: bool = False

    @property
    def pending(self):
        return self.request.status == REQUESTED


def _existing(idempotency_key):
    return ApprovalRequest.objects.filter(
        idempotency_key=idempotency_key, status__in=KEY_HELD_STATUSES,
    ).select_related('scope_node', 'policy').first()


def _target_fields(target, target_type, target_id):
    if target is not None:
        return (
            target_type or target._meta.model_name,
            str(target_id or getattr(target, 'pk', '')),
        )
    return target_type or '', str(target_id or '')


def require_approval_or_execute(
    action_key, *, actor, country, amount=None, currency=None,
    scope_node=None, target=None, target_type='', target_id='',
    payload=None, execute=None, idempotency_key, reason='', http_request=None,
):
    """Do it now if the maker may, else raise a request for a checker.

    `execute` is optional. When given it runs the WITHIN-LIMIT path (it
    receives the request row and returns a JSON-able dict); the deferred
    path always runs the executor registered for `action_key`, because a
    closure cannot be stored on a row. Either way an executor must be
    registered, and that is checked before anything is written, so a module
    cannot raise a request that nobody can ever approve.
    """
    if not is_operator(actor):
        raise NotPermitted('Only an operator may raise a maker-checker request.',
                           code='NOT_AN_OPERATOR')
    if not idempotency_key:
        raise InvalidRequest('idempotency_key is required.',
                             code='IDEMPOTENCY_KEY_REQUIRED', field='idempotency_key')

    needs_checker, policy = decide(action_key, country, amount, currency)
    country = _check_country(country)
    if currency:
        currency = str(currency).upper().strip()
        if not is_supported(currency):
            raise InvalidRequest(f'Unsupported currency {currency!r}.',
                                 code='UNSUPPORTED_CURRENCY', field='currency')

    try:
        registered = registry.executor_for(action_key)
    except registry.NoExecutor as exc:
        raise NoExecutor(str(exc))
    executor = execute or registered

    existing = _existing(idempotency_key)
    if existing is not None:
        return Outcome(existing, executed=False, duplicate=True)

    kind, ident = _target_fields(target, target_type, target_id)
    row = ApprovalRequest(
        action_key=action_key, country=country,
        scope_node=anchor_for(scope_node, country),
        amount=amount if amount is None else _check_amount(amount),
        currency=currency or '', policy=policy,
        target_type=kind[:64], target_id=ident[:64],
        payload=payload or {}, request_reason=(reason or '')[:10000],
        idempotency_key=idempotency_key[:200],
    )
    row.set_maker(actor)
    if not needs_checker:
        # Nobody else is asked, and the row says so: no checker, decided
        # on the spot, with the limit it was under written into the audit.
        row.status = APPROVED
        row.decided_at = timezone.now()
        row.decision_reason = 'within maker limit'

    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        # Lost the race to another request with the same key: that one is
        # the answer, exactly as if it had been found a moment earlier.
        existing = _existing(idempotency_key)
        if existing is None:  # pragma: no cover - constraint says otherwise
            raise
        return Outcome(existing, executed=False, duplicate=True)

    if needs_checker:
        _audit(row, 'approval_requested', actor=actor, http_request=http_request,
               reason=reason)
        return Outcome(row, executed=False)

    _audit(
        row, 'approval_within_maker_limit', actor=actor, http_request=http_request,
        reason=reason,
        after={**_facts(row), 'within_maker_limit': True,
               'maker_limit': policy.maker_limit if policy else None},
    )
    _run(row, executor, actor=actor, http_request=http_request, reason=reason)
    return Outcome(row, executed=True)


# ---------------------------------------------------------------------------
# Running the executor, once
# ---------------------------------------------------------------------------

def _run(row, executor, *, actor, http_request, reason):
    """Run `executor(row)` for an APPROVED row and record what happened.

    Called only by the code path that made the `-> approved` transition,
    which is what makes it once. The status flip to executed/failed is a
    conditional UPDATE, so a stale copy of the row cannot overwrite a
    later state.
    """
    try:
        with audit_context(actor=actor, request=http_request, reason=reason):
            result = executor(row)
    except registry.ExecutionRefused as exc:
        _finish(row, FAILED, error_code=exc.code, error=exc.message)
        _audit(row, 'approval_failed', actor=actor, http_request=http_request,
               reason=reason, after={**_facts(row), 'error_code': exc.code,
                                     'error': exc.message})
        return
    except Exception as exc:  # noqa: BLE001 - recorded on the row, then logged
        logger.exception('approvals: executor for %s (request %s) raised',
                         row.action_key, row.id)
        _finish(row, FAILED, error_code='EXECUTOR_ERROR', error=str(exc)[:2000])
        _audit(row, 'approval_failed', actor=actor, http_request=http_request,
               reason=reason, after={**_facts(row), 'error_code': 'EXECUTOR_ERROR',
                                     'error': str(exc)[:2000]})
        return

    _finish(row, EXECUTED, result=result if isinstance(result, dict) else {'result': result})
    _audit(row, 'approval_executed', actor=actor, http_request=http_request,
           reason=reason, after={**_facts(row), 'result': row.result})


def _finish(row, new_status, *, result=None, error_code='', error=''):
    now = timezone.now()
    updated = ApprovalRequest.objects.filter(pk=row.pk, status=APPROVED).update(
        status=new_status, executed_at=now, result=result or {},
        error_code=error_code, error=error,
    )
    if not updated:  # pragma: no cover - defensive; the lock makes this unreachable
        logger.error('approvals: request %s was not APPROVED when finishing as %s',
                     row.pk, new_status)
    row.refresh_from_db()


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

def _lock(request_or_id):
    """The row, locked. No `select_related`: `scope_node` and `policy` are
    nullable, so joining them makes a LEFT OUTER JOIN, and PostgreSQL
    refuses FOR UPDATE on the nullable side of one (the same trap
    `bridge.in_scope` documents for withdrawals). Two lazy lookups are the
    price of a lock that actually takes."""
    pk = getattr(request_or_id, 'pk', request_or_id)
    return ApprovalRequest.objects.select_for_update().get(pk=pk)


def _check_checker(row, checker):
    """The three questions, in the order that leaks least.

    Capability anywhere? (403.) In this request's scope? (404 -- the row
    does not exist for this checker.) The action's own capability there?
    (403.) Not the maker? (403.)
    """
    allowed, _how = granted(checker, 'approval.check')
    if not allowed:
        raise NotPermitted('You do not hold approval.check.',
                           code='CHECKER_LACKS_CAPABILITY')
    if not may_act(checker, 'approval.check', row.scope_node):
        raise NotInScope(f'No approval request matches id {row.pk}.')
    action = action_for(row.action_key)
    if not may_act(checker, action.capability, row.scope_node):
        raise NotPermitted(
            f'Checking {row.action_key} needs {action.capability} in the '
            f'request\'s scope, as well as approval.check.',
            code='CHECKER_LACKS_ACTION_CAPABILITY',
        )
    if checker.pk == row.maker_id:
        raise NotPermitted(
            'The checker must be a different admin from the maker.',
            code='SELF_APPROVAL',
        )


def _expire_if_due(row):
    """Mark a locked, open, overdue row expired. Returns whether it did."""
    if row.status == REQUESTED and row.is_past_expiry:
        row.status = EXPIRED
        row.save(update_fields=['status'])
        _audit(row, 'approval_expired', actor=None, http_request=None,
               actor_label=EXPIRY_LABEL, reason='expired before a checker acted')
        return True
    return False


def _expired(row):
    """Expire `row` in a transaction of its own, then say so.

    Called AFTER the caller's lock block has exited: raising the refusal
    from inside that block would roll the expiry (and its audit row) back
    with it, leaving an overdue request that is refused every time and
    never recorded as expired.
    """
    with transaction.atomic():
        _expire_if_due(_lock(row.pk))
    return WrongState('This request expired before a checker acted.', code='EXPIRED')


def _is_overdue(row):
    return row.status == REQUESTED and row.is_past_expiry


def approve(request, checker, reason='', http_request=None):
    """The checker says yes; the executor runs once; the row records both.

    Returns the row. Raises on any refusal; see `_check_checker`.
    """
    with transaction.atomic():
        row = _lock(request)
        _check_checker(row, checker)
        overdue = _is_overdue(row)
        if overdue:
            pass  # expired outside this block, see `_expired`
        elif row.status != REQUESTED:
            if row.checker_id == checker.pk and row.status in (APPROVED, EXECUTED, FAILED):
                return row  # the same decision, again: idempotent
            raise WrongState(
                f'This request is {row.status}; only a requested one can be approved.',
                code='ALREADY_DECIDED' if row.checker_id else 'NOT_OPEN',
            )
        if not overdue:
            before = _facts(row)
            row.status = APPROVED
            row.set_checker(checker)
            row.decided_at = timezone.now()
            row.decision_reason = (reason or '')[:10000]
            row.save()
            _audit(row, 'approval_approved', actor=checker, http_request=http_request,
                   before=before, reason=reason)
    if overdue:
        raise _expired(row)

    try:
        executor = registry.executor_for(row.action_key)
    except registry.NoExecutor as exc:
        _finish(row, FAILED, error_code='NO_EXECUTOR', error=str(exc))
        _audit(row, 'approval_failed', actor=checker, http_request=http_request,
               reason=reason, after={**_facts(row), 'error_code': 'NO_EXECUTOR'})
        return row

    _run(row, executor, actor=checker, http_request=http_request, reason=reason)
    return row


def reject(request, checker, reason, http_request=None):
    """The checker says no. Nothing runs, ever, from a rejected request."""
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required to reject a request.',
                             code='REASON_REQUIRED', field='reason')
    with transaction.atomic():
        row = _lock(request)
        _check_checker(row, checker)
        overdue = _is_overdue(row)
        if overdue:
            pass  # expired outside this block, see `_expired`
        elif row.status != REQUESTED:
            if row.checker_id == checker.pk and row.status == REJECTED:
                return row
            raise WrongState(
                f'This request is {row.status}; only a requested one can be rejected.',
                code='ALREADY_DECIDED' if row.checker_id else 'NOT_OPEN',
            )
        else:
            before = _facts(row)
            row.status = REJECTED
            row.set_checker(checker)
            row.decided_at = timezone.now()
            row.decision_reason = reason[:10000]
            row.save()
            _audit(row, 'approval_rejected', actor=checker, http_request=http_request,
                   before=before, reason=reason)
    if overdue:
        raise _expired(row)
    return row


def cancel(request, maker, reason='', http_request=None):
    """The maker withdraws their own request while it is still open."""
    with transaction.atomic():
        row = _lock(request)
        if maker.pk != row.maker_id:
            raise NotPermitted('Only the maker may cancel a request.', code='NOT_MAKER')
        overdue = _is_overdue(row)
        if overdue:
            pass  # expired outside this block, see `_expired`
        elif row.status != REQUESTED:
            if row.status == CANCELLED:
                return row
            raise WrongState(
                f'This request is {row.status}; only a requested one can be cancelled.',
                code='NOT_OPEN',
            )
        else:
            before = _facts(row)
            row.status = CANCELLED
            row.decided_at = timezone.now()
            row.decision_reason = (reason or '')[:10000]
            row.save()
            _audit(row, 'approval_cancelled', actor=maker, http_request=http_request,
                   before=before, reason=reason)
    if overdue:
        raise _expired(row)
    return row


def expire_stale(now=None):
    """Mark every open request past its `expires_at` as expired. Returns count.

    Expiry is also applied lazily when a checker reaches a stale row, so
    this sweep is for the log and the queue, not for correctness.
    """
    now = now or timezone.now()
    due = list(ApprovalRequest.objects.filter(
        status=REQUESTED, expires_at__lte=now,
    ).values_list('pk', flat=True))
    count = 0
    for pk in due:
        with transaction.atomic():
            row = _lock(pk)
            if _expire_if_due(row):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

def _validate_policy_fields(country, action_key, limit_kind, currency, maker_limit):
    country = _check_country(country)
    try:
        action = action_for(action_key)
    except ValueError as exc:
        raise InvalidRequest(str(exc), code='UNKNOWN_ACTION', field='action_key')
    if action.always_checker:
        raise InvalidRequest(
            f'{action_key} always needs a checker by construction; it has no policy row.',
            code='ACTION_HAS_NO_POLICY', field='action_key',
        )
    if limit_kind not in (MONEY, COUNT, ALWAYS):
        raise InvalidRequest(f'limit_kind must be one of money, count, always; got {limit_kind!r}.',
                             code='INVALID_LIMIT_KIND', field='limit_kind')
    currency = str(currency or '').upper().strip()
    local = get_country(country).currency
    if limit_kind == MONEY:
        if currency != local:
            # "Per country, in local currency" is the ruling. A dollar limit
            # for India is not a limit anybody can compare a refund with.
            raise InvalidRequest(
                f'A money threshold for {country} must be in {local}, not {currency or "nothing"}.',
                code='CURRENCY_NOT_LOCAL', field='currency',
            )
    else:
        if currency:
            raise InvalidRequest(f'A {limit_kind} threshold carries no currency.',
                                 code='CURRENCY_NOT_ALLOWED', field='currency')
    if limit_kind == ALWAYS:
        if maker_limit is not None:
            raise InvalidRequest('An always-checker policy has no maker_limit.',
                                 code='LIMIT_NOT_ALLOWED', field='maker_limit')
    elif maker_limit is not None:
        _check_amount(maker_limit)
    return country, currency, maker_limit


def write_policy_version(*, country, action_key, limit_kind, currency, maker_limit,
                         created_by=None, created_by_snapshot=None, reason='',
                         changed_via=None):
    """Close the active row for (country, action) and open the next version.

    The one writer of policy rows: the seed command (first version, no
    request) and the `approvals.policy_change` executor (every later one).
    `created_by` is a user; `created_by_snapshot` an audit-shaped dict for
    when only the snapshot is to hand. Returns `(new_policy,
    superseded_policy_or_None)`.
    """
    country, currency, maker_limit = _validate_policy_fields(
        country, action_key, limit_kind, currency, maker_limit)
    now = timezone.now()
    with transaction.atomic():
        current = (
            ApprovalPolicy.objects.select_for_update()
            .filter(country=country, action_key=action_key, is_active=True)
            .first()
        )
        latest = (
            ApprovalPolicy.objects.filter(country=country, action_key=action_key)
            .order_by('-version').values_list('version', flat=True).first()
        ) or 0
        if current is not None:
            current.is_active = False
            current.valid_to = now
            current.save(update_fields=['is_active', 'valid_to'])
        policy = ApprovalPolicy(
            country=country, action_key=action_key, limit_kind=limit_kind,
            currency=currency, maker_limit=maker_limit, version=latest + 1,
            valid_from=now, reason=(reason or '')[:10000], changed_via=changed_via,
        )
        if created_by_snapshot is not None:
            policy.set_created_by_snapshot(created_by_snapshot)
        else:
            policy.set_created_by(created_by)
        policy.save()
    return policy, current


def propose_policy_change(actor, *, country, action_key, limit_kind, currency=None,
                          maker_limit=None, reason='', http_request=None):
    """Raise the maker-checker request that, once approved, writes a policy.

    Needs `platform.settings` over the country; out of the caller's scope
    answers 403 SCOPE_NOT_HELD, because there is no object here to be "not
    found". A reason is required: a threshold change with no stated reason
    is the kind of row an audit asks about first.
    """
    country, currency, maker_limit = _validate_policy_fields(
        country, action_key, limit_kind, currency, maker_limit)
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required to change a threshold.',
                             code='REASON_REQUIRED', field='reason')
    node = country_node(country)
    if not may_act(actor, 'platform.settings', node):
        raise NotPermitted(f'You do not hold platform.settings over {country}.',
                           code='SCOPE_NOT_HELD', field='country')
    payload = {
        'country': country, 'action_key': action_key, 'limit_kind': limit_kind,
        'currency': currency, 'maker_limit': maker_limit,
    }
    key = (f'policy_change:{country}:{action_key}:{limit_kind}:'
           f'{currency or "-"}:{maker_limit if maker_limit is not None else "none"}')
    return require_approval_or_execute(
        POLICY_CHANGE, actor=actor, country=country, scope_node=node,
        target_type='approval_policy', target_id=f'{country}:{action_key}',
        payload=payload, idempotency_key=key, reason=reason,
        http_request=http_request,
    )
