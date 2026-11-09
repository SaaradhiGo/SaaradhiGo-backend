"""The scheduled payout run: weekly, or every N days (owner ruling 2026-11-09), per country,
state or city (`payout_scope`: a state or city that sets its own schedule runs on its own cycle).

WHAT IT DOES
------------
A Celery beat task fires every hour. For each country whose ACTIVE
PayoutCountryConfig is weekly, it acts only when "now" in the country's own
time zone falls on the configured weekday. Then, once for that country and
week:

  0. a driver with NO PAYOUT HANDLE (no UPI contact with a fund account, no bank
     account) is skipped when the country's currency has a payout rail: nothing
     could be sent to them. They are listed in the run summary (`PayoutRun.skipped`)
     and every finance holder in scope gets an operator inbox item. Their balance
     is untouched and they are picked up the week they add a handle. A currency
     with no rail yet (USD) skips nobody: the withdrawals are created and held
     `awaiting_rail` once approved (payments.payout_rails);
  1. every driver in the country with a balance at or above the minimum and no
     open withdrawal gets a WithdrawalRequest (system actor, origin
     'scheduled', reason "scheduled weekly payout", fee from withdrawal_fee),
     and the amount leaves the wallet at once, exactly as a driver's own
     request does;
  2. ONE payout-run approval request is opened for all of them
     (FINANCE_PAYOUT with withdrawal_ids) through require_approval_or_execute,
     with the system as its maker. A system maker can only open a request:
     nothing is dispatched without a checker.

With no FINANCE_PAYOUT policy in the country the withdrawals stay pending and
the run is recorded as blocked_no_policy; the console says so, and Finance can
approve the batch once a policy exists.

IDEMPOTENT
----------
PayoutRun is unique per (country, scope_key, week_start): `scope_key` is '' for the country's own
schedule (else the state or city code), and `week_start` is the cycle's start: the Monday of the
week for a weekly schedule, the cycle date for every N days. The beat task can run every hour; the
second and later runs in the week find the row and do nothing. The whole run
(withdrawals, the PayoutRun row, the approval request) is one transaction, so
a crash leaves no half-run behind.

BOUNDED
-------
At most MAX_DRIVERS_PER_RUN drivers are paid in one run (one indexed wallet
scan, ordered by id); a run that hits the cap records truncated=True and the
rest wait for the next week.
"""

import logging
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from base.countries import get_country
from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action

logger = logging.getLogger(__name__)

SYSTEM_LABEL = 'system:weekly_payout'
REASON = 'scheduled weekly payout'
SKIPPED_KIND = 'payout_run_skipped'
SKIP_NO_HANDLE = 'no_payout_handle'
#: A suspended driver whose payouts are on hold (serious fraud, a second admin approved, a maximum
#: duration): skipped and listed for Finance. AUDIT 27.56.
SKIP_HELD = 'held'
MAX_DRIVERS_PER_RUN = 2000
OPEN_STATUSES = ('pending', 'approved', 'processed', 'unresolved', 'conflict')


def week_start_of(now, tz):
    """The Monday (a date) of the week `now` falls in, in `tz`."""
    local = now.astimezone(tz)
    return local.date() - timedelta(days=local.weekday())


def _candidates(config, floor=None):
    """Driver wallets that qualify on balance, without an open withdrawal."""
    from servers.driver.models import Driver, WithdrawalRequest
    from servers.rider.models import Wallet

    floor = max(1, config.minimum_withdrawal_minor if floor is None else floor)
    wallet_users = Wallet.objects.filter(
        scope=Wallet.SCOPE_DRIVER, currency=config.currency, balance_minor__gte=floor,
    ).values('user_id')
    open_withdrawal = WithdrawalRequest.objects.filter(
        driver=OuterRef('pk'), status__in=OPEN_STATUSES)
    return (Driver.objects.filter(user_id__in=wallet_users)
            .exclude(Exists(open_withdrawal))
            .select_related('user_id', 'home_city')
            .order_by('id')[:MAX_DRIVERS_PER_RUN + 1])


