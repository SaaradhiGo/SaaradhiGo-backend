"""Reviewing a flag: assign, note, decide, reopen, and record a hand-off.

EVERY WRITE
-----------
takes the flag's row lock, checks the transition, writes one `FlagEvent` (the timeline) and one
audit row (`record_admin_action`), in one transaction. Two reviewers deciding at once: the second
finds the flag already decided and gets `409 ALREADY_DECIDED`.

WHAT CONFIRMING DOES, AND DOES NOT
----------------------------------
Confirming records the decision, and for a rider flag raised by a promotion rule keeps the Riders
page in step (`RiderFlag('promo_abuse')` set or cleared with the same note). It does not suspend,
claw back, adjust, hold a payout or notify. The only money it touches is a REWARD HOLD (owner
ruling a, AUDIT 27.56): a dismissal releases it, a confirmation keeps it for Finance's recovery
only for promo abuse, collusion and multiple accounts (see `money.on_decision`). The hand-offs below
are LINKS to actions that already exist in other modules, each behind its own capability, scope and
maker-checker; taking one only records that the reviewer chose it.
"""

from django.db import transaction
from django.utils import timezone

from servers.admin_audit.services import record_admin_action

from .models import (
    ACTIVE_STATUSES, CONFIRM_REASONS, CONFIRMED, DECIDED_STATUSES, DISMISS_REASONS, DISMISSED,
    EV_ASSIGNED, EV_CONFIRMED, EV_DISMISSED, EV_HANDOFF, EV_NOTE, EV_REOPENED, EV_UNASSIGNED,
    IN_REVIEW, NOTE_MAX, NOTE_MIN, OPEN, REOPEN_REASONS, FlagEvent, RiskFlag,
)

TARGET = 'risk_flag'

#: Flag kinds whose rider subject has a `RiderFlag('promo_abuse')` on the Riders page.
PROMO_KINDS = ('referral_abuse', 'multi_accounting')

#: kind -> (label, the capability the OTHER module asks, the subject's role, console path).
#: A link, never an action. `{id}` is the rider's user id or the driver's id.
HANDOFFS = {
    'driver_suspend': ('Suspend the driver', 'driver.write', 'driver', '/drivers/{id}'),
    'driver_deactivate': ('Request driver deactivation', 'driver.deactivate', 'driver',
                          '/drivers/{id}'),
    'rider_block': ('Suspend the rider', 'rider.block', 'rider', '/riders/{id}'),
    'rider_flag': ('Flag the rider', 'rider.flag', 'rider', '/riders/{id}'),
    'finance_adjustment': ('Request a Finance adjustment', 'finance.adjust', 'either',
                           '/finance'),     # a driver's ledger, or the rider's page; see below
}


class ReviewError(Exception):
    def __init__(self, code, message, status=400, field='general'):
        super().__init__(message)
        self.code, self.message, self.status, self.field = code, message, status, field


def clean_note(note, *, required=True):
    note = (note or '').strip()
    if not note and not required:
        return ''
    if len(note) < NOTE_MIN or len(note) > NOTE_MAX:
        raise ReviewError('INVALID_NOTE', f'A note of {NOTE_MIN} to {NOTE_MAX} characters is '
                          'required.', 400, 'note')
    return note


def _lock(flag_id):
    flag = RiskFlag.objects.select_for_update().filter(pk=flag_id).first()
    if flag is None:
        raise ReviewError('NOT_FOUND', 'Flag not found.', 404, 'flag_id')
    return flag


def _event(flag, action, actor, *, from_status='', to_status='', reason='', note='', data=None):
    from servers.admin_audit.models import actor_snapshot

    snap = actor_snapshot(actor) if actor is not None else {}
    return FlagEvent.objects.create(
        flag=flag, action=action, from_status=from_status, to_status=to_status, reason=reason,
        note=note, actor_id=snap.get('actor_id'), actor_label=(snap.get('actor_label') or '')[:255],
        data=data or {})


def _audit(request, action, flag, before, after, reason=''):
    record_admin_action(request, action=action, target_type=TARGET, target_id=flag.pk,
                        before=before, after=after, reason=reason)


def _state(flag):
    return {'status': flag.status, 'assigned_to': flag.assigned_to_id,
            'decision_reason': flag.decision_reason, 'kind': flag.kind,
            'subject_id': flag.subject_id}


# ---------------------------------------------------------------------------
# Assignment and notes
# ---------------------------------------------------------------------------

def assignee_may_review(assignee, flag):
    """Does `assignee` hold fraud.review at the flag's place, and see the flag?"""
    from servers.admin_access.bridge import granted
    from servers.admin_access.scoping import scoped

    if not assignee.is_active or not granted(assignee, 'fraud.review', flag.anchor_node)[0]:
        return False
    return scoped(assignee, RiskFlag.objects.filter(pk=flag.pk)).exists()


