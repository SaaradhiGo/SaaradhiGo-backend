"""Driver fee plans through the `pricing.change` maker-checker (9 Nov).

draft (direct, audited) -> submit (a request a checker decides) -> the executor activates it from
the NEXT local day of its market (never mid-day): the scope's previous active plan is archived with
`effective_to` that day, so every trip keeps the plan it was booked under. The executor refuses a
plan that takes more than the legal share of a normal trip.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from servers.admin_access.models import GeoNode
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import PRICING_CHANGE
from servers.approvals.service import InvalidRequest, NotPermitted, require_approval_or_execute

from . import fee_plans
from .fee_plan_models import (
    ACTIVE, ARCHIVED, DRAFT, PENDING_APPROVAL, PLAN_TYPES, DriverFeePlan,
)

KIND = 'driver_fee_plan'
AMOUNT_KEYS = ('per_trip_fee_minor', 'daily_pass_minor', 'fee_free_after_trips', 'commission_percent',
               'rider_platform_fee_minor')


def plan_dict(p):
    return {'id': p.id, 'country': p.country, 'scope_code': p.scope_code, 'currency': p.currency,
            'plan_type': p.plan_type, 'amounts': p.amounts, 'earnings_floor_minor': p.earnings_floor_minor,
            'daily_cap_minor': p.daily_cap_minor,
            'waiver_until': p.waiver_until.isoformat() if p.waiver_until else None,
            'waiver_first_days': p.waiver_first_days, 'status': p.status, 'version': p.version,
            'effective_from': p.effective_from.isoformat() if p.effective_from else None,
            'effective_to': p.effective_to.isoformat() if p.effective_to else None,
            'note': p.note, 'reason': p.reason, 'approval_request_id': p.approval_request_id,
            'created_by': p.created_by_label,
            'created_at': p.created_at.isoformat() if p.created_at else None,
            'activated_at': p.activated_at.isoformat() if p.activated_at else None,
            # 22 Nov review: the place by name ("Treasure Valley", not "US-ID-TV").
            'scope_name': _scope_name(p)}


def _scope_name(p):
    node = _node(p.country, p.scope_code)
    return node.name if node is not None else (p.scope_code or p.country)


def _node(country, scope_code):
    return (GeoNode.objects.filter(code=scope_code).first() if scope_code
            else GeoNode.objects.filter(kind='country', code=country).first())


def _clean(data):
    plan_type = data.get('plan_type')
    if plan_type not in {k for k, _ in PLAN_TYPES}:
        raise InvalidRequest('Unknown plan type.', code='INVALID_PLAN_TYPE', field='plan_type')
    amounts = {}
    for cls, vals in (data.get('amounts') or {}).items():
        if not isinstance(vals, dict):
            raise InvalidRequest('amounts is {class: {...}}.', code='INVALID_AMOUNTS', field='amounts')
        row = {}
        for k, v in vals.items():
            if k not in AMOUNT_KEYS:
                raise InvalidRequest(f'Unknown amount {k}.', code='INVALID_AMOUNTS', field='amounts')
            if v in (None, ''):
                continue
            try:
                row[k] = float(v) if k == 'commission_percent' else int(v)
            except (TypeError, ValueError):
                raise InvalidRequest(f'{k} must be a number.', code='INVALID_AMOUNTS', field='amounts')
            if row[k] < 0:
                raise InvalidRequest(f'{k} cannot be negative.', code='INVALID_AMOUNTS', field='amounts')
        amounts[str(cls).lower()] = row
    out = {'plan_type': plan_type, 'amounts': amounts,
           'earnings_floor_minor': int(data.get('earnings_floor_minor') or 0),
           'daily_cap_minor': int(data['daily_cap_minor']) if data.get('daily_cap_minor') not in (None, '') else None,
           'waiver_first_days': int(data['waiver_first_days']) if data.get('waiver_first_days') not in (None, '') else None,
           'note': str(data.get('note') or '')[:255]}
    if data.get('waiver_until'):
        from datetime import date
        try:
            out['waiver_until'] = date.fromisoformat(str(data['waiver_until']))
        except ValueError:
            raise InvalidRequest('waiver_until is YYYY-MM-DD.', code='INVALID_DATE', field='waiver_until')
    return out


def create_draft(actor, data, *, label=''):
    from servers.admin_access.bridge import granted

    country = str(data.get('country') or '').upper()
    scope = str(data.get('scope_code') or '').upper()
    node = _node(country, scope)
    if node is None:
        raise InvalidRequest('No such country or place.', code='UNKNOWN_SCOPE', field='scope_code')
    if not granted(actor, 'pricing.write', node)[0]:
        raise NotPermitted('You do not hold pricing.write here.', code='SCOPE_NOT_HELD', field='scope_code')
    values = _clean(data)
    last = (DriverFeePlan.objects.filter(country=country, scope_code=scope).order_by('-version')
            .values_list('version', flat=True).first() or 0)
    with transaction.atomic():
        plan = DriverFeePlan.objects.create(country=country, scope_code=scope, version=last + 1,
                                            status=DRAFT, reason=str(data.get('reason') or ''),
                                            created_by_label=label, **values)
        record_admin_action(None, action='driver_fee_plan_drafted', target_type=KIND,
                            target_id=str(plan.id), after=plan_dict(plan), reason=plan.reason)
    return plan


def update_draft(actor, plan, data):
    if plan.status != DRAFT:
        raise InvalidRequest('Only a draft can be edited.', code='NOT_A_DRAFT', field='status')
    before = plan_dict(plan)
    for k, v in _clean({**plan_dict(plan), **data}).items():
        setattr(plan, k, v)
    plan.save()
    record_admin_action(None, action='driver_fee_plan_edited', target_type=KIND, target_id=str(plan.id),
                        before=before, after=plan_dict(plan), reason=str(data.get('reason') or ''))
    return plan


def submit(actor, plan, *, reason='', http_request=None, requested_from=None, display=None):
    if plan.status != DRAFT:
        raise InvalidRequest('Only a draft can be submitted.', code='NOT_A_DRAFT', field='status')
    if not reason.strip():
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    bad = fee_plans.breaks_legal_share_on_normal_trip(plan)
    if bad:
        raise InvalidRequest(f'This plan takes more than the legal share of a normal trip for: {", ".join(bad)}.',
                             code='BREAKS_LEGAL_SHARE', field='amounts')
    if plan.plan_type == fee_plans.PERCENT_COMMISSION:
        cap = fee_plans.legal_cap_percent(plan.country)
        over = [c for c, a in (plan.amounts or {}).items()
                if a.get('commission_percent') is not None and fee_plans.Decimal(str(a['commission_percent'])) > cap]
        if over:
            raise InvalidRequest(f'A commission cannot be above {cap}% ({", ".join(over)}).',
                                 code='ABOVE_COMMISSION_CAP', field='amounts')
    outcome = require_approval_or_execute(
        PRICING_CHANGE, actor=actor, country=plan.country, scope_node=_node(plan.country, plan.scope_code),
        target_type=KIND, target_id=str(plan.id),
        payload={'kind': KIND, 'plan_id': plan.id,
                 **({'requested_from': requested_from} if requested_from else {}),
                 **({'display': display} if display else {})},
        idempotency_key=f'{KIND}:{plan.id}', reason=reason,
        http_request=http_request)
    plan.refresh_from_db()
    if plan.status == DRAFT:
        plan.status = PENDING_APPROVAL
        plan.approval_request_id = outcome.request.id
        plan.save(update_fields=['status', 'approval_request_id'])
    return outcome


def execute(request_row):
    p = request_row.payload or {}
    with transaction.atomic():
        plan = DriverFeePlan.objects.select_for_update().filter(pk=p.get('plan_id')).first()
        if plan is None or plan.status not in (DRAFT, PENDING_APPROVAL):
            raise registry.ExecutionRefused('PLAN_NOT_PENDING', 'This plan is no longer waiting.')
        bad = fee_plans.breaks_legal_share_on_normal_trip(plan)
        if bad:
            raise registry.ExecutionRefused('BREAKS_LEGAL_SHARE', f'Breaks the legal share for {", ".join(bad)}.')
        today = fee_plans.local_day(plan.country)
        start = today + timedelta(days=1)
        if fee_plans.raises_fee(plan, today):
            # An increase: drivers get the notice period first (22 Nov, the existing 7-day rule).
            from servers.pricing.commission import notice_days

            start = max(start, today + timedelta(days=notice_days(plan.country)))
        if p.get('requested_from'):
            from datetime import date

            start = max(start, date.fromisoformat(p['requested_from']))
        for old in DriverFeePlan.objects.select_for_update().filter(
                country=plan.country, scope_code=plan.scope_code, status=ACTIVE):
            old.status, old.effective_to = ARCHIVED, start
            old.save(update_fields=['status', 'effective_to'])
        plan.status, plan.effective_from, plan.activated_at = ACTIVE, start, timezone.now()
        plan.approval_request_id = request_row.id
        plan.save(update_fields=['status', 'effective_from', 'activated_at', 'approval_request_id'])
        record_admin_action(None, action='driver_fee_plan_activated', target_type=KIND,
                            target_id=str(plan.id), after=plan_dict(plan),
                            reason=request_row.decision_reason or request_row.request_reason)
    return {'plan_id': plan.id, 'effective_from': start.isoformat()}


def edit(actor, base, data, *, http_request=None):
    """Edit (25 Nov): a new version of the plan in force, or the draft that came back, sent for
    approval in one step with the impact the editor saw. The version in force stays until approved."""
    from . import edit_pattern

    data = dict(data or {})
    reason = str(data.pop('reason', '') or '').strip()
    if len(reason) < 5:
        raise InvalidRequest('Say why, in a few words.', code='REASON_REQUIRED', field='reason')
    if base.status not in (ACTIVE, DRAFT):
        raise InvalidRequest('Only the plan in force or a draft can be edited.', code='NOT_EDITABLE', field='status')
    from servers.admin_access.bridge import granted

    node = _node(base.country, base.scope_code)
    if node is None or not granted(actor, 'pricing.write', node)[0]:
        raise NotPermitted('You do not hold pricing.write here.', code='SCOPE_NOT_HELD', field='scope_code')
    if DriverFeePlan.objects.filter(country=base.country, scope_code=base.scope_code,
                                    status=PENDING_APPROVAL).exclude(pk=base.pk).exists():
        raise InvalidRequest('A change for this place is already waiting for approval: withdraw it or wait.',
                             code='CHANGE_WAITING', field='status')
    new, old, body = edit_pattern.preview(base, data)
    if not body['changes'] or (old is not None and all(c['from'] == c['to'] for c in body['changes'])):
        raise InvalidRequest('Nothing is changed.', code='NO_CHANGE', field='general')
    values = {k: data[k] for k in ('plan_type', 'amounts', 'earnings_floor_minor', 'daily_cap_minor',
                                   'waiver_first_days', 'waiver_until', 'note') if k in data}
    if base.status == DRAFT:
        plan = update_draft(actor, base, {**values, 'reason': reason})
    else:
        plan = create_draft(actor, {**plan_dict(base), **values, 'reason': reason},
                            label=_label(actor))
    return submit(actor, plan, reason=reason, http_request=http_request,
                  requested_from=body['start']['effective_from'], display=edit_pattern.display(body))


def _label(user):
    from servers.admin_audit.models import actor_snapshot

    return actor_snapshot(user).get('actor_label') or f'admin {getattr(user, "pk", "")}'


def closed(request, outcome):
    """Withdrawn, sent back, rejected or expired: the plan goes back to a draft its editor can
    change and send again. The plan in force never moved."""
    payload = request.payload or {}
    plan = DriverFeePlan.objects.filter(pk=payload.get('plan_id'), status=PENDING_APPROVAL).first()
    if plan is None:
        return
    plan.status = DRAFT
    plan.save(update_fields=['status'])
    record_admin_action(None, action='driver_fee_plan_returned_to_draft', target_type=KIND,
                        target_id=str(plan.id), after={'approval_request_id': request.id, 'outcome': outcome},
                        reason=request.decision_reason or f'request {outcome}')
