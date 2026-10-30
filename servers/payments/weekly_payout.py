"""The scheduled weekly payout run, for countries whose payout config says weekly.

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
PayoutRun is unique per (country, week_start), week_start being the Monday of
the week in the country's own zone. The beat task can run every hour; the
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
MAX_DRIVERS_PER_RUN = 2000
OPEN_STATUSES = ('pending', 'approved', 'processed', 'unresolved')


def week_start_of(now, tz):
    """The Monday (a date) of the week `now` falls in, in `tz`."""
    local = now.astimezone(tz)
    return local.date() - timedelta(days=local.weekday())


def _candidates(config):
    """Driver wallets that qualify on balance, without an open withdrawal."""
    from servers.driver.models import Driver, WithdrawalRequest
    from servers.rider.models import Wallet

    floor = max(1, config.minimum_withdrawal_minor)
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

    with transaction.atomic():
        wallet = (Wallet.objects.select_for_update()
                  .filter(user_id=driver.user_id_id, scope=Wallet.SCOPE_DRIVER,
                          currency=config.currency).first())
        if wallet is None or wallet.balance_minor < max(1, config.minimum_withdrawal_minor):
            return None
        if driver_payout_country(driver) != config.country:
            return None
        if WithdrawalRequest.objects.filter(driver=driver, status__in=OPEN_STATUSES).exists():
            return None
        amount = wallet.balance_minor
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
        wallet.balance_minor -= amount
        wallet.save(update_fields=['balance_minor'])
        record_admin_action(
            None, action='withdrawal_scheduled_created', target_type='withdrawal_request',
            target_id=withdrawal.id,
            after={'amount_minor': amount, 'currency': config.currency, 'fee_minor': fee,
                   'driver_id': driver.id, 'origin': 'scheduled'},
            reason=REASON)
    return withdrawal


def run_country(config, now=None):
    """Run one country's weekly payout if today is its day. Returns a summary dict."""
    from servers.approvals import service as approvals
    from servers.approvals.actions import FINANCE_PAYOUT
    from servers.approvals.executors import batch_key

    from .models import PayoutRun

    now = now or timezone.now()
    country = config.country
    tz = ZoneInfo(get_country(country).default_timezone)
    if config.payout_weekday is None or now.astimezone(tz).weekday() != config.payout_weekday:
        return {'country': country, 'ran': False, 'why': 'not_the_day'}
    week = week_start_of(now, tz)
    if PayoutRun.objects.filter(country=country, week_start=week).exists():
        return {'country': country, 'ran': False, 'why': 'already_ran', 'week_start': str(week)}

    candidates = list(_candidates(config))
    truncated = len(candidates) > MAX_DRIVERS_PER_RUN
    candidates = candidates[:MAX_DRIVERS_PER_RUN]

    try:
        with transaction.atomic(), audit_context(actor=None, actor_label=SYSTEM_LABEL,
                                                 reason=REASON):
            run = PayoutRun.objects.create(
                country=country, currency=config.currency, week_start=week,
                truncated=truncated, outcome='nothing_to_pay')
            from . import payout_rails

            needs_handle = payout_rails.has_rail(config.currency)
            skipped = []
            skipped_drivers = []
            payable = []
            for d in candidates:
                if needs_handle and not has_payout_handle(d):
                    skipped.append({'driver_id': d.id, 'reason': SKIP_NO_HANDLE})
                    skipped_drivers.append(d)
                else:
                    payable.append(d)
            created = [w for w in (_create_one(d, config) for d in payable) if w is not None]
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
                        idempotency_key=f'payout_run:{country}:{week}', reason=REASON)
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
                after={'country': country, 'week_start': str(week), 'withdrawals': len(created),
                       'amount_minor': run.amount_minor, 'currency': config.currency,
                       'approval_request_id': run.approval_request_id, 'outcome': run.outcome,
                       'truncated': truncated, 'skipped_no_handle': len(skipped)},
                reason=REASON)
            if skipped:
                _notify_skipped(run, config, skipped, skipped_drivers)
    except IntegrityError:
        # Another worker created this week's run between our check and our insert.
        return {'country': country, 'ran': False, 'why': 'already_ran', 'week_start': str(week)}
    return {'country': country, 'ran': True, 'week_start': str(week),
            'withdrawals': run.withdrawal_count, 'outcome': run.outcome,
            'approval_request_id': run.approval_request_id,
            'skipped_no_handle': [s['driver_id'] for s in run.skipped]}


def run_weekly_payouts(now=None):
    """Every weekly country, in order. A country with no config does nothing."""
    from .models import PayoutCountryConfig

    results = []
    for config in PayoutCountryConfig.objects.filter(
            is_active=True, payout_schedule=PayoutCountryConfig.WEEKLY).order_by('country'):
        try:
            results.append(run_country(config, now))
        except Exception:  # noqa: BLE001 - one country must not stop the others
            logger.exception('weekly payout failed for %s', config.country)
            results.append({'country': config.country, 'ran': False, 'why': 'error'})
    return results