def has_payout_handle(driver):
    """Could a payout be sent to this driver? A UPI contact with something to send
    to, or a bank account."""
    from servers.driver.models import DriverBankAccount, DriverUPIContact

    upi = DriverUPIContact.objects.filter(driver=driver, is_active=True).first()
    if upi is not None and (upi.upi_id or upi.gateway_fund_account_id):
        return True
    return DriverBankAccount.objects.filter(driver=driver).exists()


def finance_holders(country, nodes=()):
    """Active operators holding finance.payout whose scope covers `nodes` (the
    skipped drivers' home cities), else the country itself."""
    from servers.admin_access.models import AdminAssignment, GeoNode

    targets = [n for n in nodes if n is not None]
    if not targets:
        node = GeoNode.objects.filter(kind='country', code=country).first()
        targets = [node] if node is not None else []
    out = {}
    for a in (AdminAssignment.objects.filter(is_active=True, role__is_active=True,
                                             admin__is_active=True)
              .select_related('role', 'scope', 'admin')):
        if 'finance.payout' not in (a.role.permissions or []):
            continue
        if a.scope.kind == 'global' or any(a.scope.covers(t) for t in targets):
            out[a.admin_id] = a.admin
    return list(out.values())


def deliver_payout_notice(notice):
    """Hand the notice to the notifications outbox (template `payout.missing_details`).

    One driver, so it is queued inline; a failure here must never fail the payout run, and the
    beat sweep (`notifications.dispatch_queued`) drains any row left `queued`."""
    from django.db import transaction

    try:
        from servers.notifications.events import queue_payout_notice
        with transaction.atomic():   # a savepoint: a failure must not poison the run's transaction
            return bool(queue_payout_notice(notice)['messages'])
    except Exception:  # noqa: BLE001
        logger.exception('payout_notice_handoff_failed notice_id=%s', notice.id)
        return False


def _queue_driver_notices(run, drivers):
    """One outbox row per skipped driver per run. Idempotent: (run, driver) is unique."""
    from .models import DriverPayoutNotice

    for d in drivers:
        notice, created = DriverPayoutNotice.objects.get_or_create(run=run, driver=d)
        if created:
            deliver_payout_notice(notice)


def _notify_skipped(run, config, skipped, drivers):
    """One inbox item per finance holder in scope: who was skipped and why."""
    from servers.support.models import OperatorNotification

    text = (f'Weekly payout run {config.country} (week of {run.week_start}): '
            f'{len(skipped)} driver(s) skipped because they have no payout handle '
            f'(no UPI or bank account). Driver ids: '
            f'{", ".join(str(s["driver_id"]) for s in skipped[:20])}'
            f'{" and more" if len(skipped) > 20 else ""}.')
    for user in finance_holders(config.country, [d.home_city for d in drivers]):
        OperatorNotification.objects.get_or_create(
            recipient=user, kind=SKIPPED_KIND, approval_request_id=None,
            text=text[:2000])


def _create_one(driver, config):
    """Create the scheduled withdrawal for one driver, or None if they no longer qualify."""
    from servers.driver.models import WithdrawalRequest
    from servers.rider.models import Wallet, WalletTransaction

    from .payout_config import driver_payout_country, withdrawal_fee

    from servers.fraud import money as fraud_money

    floor = max(1, config.minimum_withdrawal_minor)
    with transaction.atomic():
        wallet = (Wallet.objects.select_for_update()
                  .filter(user_id=driver.user_id_id, scope=Wallet.SCOPE_DRIVER,
                          currency=config.currency).first())
        if wallet is None or wallet.balance_minor < floor:
            return None
        if driver_payout_country(driver) != config.country:
            return None
        if WithdrawalRequest.objects.filter(driver=driver, status__in=OPEN_STATUSES).exists():
            return None
        if fraud_money.active_payout_hold(driver) is not None:
            return None
        # A reward pending review stays in the wallet: it is not part of this payout.
        gross = wallet.balance_minor - fraud_money.held_minor(driver.user_id, config.currency)
        if gross < floor:
            return None
        # A recovery Finance approved comes off this payout, at most `recovery_max_percent` of it,
        # never so much that the payout falls below the minimum; the rest carries over.
        percent = config.recovery_max_percent
        deduct, plan = fraud_money.plan_deduction(driver, gross, floor, percent)
        amount = gross - deduct
        fee = withdrawal_fee(config, amount)
        if fee >= amount:
            return None
        withdrawal = WithdrawalRequest.objects.create(
            driver=driver, amount_minor=amount, currency=config.currency, status='pending',
            origin='scheduled', admin_notes=REASON, fee_minor=fee)
        WalletTransaction.objects.create(
            user_id=driver.user_id, amount_minor=amount, currency=config.currency,
            txn_type='debit', status='pending', purpose='withdrawal',
            reference_id=f'withdrawal_{withdrawal.id}',
            idempotency_key=f'withdrawal_{withdrawal.id}_{uuid.uuid4().hex[:16]}')
        wallet.balance_minor -= gross
        wallet.save(update_fields=['balance_minor'])
        if plan:
            fraud_money.apply_deduction(driver, plan, withdrawal, gross, percent)
        record_admin_action(
            None, action='withdrawal_scheduled_created', target_type='withdrawal_request',
            target_id=withdrawal.id,
            after={'amount_minor': amount, 'currency': config.currency, 'fee_minor': fee,
                   'driver_id': driver.id, 'origin': 'scheduled',
                   'recovery_deducted_minor': deduct, 'payout_gross_minor': gross},
            reason=REASON)
    return withdrawal


