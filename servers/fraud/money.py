"""Fraud money actions (owner rulings 2026-10-31, AUDIT 27.56; binding).

    a. REWARD HOLD    hold ONLY the unpaid incentive / referral reward linked to the flagged
                      activity, never trip earnings. Auto-releases after `reward_hold_days` (per
                      country) if the flag is not decided; a dismissal releases it at once.
    b. RECOVERY       a confirmed promo abuse / collusion flag may claw back linked rewards through a
                      Finance adjustment request (always a checker), never more than the reward;
                      a payout is reduced by at most `recovery_max_percent` for it, the rest carries.
    c. ONLINE ONLY    see `servers.rider` (`RiderRestriction`, `CASH_NOT_AVAILABLE`) and
                      `restrict_cash` below.
    d. PAYOUT HOLD    never automatic. Only with a suspension, a second admin, and a maximum
                      duration (`payout_hold_max_days`) after which it lapses unless renewed.

WHAT "LINKED" MEANS (the exact rule)
------------------------------------
A reward is linked to a flag when it belongs to the flag's SUBJECT and
  * referral_abuse     it is a `referral_reward` of the flagged referral (`evidence.referral_id`) or
                       of any referral between the subject and a counterpart named in
                       `evidence.refs.counterpart_user_ids`;
  * multi_accounting   it is a `referral_reward` of the subject (any referral);
  * incentive_farming  it is an `incentive_reward` of a campaign named in `evidence.refs.campaign_ids`;
  * collusion          it is an `incentive_reward` of a campaign that counted one of the flagged
                       trips (`evidence.refs.trip_ids`) for the subject driver, or a `referral_reward`
                       of a referral between the subject and a named counterpart;
  * any other kind     nothing is linked (no hold, no recovery).
Trip earnings, commissions and platform-funded lines are never touched: this module only reads
`promotions.LedgerEntry` rows of kind incentive_reward / referral_reward.

WHAT "UNPAID" MEANS
-------------------
The reward is already a credit on the wallet (it is swept into the next payout with everything
else). It is UNPAID while no non-rejected, non-failed withdrawal was created after the credit (the
weekly run, or a driver's request, takes the balance). A hold does not move the wallet balance: it
is a reserve that the weekly run, a driver's withdrawal and a rider's spend leave out
(`held_minor`), capped at what the wallet still holds so a hold can never exceed the balance.

NOTHING DRIVER- OR RIDER-FACING SAYS "FRAUD"
--------------------------------------------
A held reward reads "Pending review"; a recovery line reads "Recovery of reward (reviewed)". The
ledger purpose of the recovery debit is `reward_recovery`, not a fraud name.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action
from servers.approvals import adjust, registry
from servers.approvals.actions import FINANCE_ADJUST, FINANCE_PAYOUT_HOLD

from .models import (
    ACTIVE_STATUSES, COLLUSION, CONFIRMED, EV_NOTE, INCENTIVE_FARMING, MULTI_ACCOUNTING,
    REFERRAL_ABUSE, FlagEvent, PayoutHold, RecoveryDebt, RecoveryDeduction, RewardHold, RiskFlag,
)

logger = logging.getLogger(__name__)

SYSTEM_LABEL = 'system:fraud_money'
PENDING_REVIEW = 'pending_review'
PENDING_REVIEW_LABEL = 'Pending review'
RECOVERY_LABEL = 'Recovery of reward (reviewed)'
#: The ledger purpose of a recovery debit. Deliberately not a fraud word: it reaches the driver's
#: and rider's own responses.
RECOVERY_PURPOSE = 'reward_recovery'
RECOVERY_REASON_CODE = 'fraud_recovery'
RECOVERY_TARGET = 'fraud_recovery'

#: Flag kinds that can link rewards.
REWARD_KINDS = (REFERRAL_ABUSE, MULTI_ACCOUNTING, COLLUSION, INCENTIVE_FARMING)
#: Confirm reasons under which a confirmed flag may recover rewards (ruling b).
RECOVERY_REASONS = ('promo_abuse', 'collusion', 'multi_accounting')
#: Confirm reasons that count as SERIOUS fraud for a payout hold (ruling d): fake or spoofed trips
#: and collusion. A decision, flagged in the PR: the ruling names "fake trips" as the example.
SERIOUS_REASONS = ('gps_spoofing', 'collusion')

DEFAULT_SETTINGS = {'reward_hold_days': 7, 'recovery_max_percent': 50, 'payout_hold_max_days': 14}


class MoneyError(Exception):
    def __init__(self, code, message, status=400, field='general'):
        super().__init__(message)
        self.code, self.message, self.status, self.field = code, message, status, field


# ---------------------------------------------------------------------------
# Config and places
# ---------------------------------------------------------------------------

def country_of_user(user):
    from servers.notifications import recipients

    return recipients.country_of(user)


def settings_for(country):
    """The three per-country numbers (the active payout config row; the defaults if none)."""
    from servers.payments.payout_config import active_config

    config = active_config(country)
    if config is None:
        return dict(DEFAULT_SETTINGS)
    return {k: getattr(config, k) for k in DEFAULT_SETTINGS}


def _role(user):
    return 'driver' if getattr(user, 'role', '') == 'driver' else 'rider'


def _driver_of(user):
    return getattr(user, 'driver', None) if getattr(user, 'role', '') == 'driver' else None


def _wallet(user, lock=False):
    from servers.rider.models import Wallet, get_wallet

    scope = Wallet.SCOPE_DRIVER if _role(user) == 'driver' else Wallet.SCOPE_RIDER
    return get_wallet(user, scope, lock=lock)


# ---------------------------------------------------------------------------
# Which rewards are linked
# ---------------------------------------------------------------------------

def _referral_keys(subject, evidence, refs):
    from servers.promotions.models import Referral

    counterparts = [int(x) for x in refs.get('counterpart_user_ids') or []]
    query = Q(pk=-1)
    if evidence.get('referral_id'):
        query |= Q(pk=int(evidence['referral_id']))
    if counterparts:
        query |= (Q(referrer=subject, referee_id__in=counterparts)
                  | Q(referee=subject, referrer_id__in=counterparts))
    keys = []
    for r in Referral.objects.filter(query):
        if r.referrer_id == subject.pk:
            keys.append(f'referral:{r.pk}:referrer')
        if r.referee_id == subject.pk:
            keys.append(f'referral:{r.pk}:referee')
    return keys


def linked_entries(flag):
    """The `LedgerEntry` rows linked to `flag` (see the module docstring), oldest first."""
    from servers.promotions.models import INCENTIVE_REWARD, REFERRAL_REWARD, IncentiveTrip, LedgerEntry

    if flag.kind not in REWARD_KINDS:
        return []
    subject = flag.subject
    evidence = flag.evidence or {}
    refs = evidence.get('refs') or {}
    base = LedgerEntry.objects.filter(user=subject).select_related('wallet_transaction', 'campaign')
    found = {}
    if flag.kind in (REFERRAL_ABUSE, COLLUSION):
        keys = _referral_keys(subject, evidence, refs)
        if keys:
            for e in base.filter(kind=REFERRAL_REWARD, idempotency_key__in=keys):
                found[e.pk] = e
    if flag.kind == MULTI_ACCOUNTING:
        for e in base.filter(kind=REFERRAL_REWARD):
            found[e.pk] = e
    if flag.kind in (INCENTIVE_FARMING, COLLUSION):
        campaigns = {int(c) for c in refs.get('campaign_ids') or []} if flag.kind == INCENTIVE_FARMING else set()
        driver = _driver_of(subject)
        trip_ids = [int(t) for t in refs.get('trip_ids') or []]
        if flag.kind == COLLUSION and driver is not None and trip_ids:
            campaigns |= set(IncentiveTrip.objects.filter(
                driver=driver, trip_id__in=trip_ids).values_list('campaign_id', flat=True))
        if campaigns:
            for e in base.filter(kind=INCENTIVE_REWARD, campaign_id__in=campaigns):
                found[e.pk] = e
    return sorted(found.values(), key=lambda e: (e.created_at, e.pk))


def _paid(entry, driver):
    """Has a payout taken this reward? (Drivers only: a rider's wallet credit has no payout.)"""
    if driver is None:
        return False
    from servers.driver.models import WithdrawalRequest

    return WithdrawalRequest.objects.filter(
        driver=driver, requested_at__gte=entry.created_at).exclude(
        status__in=('rejected', 'failed')).exists()


def held_minor(user, currency=None):
    """The reward amounts currently held (pending review) on this person's own wallet."""
    qs = RewardHold.objects.filter(user=user, status=RewardHold.HELD, role=_role(user))
    if currency:
        qs = qs.filter(currency=currency)
    return qs.aggregate(t=Sum('amount_minor'))['t'] or 0


def available_minor(user):
    """The wallet balance NOT held for review (never below zero)."""
    wallet = _wallet(user)
    return max(0, wallet.balance_minor - held_minor(user, wallet.currency))


# ---------------------------------------------------------------------------
# Ruling a: hold
# ---------------------------------------------------------------------------

def _audit(action, target_type, target_id, before=None, after=None, reason='', request=None):
    record_admin_action(request, action=action, target_type=target_type, target_id=target_id,
                        before=before, after=after, reason=reason)


def _note(flag, text, actor=None, data=None, reason=''):
    from .review import _event

    _event(flag, EV_NOTE, actor, from_status=flag.status, to_status=flag.status, reason=reason,
           note=text[:500], data=data or {})


def hold_for_flag(flag, now=None):
    """Hold the unpaid linked rewards of an ACTIVE flag. Idempotent; returns the new holds.

    Never touches a reward that was held before (a released one is not held again, so a flag
    cannot keep a reward in limbo by being touched again), nor any reward already paid out.
    """
    now = now or timezone.now()
    if flag.status not in ACTIVE_STATUSES:
        return []
    entries = linked_entries(flag)
    if not entries:
        return []
    subject = flag.subject
    driver = _driver_of(subject)
    days = settings_for(country_of_user(subject))['reward_hold_days']
    created = []
    with audit_context(actor=None, actor_label=SYSTEM_LABEL, reason='reward held pending review'):
        with transaction.atomic():
            wallet = _wallet(subject, lock=True)
            for entry in entries:
                if RewardHold.objects.filter(ledger_entry=entry).exists():
                    continue
                if _paid(entry, driver) or entry.currency != wallet.currency:
                    continue
                room = wallet.balance_minor - held_minor(subject, wallet.currency)
                amount = min(entry.amount_minor, room)
                if amount <= 0:
                    continue
                hold = RewardHold.objects.create(
                    flag=flag, user=subject, role=_role(subject), ledger_entry=entry,
                    kind=entry.kind, amount_minor=amount, reward_minor=entry.amount_minor,
                    currency=entry.currency, held_at=now, release_at=now + timedelta(days=days))
                created.append(hold)
                _audit('reward_held', 'risk_flag', flag.pk,
                       after={'hold_id': hold.pk, 'amount_minor': amount,
                              'currency': entry.currency, 'kind': entry.kind,
                              'release_at': hold.release_at.isoformat()},
                       reason='unpaid reward linked to the flagged activity, pending review')
            if created:
                total = sum(h.amount_minor for h in created)
                _note(flag, f'Reward held pending review: {total} minor units on '
                            f'{len(created)} reward(s); released automatically after {days} days '
                            f'if the flag is not decided.',
                      data={'reward_holds': [h.pk for h in created], 'amount_minor': total},
                      reason='reward_hold')
    return created


def release_hold(hold, reason, now=None):
    """Release one hold (the reward is paid out normally from now on)."""
    now = now or timezone.now()
    hold.status, hold.released_at, hold.release_reason = RewardHold.RELEASED, now, reason
    hold.save(update_fields=['status', 'released_at', 'release_reason'])
    _audit('reward_released', 'risk_flag', hold.flag_id,
           before={'hold_id': hold.pk, 'status': 'held'},
           after={'hold_id': hold.pk, 'status': 'released', 'amount_minor': hold.amount_minor,
                  'reason': reason}, reason=RewardHold.RELEASE_REASONS.get(reason, reason))


def release_for_flag(flag, reason, now=None):
    holds = list(RewardHold.objects.select_for_update().filter(flag=flag, status=RewardHold.HELD))
    for hold in holds:
        release_hold(hold, reason, now)
    if holds:
        _note(flag, f'{len(holds)} held reward(s) released: '
                    f'{RewardHold.RELEASE_REASONS.get(reason, reason)}.',
              data={'reward_holds': [h.pk for h in holds]}, reason='reward_release')
    return holds


def recovery_applies(flag):
    """May this CONFIRMED flag claw rewards back? (Ruling b.)"""
    return (flag.status == CONFIRMED and flag.kind in REWARD_KINDS
            and flag.decision_reason in RECOVERY_REASONS and bool(linked_entries(flag)))


def on_decision(flag, decision, now=None):
    """Called by `review.decide` inside its transaction.

    DISMISSED: release at once. CONFIRMED under a reason that recovers rewards (promo abuse,
    collusion, multiple accounts): the hold STAYS so Finance can recover it, and its clock restarts
    (`reward_hold_days` from the decision, the longest it can wait for Finance); under any other
    reason it is released. Nothing is forfeited here: only a Finance approval takes a reward back.
    """
    now = now or timezone.now()
    with audit_context(actor=None, actor_label=SYSTEM_LABEL, reason='flag decided'):
        if decision == CONFIRMED and flag.decision_reason in RECOVERY_REASONS:
            days = settings_for(country_of_user(flag.subject))['reward_hold_days']
            for hold in RewardHold.objects.select_for_update().filter(
                    flag=flag, status=RewardHold.HELD):
                hold.release_at = now + timedelta(days=days)
                hold.save(update_fields=['release_at'])
            return 'kept_for_finance'
        reason = 'dismissed' if decision != CONFIRMED else 'confirmed_no_recovery'
        released = release_for_flag(flag, reason, now)
    return 'released' if released else 'none'


def release_due(now=None):
    """The beat sweep: release every hold whose time is up. Returns how many."""
    now = now or timezone.now()
    count = 0
    with audit_context(actor=None, actor_label=SYSTEM_LABEL, reason='reward hold ran out'):
        for pk in list(RewardHold.objects.filter(status=RewardHold.HELD, release_at__lte=now)
                       .values_list('pk', flat=True)):
            with transaction.atomic():
                hold = (RewardHold.objects.select_for_update().select_related('flag')
                        .filter(pk=pk, status=RewardHold.HELD).first())
                if hold is None:
                    continue
                reason = 'expired_after_confirm' if hold.flag.status == CONFIRMED else 'auto_expired'
                release_hold(hold, reason, now)
                _note(hold.flag, f'Held reward released automatically: '
                                 f'{RewardHold.RELEASE_REASONS[reason]}.',
                      data={'reward_holds': [hold.pk]}, reason='reward_release')
                count += 1
    return count


# ---------------------------------------------------------------------------
# What the flag detail shows (console) and what the apps show
# ---------------------------------------------------------------------------

def _pending_recovery_minor(flag):
    from servers.approvals.models import REQUESTED, ApprovalRequest

    total = 0
    for r in ApprovalRequest.objects.filter(action_key=FINANCE_ADJUST, target_type=RECOVERY_TARGET,
                                            status=REQUESTED):
        if (r.payload or {}).get('flag_id') == flag.pk:
            total += int((r.payload or {}).get('amount_minor') or 0)
    return total


def recoverable_minor(flag):
    """What this flag may still recover: linked rewards, less what is already requested/taken."""
    if not recovery_applies(flag):
        return 0
    linked = sum(e.amount_minor for e in linked_entries(flag))
    used = RecoveryDebt.objects.filter(flag=flag).aggregate(t=Sum('requested_minor'))['t'] or 0
    return max(0, linked - used - _pending_recovery_minor(flag))


def reward_summary(flag):
    """For the flag detail: each linked reward as held / paid / credited, and the totals."""
    entries = linked_entries(flag)
    if not entries:
        return {'rewards': [], 'currency': None, 'linked_minor': 0, 'held_minor': 0,
                'paid_minor': 0, 'recoverable_minor': 0, 'can_recover': False,
                'hold_days': None, 'debts': []}
    driver = _driver_of(flag.subject)
    holds = {h.ledger_entry_id: h for h in RewardHold.objects.filter(flag=flag).order_by('id')}
    rows, held, paid = [], 0, 0
    for e in entries:
        hold = holds.get(e.pk)
        if hold is not None and hold.status == RewardHold.HELD:
            state, held = 'held', held + hold.amount_minor
        elif hold is not None and hold.status == RewardHold.FORFEITED:
            state = 'forfeited'
        elif _paid(e, driver):
            state, paid = 'paid', paid + e.amount_minor
        else:
            state = 'credited'
        rows.append({
            'ledger_entry_id': e.pk, 'kind': e.kind, 'campaign_id': e.campaign_id,
            'campaign': e.campaign.name if e.campaign_id else '',
            'amount_minor': e.amount_minor, 'currency': e.currency, 'state': state,
            'held_minor': hold.amount_minor if hold and hold.status == RewardHold.HELD else 0,
            'release_at': hold.release_at.isoformat() if hold and hold.status == RewardHold.HELD
            else None,
            'credited_at': e.created_at.isoformat()})
    debts = [{'id': d.pk, 'requested_minor': d.requested_minor, 'forfeited_minor': d.forfeited_minor,
              'amount_minor': d.amount_minor, 'recovered_minor': d.recovered_minor,
              'uncollected_minor': d.uncollected_minor, 'remaining_minor': d.remaining_minor,
              'currency': d.currency, 'created_at': d.created_at.isoformat()}
             for d in RecoveryDebt.objects.filter(flag=flag)]
    recoverable = recoverable_minor(flag)
    return {
        'rewards': rows, 'currency': entries[0].currency,
        'linked_minor': sum(e.amount_minor for e in entries), 'held_minor': held,
        'paid_minor': paid, 'recoverable_minor': recoverable,
        'can_recover': recoverable > 0,
        'hold_days': settings_for(country_of_user(flag.subject))['reward_hold_days'],
        'debts': debts,
    }


def pending_review_lines(user):
    """What the driver's or rider's own app shows: the held amount, as 'pending review'."""
    from base.money import from_minor

    wallet = _wallet(user)
    held = held_minor(user, wallet.currency)
    return {'rewards_pending_review': str(from_minor(held, wallet.currency)),
            'rewards_pending_review_label': PENDING_REVIEW_LABEL,
            'rewards_pending_review_minor': held, 'currency': wallet.currency}


def reward_status_for(user, ledger_entry_ids):
    """`{ledger_entry_id: 'pending_review'}` for the rewards of `user` currently held."""
    return {h.ledger_entry_id: PENDING_REVIEW for h in RewardHold.objects.filter(
        user=user, status=RewardHold.HELD, ledger_entry_id__in=list(ledger_entry_ids))}


# ---------------------------------------------------------------------------
# Ruling b: recovery (a Finance adjustment, always a checker)
# ---------------------------------------------------------------------------

def request_recovery(actor, flag, amount_minor, note, key, http_request=None):
    """Raise the Finance adjustment request. Nothing moves until a different admin approves.

    Server-enforced: the flag is confirmed under a recovery reason, `0 < amount <= recoverable`
    (linked rewards less what is already requested or taken: never more than the reward).
    """
    from servers.admin_access.bridge import granted
    from servers.approvals import service as approvals

    if not recovery_applies(flag):
        raise MoneyError('RECOVERY_NOT_APPLICABLE',
                         'Rewards can be recovered only from a confirmed promotion-abuse, '
                         'collusion or multiple-accounts flag with linked rewards.', 409, 'status')
    if not (granted(actor, 'finance.adjust', flag.anchor_node)[0]):
        raise MoneyError('PERMISSION_DENIED', 'You do not hold finance.adjust here.', 403,
                         'permission')
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise MoneyError('INVALID_AMOUNT', 'amount_minor must be a whole number above zero.', 400,
                         'amount_minor')
    cap = recoverable_minor(flag)
    if amount_minor > cap:
        raise MoneyError('AMOUNT_EXCEEDS_REWARD',
                         f'At most {cap} can be recovered: never more than the linked rewards.',
                         400, 'amount_minor')
    note = (note or '').strip()
    if len(note) < 5:
        raise MoneyError('INVALID_NOTE', 'A note of at least 5 characters is required.', 400,
                         'note')
    subject = flag.subject
    entries = linked_entries(flag)
    wallet = _wallet(subject)
    country = country_of_user(subject)
    if country is None:
        raise MoneyError('COUNTRY_UNKNOWN', 'This person cannot be placed in a country.', 409,
                         'flag_id')
    payload = {'flag_id': flag.pk, 'user_id': subject.pk, 'role': _role(subject),
               'amount_minor': amount_minor, 'currency': entries[0].currency,
               'reason_code': RECOVERY_REASON_CODE, 'note': note[:500],
               # Always a checker, whatever the Finance adjustment threshold says.
               'requires_checker': True, 'wallet_currency': wallet.currency}
    outcome = approvals.require_approval_or_execute(
        FINANCE_ADJUST, actor=actor, country=country, amount=amount_minor,
        currency=entries[0].currency, scope_node=flag.anchor_node, target_type=RECOVERY_TARGET,
        target_id=flag.pk, payload=payload,
        idempotency_key=f'fraud_recovery:{flag.pk}:{key}'[:200],
        reason=f'Recovery of reward (reviewed): {note}'[:2000], http_request=http_request)
    return outcome


@adjust.register(RECOVERY_TARGET)
def execute_recovery(request_row):
    """Runs when a checker approves. Held rewards first (forfeited), the rest becomes a debt.

    DRIVER: the remainder is collected from payouts (capped per payout), never more than approved.
    RIDER: taken from the wallet balance now, never below zero; the rest is recorded and shown to
    Finance, nothing else is collected.
    """
    from servers.rider.models import WalletTransaction

    p = request_row.payload or {}
    amount = int(p.get('amount_minor') or 0)
    flag = RiskFlag.objects.select_related('subject').filter(pk=p.get('flag_id')).first()
    if flag is None or amount <= 0:
        raise registry.ExecutionRefused('BAD_RECOVERY', 'The recovery request is not valid.')
    existing = RecoveryDebt.objects.filter(approval_request=request_row).first()
    if existing is not None:
        return {'recovery_debt_id': existing.pk, 'duplicate': True}
    if not recovery_applies(flag):
        raise registry.ExecutionRefused(
            'RECOVERY_NOT_APPLICABLE', 'The flag is no longer confirmed for recovery.')
    with transaction.atomic():
        cap = recoverable_minor(flag) + _own_pending(flag, request_row)
        if amount > cap:
            raise registry.ExecutionRefused(
                'AMOUNT_EXCEEDS_REWARD',
                'The recovery is more than the linked rewards still allow.')
        subject = flag.subject
        wallet = _wallet(subject, lock=True)
        if wallet.currency != p.get('currency'):
            raise registry.ExecutionRefused('CURRENCY_CHANGED', "The wallet's currency changed.")
        remaining, forfeited = amount, 0
        for hold in RewardHold.objects.select_for_update().filter(
                flag=flag, user=subject, status=RewardHold.HELD).order_by('id'):
            if hold.amount_minor > remaining or hold.amount_minor > wallet.balance_minor:
                continue
            key = f'recovery_{request_row.pk}_hold_{hold.pk}'
            WalletTransaction.objects.create(
                user_id=subject, currency=wallet.currency, amount_minor=hold.amount_minor,
                txn_type='debit', status='completed', purpose=RECOVERY_PURPOSE,
                reference_id=f'RECOVERY_{request_row.pk}', idempotency_key=key)
            wallet.balance_minor -= hold.amount_minor
            hold.status, hold.released_at = RewardHold.FORFEITED, timezone.now()
            hold.release_reason, hold.forfeited_via = 'forfeited_by_recovery', request_row
            hold.save(update_fields=['status', 'released_at', 'release_reason', 'forfeited_via'])
            remaining -= hold.amount_minor
            forfeited += hold.amount_minor
        wallet.save(update_fields=['balance_minor'])
        debt = RecoveryDebt.objects.create(
            user=subject, role=_role(subject), flag=flag, approval_request=request_row,
            requested_minor=amount, forfeited_minor=forfeited, amount_minor=remaining,
            currency=wallet.currency, note=(p.get('note') or '')[:500])
        taken = uncollected = 0
        if _role(subject) == 'rider' and remaining > 0:
            free = max(0, wallet.balance_minor - held_minor(subject, wallet.currency))
            taken = min(remaining, free)
            if taken > 0:
                WalletTransaction.objects.create(
                    user_id=subject, currency=wallet.currency, amount_minor=taken,
                    txn_type='debit', status='completed', purpose=RECOVERY_PURPOSE,
                    reference_id=f'RECOVERY_{request_row.pk}',
                    idempotency_key=f'recovery_{request_row.pk}_wallet')
                wallet.balance_minor -= taken
                wallet.save(update_fields=['balance_minor'])
            uncollected = remaining - taken
            debt.recovered_minor, debt.uncollected_minor = taken, uncollected
            debt.closed_at = timezone.now()
            debt.save(update_fields=['recovered_minor', 'uncollected_minor', 'closed_at'])
        if debt.amount_minor == 0 and debt.closed_at is None:
            debt.closed_at = timezone.now()
            debt.save(update_fields=['closed_at'])
        record_admin_action(
            None, action='fraud_recovery_executed', target_type='risk_flag', target_id=flag.pk,
            after={'recovery_debt_id': debt.pk, 'requested_minor': amount,
                   'forfeited_minor': forfeited, 'debt_minor': remaining,
                   'taken_now_minor': taken, 'uncollected_minor': uncollected,
                   'currency': wallet.currency, 'approval_request_id': request_row.pk},
            reason=request_row.request_reason)
        _note(flag, f'Recovery approved: {amount} minor units ({forfeited} from held rewards).',
              data={'recovery_debt_id': debt.pk}, reason='recovery')
    return {'recovery_debt_id': debt.pk, 'requested_minor': amount, 'forfeited_minor': forfeited,
            'debt_minor': remaining, 'taken_now_minor': taken, 'uncollected_minor': uncollected}


def _own_pending(flag, request_row):
    """The request being executed counts as pending in `recoverable_minor`; add it back."""
    from servers.approvals.models import REQUESTED

    if request_row.status == REQUESTED:
        return int((request_row.payload or {}).get('amount_minor') or 0)
    return 0


# --- collecting a debt from a payout ---------------------------------------

def open_debts(driver):
    return list(RecoveryDebt.objects.select_for_update().filter(
        user_id=driver.user_id_id, role='driver', closed_at__isnull=True).order_by('id'))


def plan_deduction(driver, payout_minor, floor_minor, percent):
    """`(total, [(debt, amount)])`: at most `percent` of `payout_minor`, never more than owed, and
    never so much that the payout falls below `floor_minor` (less taken: the debt carries over)."""
    debts = [d for d in open_debts(driver) if d.remaining_minor > 0]
    if not debts or payout_minor <= 0:
        return 0, []
    cap = payout_minor * int(percent) // 100
    cap = min(cap, max(0, payout_minor - max(0, floor_minor)))
    plan, left = [], cap
    for debt in debts:
        take = min(debt.remaining_minor, left)
        if take > 0:
            plan.append((debt, take))
            left -= take
    return sum(a for _d, a in plan), plan


def apply_deduction(driver, plan, withdrawal, payout_gross_minor, percent):
    """Write each deduction as its own ledger line and its own payout line. The CALLER lowers the
    wallet balance (the recovery is part of what leaves the wallet in this payout)."""
    from servers.rider.models import WalletTransaction

    for debt, amount in plan:
        txn = WalletTransaction.objects.create(
            user_id=driver.user_id, currency=debt.currency, amount_minor=amount,
            txn_type='debit', status='completed', purpose=RECOVERY_PURPOSE,
            reference_id=f'RECOVERY_{debt.approval_request_id}',
            idempotency_key=f'recovery_{debt.pk}_w{withdrawal.pk}')
        RecoveryDeduction.objects.create(
            debt=debt, withdrawal_id=withdrawal.pk, wallet_transaction=txn, amount_minor=amount,
            payout_gross_minor=payout_gross_minor, percent_cap=int(percent))
        debt.recovered_minor += amount
        if debt.remaining_minor <= 0:
            debt.closed_at = timezone.now()
        debt.save(update_fields=['recovered_minor', 'closed_at'])
        record_admin_action(
            None, action='fraud_recovery_deducted', target_type='risk_flag',
            target_id=debt.flag_id,
            after={'recovery_debt_id': debt.pk, 'withdrawal_id': withdrawal.pk,
                   'amount_minor': amount, 'payout_gross_minor': payout_gross_minor,
                   'cap_percent': int(percent), 'remaining_minor': debt.remaining_minor},
            reason='recovery deducted from a payout')


def deduction_settings(driver):
    from servers.payments.payout_config import driver_payout_country

    return settings_for(driver_payout_country(driver))


# ---------------------------------------------------------------------------
# Ruling d: payout hold (suspension + second admin + a maximum length)
# ---------------------------------------------------------------------------

def active_payout_hold(driver, now=None):
    now = now or timezone.now()
    return PayoutHold.objects.filter(driver=driver, status=PayoutHold.ACTIVE,
                                     expires_at__gt=now).first()


def _suspended(driver):
    from servers.driver.models import DriverSuspension

    return DriverSuspension.objects.filter(driver=driver, lifted_at__isnull=True).exists()


def payout_hold_blockers(flag):
    """Why a payout hold cannot be requested for this flag now (empty = allowed)."""
    out = []
    if flag.status != CONFIRMED:
        out.append('flag_not_confirmed')
    elif flag.decision_reason not in SERIOUS_REASONS:
        out.append('reason_not_serious')
    driver = _driver_of(flag.subject)
    if driver is None:
        out.append('subject_not_driver')
    elif not _suspended(driver):
        out.append('driver_not_suspended')
    return out


def payout_hold_state(flag):
    driver = _driver_of(flag.subject)
    holds = PayoutHold.objects.filter(flag=flag).order_by('-id')
    cfg = settings_for(country_of_user(flag.subject))
    return {
        'blockers': payout_hold_blockers(flag), 'max_days': cfg['payout_hold_max_days'],
        'serious_reasons': list(SERIOUS_REASONS),
        'active': driver is not None and active_payout_hold(driver) is not None,
        'holds': [hold_dict(h) for h in holds[:5]],
    }


def hold_dict(h):
    return {'id': h.pk, 'status': h.status, 'starts_at': h.starts_at.isoformat(),
            'expires_at': h.expires_at.isoformat(), 'max_days': h.max_days,
            'renewal_of': h.renewal_of_id, 'note': h.note,
            'released_at': h.released_at.isoformat() if h.released_at else None,
            'release_note': h.release_note, 'approval_request_id': h.approval_request_id}


def request_payout_hold(actor, flag, note, days, key, renew=False, http_request=None):
    """Raise the `finance.payout_hold` request (always a checker, India and US)."""
    from servers.admin_access.bridge import granted
    from servers.approvals import service as approvals

    if not granted(actor, 'fraud.review', flag.anchor_node)[0]:
        raise MoneyError('PERMISSION_DENIED', 'You do not hold fraud.review here.', 403,
                         'permission')
    blockers = payout_hold_blockers(flag)
    if blockers:
        raise MoneyError(
            'PAYOUT_HOLD_NOT_ALLOWED',
            'A payout hold needs a confirmed serious-fraud flag on a driver who is suspended '
            f'({", ".join(blockers)}).', 409, 'status')
    driver = _driver_of(flag.subject)
    previous = PayoutHold.objects.filter(driver=driver, flag=flag).order_by('-id').first()
    active = active_payout_hold(driver)
    if renew:
        if previous is None:
            raise MoneyError('NOTHING_TO_RENEW', 'There is no earlier hold to renew.', 409,
                             'renew')
    elif active is not None:
        raise MoneyError('ALREADY_HELD', 'This driver already has an active payout hold.', 409,
                         'status')
    elif previous is not None:
        raise MoneyError('USE_RENEWAL', 'A hold already existed for this flag; renewing it is '
                         'another checker request.', 409, 'renew')
    note = (note or '').strip()
    if len(note) < 5:
        raise MoneyError('INVALID_NOTE', 'A note of at least 5 characters is required.', 400,
                         'note')
    from servers.payments.payout_config import driver_payout_country

    country = driver_payout_country(driver)
    cfg = settings_for(country)
    max_days = cfg['payout_hold_max_days']
    days = max_days if days in (None, '') else days
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= max_days:
        raise MoneyError('INVALID_DAYS', f'days must be between 1 and {max_days}.', 400, 'days')
    outcome = approvals.require_approval_or_execute(
        FINANCE_PAYOUT_HOLD, actor=actor, country=country, scope_node=driver.home_city,
        target_type='payout_hold', target_id=driver.pk,
        payload={'flag_id': flag.pk, 'driver_id': driver.pk, 'days': days, 'renew': bool(renew),
                 'note': note[:500]},
        idempotency_key=f'payout_hold:{flag.pk}:{key}'[:200],
        reason=f'Payout hold ({"renewal, " if renew else ""}{days} days): {note}'[:2000],
        http_request=http_request)
    return outcome


@registry.register(FINANCE_PAYOUT_HOLD)
def execute_payout_hold(request_row):
    from servers.driver.models import Driver

    p = request_row.payload or {}
    flag = RiskFlag.objects.select_related('subject').filter(pk=p.get('flag_id')).first()
    driver = Driver.objects.filter(pk=p.get('driver_id')).first()
    if flag is None or driver is None:
        raise registry.ExecutionRefused('BAD_PAYOUT_HOLD', 'The request is not valid.')
    existing = PayoutHold.objects.filter(approval_request=request_row).first()
    if existing is not None:
        return {'payout_hold_id': existing.pk, 'duplicate': True}
    blockers = payout_hold_blockers(flag)
    if blockers:
        raise registry.ExecutionRefused(
            'PAYOUT_HOLD_NOT_ALLOWED',
            f'The conditions no longer hold ({", ".join(blockers)}).')
    from servers.payments.payout_config import driver_payout_country

    max_days = settings_for(driver_payout_country(driver))['payout_hold_max_days']
    days = min(int(p.get('days') or max_days), max_days)
    now = timezone.now()
    with transaction.atomic():
        previous = (PayoutHold.objects.select_for_update().filter(driver=driver, flag=flag)
                    .order_by('-id').first())
        if previous is not None and previous.status == PayoutHold.ACTIVE:
            previous.status, previous.released_at = PayoutHold.RELEASED, now
            previous.release_note = 'renewed'
            previous.save(update_fields=['status', 'released_at', 'release_note'])
        hold = PayoutHold.objects.create(
            driver=driver, flag=flag, approval_request=request_row, starts_at=now,
            expires_at=now + timedelta(days=days), max_days=max_days,
            renewal_of=previous if p.get('renew') else None, note=(p.get('note') or '')[:500])
        record_admin_action(
            None, action='payout_hold_placed', target_type='driver', target_id=driver.pk,
            after={'payout_hold_id': hold.pk, 'flag_id': flag.pk, 'days': days,
                   'expires_at': hold.expires_at.isoformat(), 'renewal': bool(p.get('renew')),
                   'approval_request_id': request_row.pk},
            reason=request_row.request_reason)
        _note(flag, f'Payout hold approved for {days} days (until {hold.expires_at:%Y-%m-%d}).',
              data={'payout_hold_id': hold.pk}, reason='payout_hold')
    return {'payout_hold_id': hold.pk, 'expires_at': hold.expires_at.isoformat(), 'days': days}


def release_payout_hold(actor, hold, note, http_request=None):
    """Release early. No checker: it only lets the driver be paid."""
    note = (note or '').strip()
    if len(note) < 5:
        raise MoneyError('INVALID_NOTE', 'A note of at least 5 characters is required.', 400,
                         'note')
    with transaction.atomic():
        hold = PayoutHold.objects.select_for_update().get(pk=hold.pk)
        if hold.status != PayoutHold.ACTIVE:
            raise MoneyError('NOT_ACTIVE', 'This hold is no longer active.', 409, 'status')
        hold.status, hold.released_at = PayoutHold.RELEASED, timezone.now()
        hold.released_by_id, hold.release_note = actor.pk, note[:500]
        hold.save(update_fields=['status', 'released_at', 'released_by_id', 'release_note'])
        record_admin_action(
            http_request, action='payout_hold_released', target_type='driver',
            target_id=hold.driver_id,
            before={'payout_hold_id': hold.pk, 'status': 'active'},
            after={'payout_hold_id': hold.pk, 'status': 'released'}, reason=note)
        _note(hold.flag, 'Payout hold released early.', actor,
              data={'payout_hold_id': hold.pk}, reason='payout_hold_release')
    return hold


def lapse_due_holds(now=None):
    """The beat sweep: a hold past `expires_at` lapses (the driver is paid again) and Finance and
    the flag's reviewers get a Command Centre alert to renew or leave it released."""
    from servers.command_centre.models import FRAUD_PAYOUT_HOLD_EXPIRED, WARNING
    from servers.command_centre.services import raise_alert

    now = now or timezone.now()
    count = 0
    with audit_context(actor=None, actor_label=SYSTEM_LABEL, reason='payout hold ran out'):
        for pk in list(PayoutHold.objects.filter(status=PayoutHold.ACTIVE, expires_at__lte=now)
                       .values_list('pk', flat=True)):
            with transaction.atomic():
                hold = (PayoutHold.objects.select_for_update().select_related('flag', 'driver')
                        .filter(pk=pk, status=PayoutHold.ACTIVE).first())
                if hold is None:
                    continue
                hold.status, hold.released_at = PayoutHold.LAPSED, now
                hold.release_note = 'lapsed at its maximum duration'
                hold.expiry_alerted_at = now
                hold.save(update_fields=['status', 'released_at', 'release_note',
                                         'expiry_alerted_at'])
                city = hold.flag.anchor_node
                raise_alert(
                    FRAUD_PAYOUT_HOLD_EXPIRED, severity=WARNING,
                    title=('A payout hold ended' + (f' in {city.name}' if city else '')
                           + ': renew it or leave the payout released')[:200],
                    payload={'payout_hold_id': hold.pk, 'flag_id': hold.flag_id,
                             'path': f'/fraud/{hold.flag_id}'},
                    source_type='fraud_payout_hold_expired', source_id=str(hold.pk), city=city)
                record_admin_action(
                    None, action='payout_hold_lapsed', target_type='driver',
                    target_id=hold.driver_id,
                    before={'payout_hold_id': hold.pk, 'status': 'active'},
                    after={'payout_hold_id': hold.pk, 'status': 'lapsed'},
                    reason='maximum duration reached; released unless renewed')
                _note(hold.flag, 'Payout hold ended at its maximum duration; the driver is '
                                 'paid again unless it is renewed.',
                      data={'payout_hold_id': hold.pk}, reason='payout_hold_lapsed')
                count += 1
    return count


def run_sweeps(now=None):
    return {'rewards_released': release_due(now), 'payout_holds_lapsed': lapse_due_holds(now)}


# ---------------------------------------------------------------------------
# Ruling c: online payment only
# ---------------------------------------------------------------------------

def restrict_cash(actor, flag, note, http_request=None):
    """Set `cash_disabled` on the rider of a CONFIRMED payment-abuse flag (`fraud.review`)."""
    from servers.admin_access.bridge import granted
    from servers.rider.models import Rider, RiderRestriction

    if flag.status != CONFIRMED or flag.decision_reason != 'payment_abuse':
        raise MoneyError('RESTRICTION_NOT_ALLOWED',
                         'Online-only is set from a flag confirmed as payment abuse.', 409, 'status')
    if getattr(flag.subject, 'role', '') != 'rider':
        raise MoneyError('RESTRICTION_NOT_APPLICABLE', 'Only a rider can be set to online-only.',
                         400, 'kind')
    if not granted(actor, 'fraud.review', flag.anchor_node)[0]:
        raise MoneyError('PERMISSION_DENIED', 'You do not hold fraud.review here.', 403,
                         'permission')
    note = (note or '').strip()
    if len(note) < 5:
        raise MoneyError('INVALID_NOTE', 'A note of at least 5 characters is required.', 400,
                         'note')
    rider = Rider.objects.filter(user_id=flag.subject).first()
    if rider is None:
        raise MoneyError('RESTRICTION_NOT_APPLICABLE', 'No rider profile.', 400, 'kind')
    with transaction.atomic():
        active = RiderRestriction.objects.select_for_update().filter(
            rider=rider, kind=RiderRestriction.CASH_DISABLED, cleared_at__isnull=True).first()
        if active is not None:
            return active, False
        row = RiderRestriction.objects.create(
            rider=rider, kind=RiderRestriction.CASH_DISABLED, reason=note[:2000], set_by=actor,
            flag_id=flag.pk)
        record_admin_action(
            http_request, action='rider_cash_disabled', target_type='rider',
            target_id=rider.user_id_id, before={'cash_disabled': False},
            after={'cash_disabled': True, 'restriction_id': row.pk, 'flag_id': flag.pk},
            reason=note)
        _note(flag, 'Rider set to online payment only.', actor,
              data={'restriction_id': row.pk}, reason='cash_disabled')
    return row, True


# ---------------------------------------------------------------------------
# What the driver's app and Finance's ledger page add
# ---------------------------------------------------------------------------

def driver_money_lines(driver):
    """Additive keys for the driver's earnings: a reward pending review and a recovery, neutral."""
    from base.money import from_minor
    from servers.rider.models import WalletTransaction

    user = driver.user_id
    wallet = _wallet(user)
    held = held_minor(user, wallet.currency)
    recovered = (WalletTransaction.objects.filter(
        user_id=user, purpose=RECOVERY_PURPOSE, status='completed', currency=wallet.currency)
        .aggregate(t=Sum('amount_minor'))['t'] or 0)
    owed = sum(d.remaining_minor for d in RecoveryDebt.objects.filter(
        user=user, role='driver', closed_at__isnull=True))
    return {
        'rewards_pending_review': str(from_minor(held, wallet.currency)),
        'rewards_pending_review_label': PENDING_REVIEW_LABEL,
        'reward_recovery': str(from_minor(recovered, wallet.currency)),
        'reward_recovery_label': RECOVERY_LABEL,
        'reward_recovery_remaining': str(from_minor(owed, wallet.currency)),
    }


def ledger_extras(driver):
    """For Finance's driver ledger page: held rewards, recovery debts with their carry-over, and
    any payout hold. Admin-facing; ids only, nothing that identifies the person beyond the page."""
    user = driver.user_id
    wallet = _wallet(user)
    debts = []
    for d in RecoveryDebt.objects.filter(user=user, role='driver').order_by('-id')[:20]:
        debts.append({
            'id': d.pk, 'flag_id': d.flag_id, 'requested_minor': d.requested_minor,
            'forfeited_minor': d.forfeited_minor, 'amount_minor': d.amount_minor,
            'recovered_minor': d.recovered_minor, 'remaining_minor': d.remaining_minor,
            'currency': d.currency, 'created_at': d.created_at.isoformat(),
            'closed_at': d.closed_at.isoformat() if d.closed_at else None,
            'approval_request_id': d.approval_request_id,
            'deductions': [{'withdrawal_id': x.withdrawal_id, 'amount_minor': x.amount_minor,
                            'payout_gross_minor': x.payout_gross_minor,
                            'percent_cap': x.percent_cap, 'at': x.created_at.isoformat()}
                           for x in d.deductions.all()]})
    hold = active_payout_hold(driver)
    held = held_minor(user, wallet.currency)
    cfg = settings_for(country_of_user(user))
    return {
        'held_rewards': {
            'minor': held, 'currency': wallet.currency,
            'holds': [{'id': h.pk, 'amount_minor': h.amount_minor, 'kind': h.kind,
                       'flag_id': h.flag_id, 'release_at': h.release_at.isoformat()}
                      for h in RewardHold.objects.filter(user=user, status=RewardHold.HELD)[:20]]},
        'recovery': {'debts': debts, 'max_percent_per_payout': cfg['recovery_max_percent'],
                     'remaining_minor': sum(x['remaining_minor'] for x in debts)},
        'payout_hold': hold_dict(hold) | {'flag_id': hold.flag_id} if hold else None,
    }