def assign(flag_id, actor, assignee, request=None):
    from django.contrib.auth import get_user_model

    with transaction.atomic():
        flag = _lock(flag_id)
        if flag.status not in ACTIVE_STATUSES:
            raise ReviewError('ALREADY_DECIDED', 'This flag is already decided; reopen it first.',
                              409, 'status')
        if not isinstance(assignee, get_user_model()):
            raise ReviewError('INVALID_ASSIGNEE', 'No such reviewer.', 400, 'assignee_id')
        if not assignee_may_review(assignee, flag):
            raise ReviewError('ASSIGNEE_NOT_ELIGIBLE',
                              'That person cannot review flags in this place.', 400, 'assignee_id')
        before, was = _state(flag), flag.status
        if flag.assigned_to_id == assignee.pk and flag.status == IN_REVIEW:
            return flag
        flag.assigned_to, flag.assigned_at, flag.status = assignee, timezone.now(), IN_REVIEW
        flag.save(update_fields=['assigned_to', 'assigned_at', 'status', 'updated_at'])
        _event(flag, EV_ASSIGNED, actor, from_status=was, to_status=flag.status,
               data={'assignee_id': assignee.pk})
        _audit(request, 'fraud_flag_assigned', flag, before, _state(flag))
    return flag


def unassign(flag_id, actor, request=None):
    with transaction.atomic():
        flag = _lock(flag_id)
        if flag.status not in ACTIVE_STATUSES:
            raise ReviewError('ALREADY_DECIDED', 'This flag is already decided; reopen it first.',
                              409, 'status')
        if flag.assigned_to_id is None:
            return flag
        before, was = _state(flag), flag.status
        previous = flag.assigned_to_id
        flag.assigned_to, flag.assigned_at, flag.status = None, None, OPEN
        flag.save(update_fields=['assigned_to', 'assigned_at', 'status', 'updated_at'])
        _event(flag, EV_UNASSIGNED, actor, from_status=was, to_status=OPEN,
               data={'previous_assignee_id': previous})
        _audit(request, 'fraud_flag_unassigned', flag, before, _state(flag))
    return flag


def add_note(flag_id, actor, note, request=None):
    note = clean_note(note)
    with transaction.atomic():
        flag = _lock(flag_id)
        _event(flag, EV_NOTE, actor, from_status=flag.status, to_status=flag.status, note=note)
        _audit(request, 'fraud_flag_noted', flag, _state(flag), _state(flag), reason=note)
    return flag


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

def decide(flag_id, actor, decision, reason_code, note, request=None):
    """Confirm or dismiss. `409 ALREADY_DECIDED` if someone else got there first."""
    if decision not in (CONFIRMED, DISMISSED):
        raise ReviewError('INVALID_DECISION', 'Decision must be confirmed or dismissed.', 400,
                          'decision')
    allowed = CONFIRM_REASONS if decision == CONFIRMED else DISMISS_REASONS
    if reason_code not in allowed:
        raise ReviewError('INVALID_REASON', f'reason_code must be one of: {", ".join(allowed)}.',
                          400, 'reason_code')
    note = clean_note(note)
    with transaction.atomic():
        flag = _lock(flag_id)
        if flag.status in DECIDED_STATUSES:
            raise ReviewError('ALREADY_DECIDED',
                              f'This flag was already {flag.status}. Reopen it to decide again.',
                              409, 'status')
        before, was = _state(flag), flag.status
        flag.status, flag.decided_by, flag.decided_at = decision, actor, timezone.now()
        flag.decision_reason, flag.decision_note = reason_code, note
        flag.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_reason',
                                 'decision_note', 'updated_at'])
        _event(flag, EV_CONFIRMED if decision == CONFIRMED else EV_DISMISSED, actor,
               from_status=was, to_status=decision, reason=reason_code, note=note)
        synced = sync_rider_flag(flag, actor, decision, note)
        # Ruling (a): a dismissal releases the held reward at once; a confirmation keeps it for
        # Finance only under a reason that recovers rewards, else releases it. Nothing is
        # forfeited, held back from trip earnings, or paid here.
        from . import money

        rewards = money.on_decision(flag, decision)
        _audit(request, f'fraud_flag_{decision}', flag, before,
               {**_state(flag), 'rider_flag': synced, 'reward_holds': rewards}, reason=note)
    return flag