def _groups(config):
    """The country's drivers who qualify, grouped by the schedule that pays them:
    `{scope_key: (EffectivePayoutConfig of the scope, [(driver, their effective config)])}`.
    The country's own scope is always present (a run with nothing to pay is still recorded)."""
    from .models import PayoutScopeOverride
    from .payout_scope import EffectivePayoutConfig, effective_for_driver

    mins = [config.minimum_withdrawal_minor] + [
        m for m in PayoutScopeOverride.objects.filter(
            country=config.country, is_active=True, minimum_withdrawal_minor__isnull=False)
        .values_list('minimum_withdrawal_minor', flat=True)]
    groups = {'': (EffectivePayoutConfig(config), [])}
    for driver in _candidates(config, floor=min(mins)):
        _, eff = effective_for_driver(driver)
        if eff is None or eff.base.pk != config.pk:
            continue
        key = eff.schedule_scope
        if key not in groups:
            groups[key] = (eff, [])
        groups[key][1].append((driver, eff))
    return groups


def run_country(config, now=None):
    """Run every schedule in one country whose cycle is due now. Returns a summary dict (the
    country's own schedule) with the state and city runs under `scopes`."""
    now = now or timezone.now()
    results = {}
    for key, (eff, members) in _groups(config).items():
        results[key] = run_scope(eff, key, members, now)
    out = dict(results.get('', {'country': config.country, 'ran': False, 'why': 'not_the_day'}))
    out['scopes'] = {k: v for k, v in results.items() if k}
    return out


def run_scope(config, scope_key, members, now):
    """One schedule's run: the country's (scope_key '') or a state's or city's override."""
    from servers.approvals import service as approvals
    from servers.approvals.actions import FINANCE_PAYOUT
    from servers.approvals.executors import batch_key

    from .models import PayoutRun
    from .payout_scope import SCHEDULED, due_cycle

    country = config.country
    if config.payout_schedule not in SCHEDULED:
        return {'country': country, 'scope': scope_key, 'ran': False, 'why': 'not_scheduled'}
    week = due_cycle(config, now)
    if week is None:
        return {'country': country, 'scope': scope_key, 'ran': False, 'why': 'not_the_day'}
    if PayoutRun.objects.filter(country=country, scope_key=scope_key, week_start=week).exists():
        return {'country': country, 'scope': scope_key, 'ran': False, 'why': 'already_ran',
                'week_start': str(week)}

    by_driver = {d.id: eff for d, eff in members}
    candidates = [d for d, _ in members]
    truncated = len(candidates) > MAX_DRIVERS_PER_RUN
    candidates = candidates[:MAX_DRIVERS_PER_RUN]

    try:
        with transaction.atomic(), audit_context(actor=None, actor_label=SYSTEM_LABEL,
                                                 reason=REASON):
            run = PayoutRun.objects.create(
                country=country, scope_key=scope_key, currency=config.currency, week_start=week,
                truncated=truncated, outcome='nothing_to_pay')
            from . import payout_rails

            needs_handle = payout_rails.has_rail(config.currency)
            skipped = []
            skipped_drivers = []
            payable = []
            from servers.fraud import money as fraud_money

            for d in candidates:
                if fraud_money.active_payout_hold(d, now) is not None:
                    skipped.append({'driver_id': d.id, 'reason': SKIP_HELD})
                    continue
                if needs_handle and not has_payout_handle(d):
                    skipped.append({'driver_id': d.id, 'reason': SKIP_NO_HANDLE})
                    skipped_drivers.append(d)
                else:
                    payable.append(d)
            created = [w for w in (_create_one(d, by_driver[d.id]) for d in payable)
                       if w is not None]
            run.skipped = skipped
            run.withdrawal_count = len(created)
            run.amount_minor = sum(w.amount_minor for w in created)
            if created:
                ids = sorted(w.id for w in created)
                try:
                    outcome = approvals.require_approval_or_execute(
                        FINANCE_PAYOUT, actor=None, system_label=SYSTEM_LABEL, country=country,
                        amount=run.amount_minor, currency=config.currency, scope_node=None,
                        target_type='withdrawal_batch',
                        target_id=batch_key(ids).split(':', 1)[1],
                        payload={'withdrawal_ids': ids, 'payout_run': run.id,
                                 'week_start': str(week)},
                        idempotency_key=(f'payout_run:{country}:{week}' if not scope_key
                                         else f'payout_run:{scope_key}:{week}'),
                        reason=REASON)
                    run.approval_request_id = outcome.request.id
                    run.outcome = 'opened'
                except approvals.ThresholdNotConfigured:
                    run.outcome = 'blocked_no_policy'
                except approvals.ApprovalError as exc:
                    logger.error('weekly payout %s %s: approval refused: %s', country, week, exc)
                    run.outcome = f'approval_refused:{exc.code}'[:24]
            run.save()
            record_admin_action(
                None, action='payout_run_created', target_type='payout_run', target_id=run.id,
                after={'country': country, 'scope': scope_key, 'week_start': str(week),
                       'withdrawals': len(created),
                       'amount_minor': run.amount_minor, 'currency': config.currency,
                       'approval_request_id': run.approval_request_id, 'outcome': run.outcome,
                       'truncated': truncated, 'skipped_no_handle': len(skipped_drivers),
                       'skipped_held': len(skipped) - len(skipped_drivers)},
                reason=REASON)
            if skipped_drivers:
                _notify_skipped(run, config, [s for s in skipped if s['reason'] == SKIP_NO_HANDLE],
                                skipped_drivers)
                _queue_driver_notices(run, skipped_drivers)
    except IntegrityError:
        # Another worker created this cycle's run between our check and our insert.
        return {'country': country, 'scope': scope_key, 'ran': False, 'why': 'already_ran',
                'week_start': str(week)}
    return {'country': country, 'scope': scope_key, 'ran': True, 'week_start': str(week),
            'withdrawals': run.withdrawal_count, 'outcome': run.outcome,
            'approval_request_id': run.approval_request_id,
            'skipped_no_handle': [s['driver_id'] for s in run.skipped
                                  if s.get('reason') == SKIP_NO_HANDLE],
            'skipped_held': [s['driver_id'] for s in run.skipped
                             if s.get('reason') == SKIP_HELD]}


def run_weekly_payouts(now=None):
    """Every country with a scheduled payout (its own, or a state's or city's), in order. The
    name predates cycles; the beat task still calls it hourly. No config, nothing done."""
    from django.db.models import Q

    from .models import PayoutCountryConfig, PayoutScopeOverride

    scheduled = ('weekly', 'every_n_days')
    with_override = PayoutScopeOverride.objects.filter(
        is_active=True, payout_schedule__in=scheduled).values_list('country', flat=True)
    results = []
    for config in PayoutCountryConfig.objects.filter(is_active=True).filter(
            Q(payout_schedule__in=scheduled) | Q(country__in=list(with_override))
    ).order_by('country'):
        try:
            results.append(run_country(config, now))
        except Exception:  # noqa: BLE001 - one country must not stop the others
            logger.exception('weekly payout failed for %s', config.country)
            results.append({'country': config.country, 'ran': False, 'why': 'error'})
    return results