def reopen(flag_id, actor, reason_code, note, request=None):
    if reason_code not in REOPEN_REASONS:
        raise ReviewError('INVALID_REASON',
                          f'reason_code must be one of: {", ".join(REOPEN_REASONS)}.', 400,
                          'reason_code')
    note = clean_note(note)
    with transaction.atomic():
        flag = _lock(flag_id)
        if flag.status not in DECIDED_STATUSES:
            raise ReviewError('NOT_DECIDED', 'Only a confirmed or dismissed flag can be reopened.',
                              409, 'status')
        if RiskFlag.objects.filter(dedupe_key=flag.dedupe_key, status__in=ACTIVE_STATUSES).exclude(
                pk=flag.pk).exists() and flag.dedupe_key:
            raise ReviewError('ACTIVE_FLAG_EXISTS',
                              'A newer flag for this person and rule is already open.', 409,
                              'status')
        before, was = _state(flag), flag.status
        flag.status, flag.decided_by, flag.decided_at = OPEN, None, None
        flag.decision_reason = flag.decision_note = ''
        flag.assigned_to, flag.assigned_at = None, None
        flag.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_reason',
                                 'decision_note', 'assigned_to', 'assigned_at', 'updated_at'])
        _event(flag, EV_REOPENED, actor, from_status=was, to_status=OPEN, reason=reason_code,
               note=note)
        _audit(request, 'fraud_flag_reopened', flag, before, _state(flag), reason=note)
        from .raising import hold_rewards

        hold_rewards(flag)
    return flag


def sync_rider_flag(flag, actor, decision, note):
    """Keep `RiderFlag('promo_abuse')` in step with the decision. Returns what was done.

    Confirmed: make sure the rider carries an active promo_abuse flag with the reviewer's note.
    Dismissed: clear it, unless another promotion flag on the same person is still active or
    confirmed. Only for a rider subject of a promotion rule; the Riders page agrees with the queue.
    """
    from servers.rider.models import Rider, RiderFlag

    if flag.kind not in PROMO_KINDS or flag.subject.role != 'rider':
        return None
    rider = Rider.objects.filter(user_id=flag.subject).first()
    if rider is None:
        return None
    active = RiderFlag.objects.select_for_update().filter(
        rider=rider, reason='promo_abuse', cleared_at__isnull=True).first()
    if decision == CONFIRMED:
        if active is None:
            RiderFlag.objects.create(rider=rider, reason='promo_abuse', note=note,
                                     created_by=actor)
            return 'set'
        return 'already_set'
    others = RiskFlag.objects.filter(subject=flag.subject, kind__in=PROMO_KINDS,
                                     status__in=[*ACTIVE_STATUSES, CONFIRMED]).exclude(pk=flag.pk)
    if active is None or others.exists():
        return 'kept' if active is not None else 'none'
    active.cleared_at, active.cleared_by, active.clear_note = timezone.now(), actor, note
    active.save(update_fields=['cleared_at', 'cleared_by', 'clear_note'])
    return 'cleared'


# ---------------------------------------------------------------------------
# Hand-offs (a record of the reviewer's choice, never an action)
# ---------------------------------------------------------------------------

def handoff_target(flag, kind):
    """The console path for `kind` on this flag's subject, or None if it does not fit."""
    label, _cap, role, path = HANDOFFS[kind]
    subject_role = flag.subject.role
    if role != 'either' and subject_role != role:
        return None
    if role == 'driver':
        driver = getattr(flag.subject, 'driver', None)
        return path.format(id=driver.pk) if driver is not None else None
    if role == 'rider':
        return path.format(id=flag.subject_id)
    # The Finance adjustment: a driver's balance is adjusted from their ledger, a rider's wallet
    # credit from the rider's page. Both are maker-checker requests in their own module.
    if subject_role == 'driver':
        driver = getattr(flag.subject, 'driver', None)
        return f'/finance/drivers/{driver.pk}/ledger' if driver is not None else None
    if subject_role == 'rider':
        return f'/riders/{flag.subject_id}'
    return None


def record_handoff(flag_id, actor, kind, request=None):
    from servers.admin_access.bridge import granted

    if kind not in HANDOFFS:
        raise ReviewError('INVALID_HANDOFF', f'kind must be one of: {", ".join(HANDOFFS)}.', 400,
                          'kind')
    with transaction.atomic():
        flag = _lock(flag_id)
        if flag.status != CONFIRMED:
            raise ReviewError('NOT_CONFIRMED', 'Hand-offs are taken from a confirmed flag.', 409,
                              'status')
        if handoff_target(flag, kind) is None:
            raise ReviewError('HANDOFF_NOT_APPLICABLE', 'That hand-off does not fit this person.',
                              400, 'kind')
        if not granted(actor, HANDOFFS[kind][1], flag.anchor_node)[0]:
            raise ReviewError('HANDOFF_NOT_PERMITTED',
                              f'You do not hold {HANDOFFS[kind][1]} here.', 403, 'kind')
        before = _state(flag)
        taken = list(flag.handoffs or [])
        taken.append({'kind': kind, 'at': timezone.now().isoformat(), 'by': actor.pk})
        flag.handoffs = taken
        flag.save(update_fields=['handoffs', 'updated_at'])
        _event(flag, EV_HANDOFF, actor, from_status=flag.status, to_status=flag.status,
               reason=kind)
        _audit(request, 'fraud_flag_handoff', flag, before, {**_state(flag), 'handoff': kind})
    return flag
