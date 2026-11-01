"""Writing a commission rule: draft, submit, approve, activate. And the country config.

THE FLOW
--------
    draft  --submit-->  pending_approval  --checker approves-->  active
       ^                      |                                      |
       +---- rejected / cancelled / expired (back to draft)          +-- later superseded: archived

* Drafting and editing a draft are direct (audited): a draft changes nothing a
  driver is charged.
* `submit` raises the maker-checker request `pricing.change` (always a checker,
  in India and the US). Nothing is live until a DIFFERENT holder of
  `approval.check` + `pricing.write` approves.
* Approval runs `execute_pricing_change`, which re-validates everything against
  the world as it is THEN (cap, notice, predecessor) and activates.

THE RULES THIS FILE ENFORCES
----------------------------
* Only kind=percentage. `flat_fee` / `subscription` are refused: "not supported yet".
* 0 <= percent <= 100.
* Cap: when the country has `max_percent`, a rule above it is refused. With none
  set, activation is allowed (the console says "cap not confirmed (legal)").
  TODO(legal): go-live blocker #13.
* Notice: a rule that RAISES the commission cannot take effect sooner than
  now + notice_days (country config, default 7). A decrease may take effect at
  once (standard practice: it favours the driver; recorded as a decision).
  "Raises" means above the rate currently governing that scope.
* Never retroactive: an `effective_from` in the past is moved to the moment of
  activation. Trips already booked keep their snapshot regardless.
* A driver notice (outbox row) is written for every raise. Sending is a stub
  until the Notifications module (13) exists.

The same executor also carries the fare-card requests (`rate_card.*`) so that
`pricing.change` has ONE registered executor; see `fare_card_workflow`.
"""

import logging
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from base.countries import COUNTRIES
from servers.admin_access.models import GeoNode
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import PRICING_CHANGE
from servers.approvals.service import (
    ApprovalError, InvalidRequest, NotInScope, NotPermitted, WrongState,
    require_approval_or_execute,
)
from servers.driver.models import VehicleType

from . import commission as resolver
from .commission_models import (
    ACTIVE, ARCHIVED, DRAFT, IMPLEMENTED_KINDS, KIND_CHOICES, PENDING_APPROVAL, PERCENTAGE,
    CommissionCountryConfig, CommissionRule, DriverNotice,
)

logger = logging.getLogger(__name__)

NOT_SUPPORTED = 'not supported yet'
PAYLOAD_RULE = 'commission_rule'
PAYLOAD_CONFIG = 'commission_country_config'


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _percent(value, field='percent'):
    try:
        pct = Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidRequest('percent must be a number.', code='INVALID_PERCENT', field=field)
    if pct < 0 or pct > 100:
        raise InvalidRequest('percent must be between 0 and 100.', code='INVALID_PERCENT',
                             field=field)
    return pct


def _aware(value):
    from django.utils.dateparse import parse_datetime

    if value in (None, ''):
        return None
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is None:
            raise InvalidRequest('effective_from must be an ISO date-time.',
                                 code='INVALID_DATE', field='effective_from')
        value = parsed
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value


def snapshot_of(actor):
    from servers.admin_audit.models import actor_snapshot

    return actor_snapshot(actor)


def rule_dict(rule, now=None):
    now = now or timezone.now()
    if rule.status in (DRAFT, PENDING_APPROVAL):
        state = rule.status
    elif rule.effective_from > now:
        state = 'scheduled'
    elif rule.effective_to is not None and rule.effective_to <= now:
        state = 'superseded'
    else:
        state = 'live'
    node = rule.geo_node
    return {
        'id': rule.id, 'country': rule.country,
        'city': node.code if node is not None and node.kind == 'city' else None,
        'city_name': node.name if node is not None and node.kind == 'city' else None,
        'scope_node': node.code if node is not None else None,
        'vehicle_type': rule.vehicle_type_id,
        'vehicle_type_name': rule.vehicle_type.type if rule.vehicle_type_id else None,
        'level': rule.level, 'kind': rule.kind, 'percent': str(rule.percent),
        'params': rule.params, 'status': rule.status, 'state': state, 'version': rule.version,
        'effective_from': rule.effective_from.isoformat(),
        'effective_to': rule.effective_to.isoformat() if rule.effective_to else None,
        'reason': rule.reason,
        'created_by': {'id': rule.created_by_id, 'email': rule.created_by_email,
                       'label': rule.created_by_label, 'role': rule.created_by_role},
        'approval_request_id': rule.approval_request_id,
        'submitted_at': rule.submitted_at.isoformat() if rule.submitted_at else None,
        'activated_at': rule.activated_at.isoformat() if rule.activated_at else None,
        'effective_from_adjusted': rule.effective_from_adjusted,
        'created_at': rule.created_at.isoformat(),
    }


def config_dict(country, row):
    return {
        'country': country, 'configured': row is not None,
        'notice_days': row.notice_days if row else 7,
        'max_percent': str(row.max_percent) if row and row.max_percent is not None else None,
        'cap_status': 'set' if row and row.max_percent is not None
        else 'cap not confirmed (legal)',
        'version': row.version if row else 0,
        'updated_at': row.updated_at.isoformat() if row else None,
        'updated_by': row.updated_by_label if row else '',
    }


# ---------------------------------------------------------------------------
# Scope and validation
# ---------------------------------------------------------------------------

def resolve_scope(user, country, city_code, vehicle_type_id, *, permission='pricing.write'):
    """`(geo_node, vehicle_type)` for a rule's scope key, or an ApprovalError.

    A country or city the caller cannot reach answers as not found, like every
    out-of-scope object; one they can reach but lack `permission` over is 403.
    """
    from servers.admin_access.bridge import granted
    from servers.admin_access.scoping import scoped_or_none

    country = str(country or '').upper().strip()
    if country not in COUNTRIES:
        raise InvalidRequest(f'Unknown country {country!r}.', code='UNKNOWN_COUNTRY',
                             field='country')
    country_node = GeoNode.objects.filter(kind='country', code=country).first()
    node = country_node
    if city_code:
        node = scoped_or_none(user, GeoNode.objects.filter(kind='city'), code=str(city_code))
        if node is None or country_node is None or not node.path.startswith(country_node.path):
            raise NotInScope('No such city.', field='city')
    elif country_node is not None:
        if scoped_or_none(user, GeoNode.objects.filter(pk=country_node.pk)) is None:
            raise NotInScope('No such country.', field='country')
    vt = None
    if vehicle_type_id not in (None, ''):
        vt = VehicleType.objects.filter(pk=vehicle_type_id).first()
        if vt is None:
            raise InvalidRequest('Unknown vehicle class.', code='UNKNOWN_VEHICLE_TYPE',
                                 field='vehicle_type')
    if not granted(user, permission, node)[0]:
        raise NotPermitted(f'You do not hold {permission} here.', code='SCOPE_NOT_HELD',
                           field='country')
    return node, vt


def _current_rate(country, node, vt_id, now):
    """The percent currently governing this scope key (its own rule, else the next
    layer up), or None."""
    city_id = node.id if node is not None and node.kind == 'city' else None
    # A vehicle-only or country rule governs "this scope" through resolve with the
    # most specific coordinates the key can express.
    rule = resolver.resolve(country, city_id, vt_id, now)
    return rule.percent if rule is not None else None


def is_increase(country, node, vt_id, percent, now):
    before = _current_rate(country, node, vt_id, now)
    return before is not None and percent > before


def check_rule(country, node, vt, kind, percent, effective_from, now, *, enforce_notice=True):
    """Validate a rule's content. Returns `(percent, kind)` or raises."""
    if kind not in {k for k, _ in KIND_CHOICES}:
        raise InvalidRequest(f'Unknown kind {kind!r}.', code='UNKNOWN_KIND', field='kind')
    if kind not in IMPLEMENTED_KINDS:
        raise InvalidRequest(f'A {kind.replace("_", " ")} commission is {NOT_SUPPORTED}.',
                             code='KIND_NOT_SUPPORTED', field='kind')
    pct = _percent(percent)
    cap = resolver.max_percent(country)
    if cap is not None and pct > cap:
        raise InvalidRequest(
            f'{pct}% is above the {country} commission cap of {cap}%.',
            code='ABOVE_CAP', field='percent')
    if enforce_notice and is_increase(country, node, vt.id if vt else None, pct, now):
        earliest = now + timedelta(days=resolver.notice_days(country))
        if effective_from < earliest:
            raise InvalidRequest(
                f'A rule that raises the commission needs {resolver.notice_days(country)} '
                f'days notice for drivers; the earliest effective date is '
                f'{earliest.isoformat()}.', code='NOTICE_TOO_SHORT', field='effective_from')
    return pct, kind


def _next_version(country, node, vt):
    last = (CommissionRule.objects.filter(country=country, geo_node=node, vehicle_type=vt)
            .order_by('-version').values_list('version', flat=True).first())
    return (last or 0) + 1


def _open_draft(country, node, vt):
    return CommissionRule.objects.filter(
        country=country, geo_node=node, vehicle_type=vt,
        status__in=(DRAFT, PENDING_APPROVAL)).first()


# ---------------------------------------------------------------------------
# Draft: create, edit, discard
# ---------------------------------------------------------------------------

def create_draft(actor, *, country, city_code=None, vehicle_type_id=None, kind=PERCENTAGE,
                 percent=None, effective_from=None, reason='', http_request=None):
    now = timezone.now()
    node, vt = resolve_scope(actor, country, city_code, vehicle_type_id)
    country = str(country).upper().strip()
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    if kind != PERCENTAGE:
        check_rule(country, node, vt, kind, percent or 0, now, now)  # raises "not supported yet"
    if percent in (None, ''):
        raise InvalidRequest('percent is required.', code='INVALID_PERCENT', field='percent')
    pct = _percent(percent)
    eff = _aware(effective_from)
    if eff is None:
        eff = now
        if is_increase(country, node, vt.id if vt else None, pct, now):
            eff = now + timedelta(days=resolver.notice_days(country))
    pct, kind = check_rule(country, node, vt, kind, pct, eff, now)
    with transaction.atomic():
        if _open_draft(country, node, vt) is not None:
            raise WrongState('This scope already has a draft or a pending rule; finish or '
                             'discard it first.', code='DRAFT_EXISTS', field='scope')
        snap = snapshot_of(actor)
        rule = CommissionRule.objects.create(
            country=country, geo_node=node, vehicle_type=vt, kind=kind, percent=pct,
            status=DRAFT, version=_next_version(country, node, vt), effective_from=eff,
            reason=reason.strip()[:10000], created_by_id=snap['actor_id'],
            created_by_label=snap['actor_label'], created_by_email=snap['actor_email'],
            created_by_role=snap['actor_role'])
        record_admin_action(
            http_request, action='commission_rule_drafted', target_type='commission_rule',
            target_id=rule.id, after=rule_dict(rule), reason=reason)
    return rule


def update_draft(actor, rule, *, percent=None, effective_from=None, reason=None,
                 http_request=None):
    if rule.status != DRAFT:
        raise WrongState('Only a draft can be edited.', code='NOT_A_DRAFT', field='status')
    now = timezone.now()
    node, vt = rule.geo_node, rule.vehicle_type
    before = rule_dict(rule)
    pct = _percent(percent) if percent not in (None, '') else rule.percent
    eff = _aware(effective_from) or rule.effective_from
    pct, _ = check_rule(rule.country, node, vt, rule.kind, pct, eff, now)
    rule.percent, rule.effective_from = pct, eff
    if reason is not None and reason.strip():
        rule.reason = reason.strip()[:10000]
    rule.save()
    record_admin_action(http_request, action='commission_rule_edited',
                        target_type='commission_rule', target_id=rule.id,
                        before=before, after=rule_dict(rule), reason=rule.reason)
    return rule


def discard_draft(actor, rule, *, http_request=None):
    if rule.status != DRAFT:
        raise WrongState('Only a draft can be discarded.', code='NOT_A_DRAFT', field='status')
    before = rule_dict(rule)
    rule_id = rule.id
    rule.delete()
    record_admin_action(http_request, action='commission_rule_discarded',
                        target_type='commission_rule', target_id=rule_id, before=before,
                        reason='draft discarded')


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

def submit(actor, rule, *, reason='', http_request=None):
    """Raise the `pricing.change` request for a draft. Returns the Outcome."""
    if rule.status != DRAFT:
        raise WrongState('Only a draft can be submitted.', code='NOT_A_DRAFT', field='status')
    now = timezone.now()
    node, vt = resolve_scope(actor, rule.country, rule.geo_node.code
                             if rule.geo_node and rule.geo_node.kind == 'city' else None,
                             rule.vehicle_type_id)
    eff = max(rule.effective_from, now) if rule.effective_from < now else rule.effective_from
    # The notice period was enforced when the date was chosen. It is NOT re-enforced
    # here: a default date (draft time + notice) is a moment short of "now + notice"
    # by the time the maker submits. Activation guarantees it, moving the date later.
    check_rule(rule.country, node, vt, rule.kind, rule.percent, eff, now, enforce_notice=False)
    why = (reason or rule.reason or '').strip()
    if not why:
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    with transaction.atomic():
        outcome = require_approval_or_execute(
            PRICING_CHANGE, actor=actor, country=rule.country, scope_node=node,
            target_type=PAYLOAD_RULE, target_id=str(rule.id),
            payload={'kind': PAYLOAD_RULE, 'rule_id': rule.id, 'percent': str(rule.percent),
                     'country': rule.country, 'scope': node.code if node else None,
                     'vehicle_type': rule.vehicle_type_id, 'version': rule.version,
                     'effective_from': rule.effective_from.isoformat()},
            idempotency_key=f'commission_rule:{rule.id}:activate', reason=why,
            http_request=http_request)
        req = outcome.request
        if not outcome.duplicate:
            rule.status = PENDING_APPROVAL
            rule.approval_request_id = req.id
            rule.submitted_at = now
            rule.reason = why[:10000]
            rule.save()
            record_admin_action(http_request, action='commission_rule_submitted',
                                target_type='commission_rule', target_id=rule.id,
                                after={**rule_dict(rule), 'approval_request_id': req.id},
                                reason=why)
    return outcome


@registry.on_closed(PRICING_CHANGE)
def _request_closed(request, outcome):
    """Rejected, cancelled or expired: the rule goes back to a draft the maker may
    edit and submit again."""
    payload = request.payload or {}
    if payload.get('kind') != PAYLOAD_RULE:
        return
    rule = CommissionRule.objects.filter(
        pk=payload.get('rule_id'), approval_request_id=request.id,
        status=PENDING_APPROVAL).first()
    if rule is None:
        return
    rule.status = DRAFT
    rule.save(update_fields=['status', 'updated_at'])
    record_admin_action(None, action='commission_rule_returned_to_draft',
                        target_type='commission_rule', target_id=rule.id,
                        after={'approval_request_id': request.id, 'outcome': outcome},
                        reason=request.decision_reason or f'request {outcome}')


# ---------------------------------------------------------------------------
# Activation (the executor body)
# ---------------------------------------------------------------------------

def activate_rule(rule_id, request_row, *, now=None, actor_reason=''):
    """Activate a pending rule. Raises `registry.ExecutionRefused` when the world
    moved. Returns a result dict."""
    now = now or timezone.now()
    with transaction.atomic():
        rule = (CommissionRule.objects.select_for_update(of=('self',))
                .select_related('geo_node', 'vehicle_type').filter(pk=rule_id).first())
        if rule is None:
            raise registry.ExecutionRefused('RULE_GONE', 'The draft no longer exists.')
        if rule.status != PENDING_APPROVAL or rule.approval_request_id != request_row.id:
            raise registry.ExecutionRefused(
                'RULE_NOT_PENDING', 'This rule is no longer waiting for this approval.')
        if rule.kind not in IMPLEMENTED_KINDS:
            raise registry.ExecutionRefused('KIND_NOT_SUPPORTED',
                                            f'{rule.kind} is {NOT_SUPPORTED}.')
        cap = resolver.max_percent(rule.country)
        if cap is not None and rule.percent > cap:
            raise registry.ExecutionRefused(
                'ABOVE_CAP', f'{rule.percent}% is above the {rule.country} cap of {cap}%.')

        eff, adjusted = rule.effective_from, False
        if eff < now:
            eff, adjusted = now, True  # never retroactive
        raised = is_increase(rule.country, rule.geo_node, rule.vehicle_type_id,
                             rule.percent, now)
        old = _current_rate(rule.country, rule.geo_node, rule.vehicle_type_id, now)
        if raised:
            earliest = now + timedelta(days=resolver.notice_days(rule.country))
            if eff < earliest:
                eff, adjusted = earliest, True

        siblings = (CommissionRule.objects.select_for_update()
                    .filter(country=rule.country, geo_node=rule.geo_node,
                            vehicle_type=rule.vehicle_type, status__in=(ACTIVE, ARCHIVED),
                            effective_to__isnull=True).exclude(pk=rule.pk))
        for prev in siblings:
            if eff <= prev.effective_from:
                raise registry.ExecutionRefused(
                    'EFFECTIVE_BEFORE_PREDECESSOR',
                    'An approved rule already takes effect at or after this date.')
            prev.effective_to = eff
            prev.status = ARCHIVED
            prev.save(update_fields=['effective_to', 'status', 'updated_at'])

        rule.status = ACTIVE
        rule.effective_from = eff
        rule.effective_from_adjusted = adjusted
        rule.activated_at = now
        rule.save()

        notice_id = None
        if raised:
            notice = DriverNotice.objects.create(
                rule=rule, country=rule.country, geo_node=rule.geo_node,
                vehicle_type=rule.vehicle_type, old_percent=old, new_percent=rule.percent,
                effective_from=eff)
            notice_id = notice.id
        record_admin_action(
            None, action='commission_rule_activated', target_type='commission_rule',
            target_id=rule.id, after=rule_dict(rule, now),
            reason=request_row.decision_reason or request_row.request_reason or actor_reason)
    if notice_id:
        from .driver_notices import deliver
        deliver(DriverNotice.objects.get(pk=notice_id))
    return {'rule_id': rule.id, 'version': rule.version, 'effective_from': eff.isoformat(),
            'effective_from_adjusted': adjusted, 'raised': raised,
            'driver_notice_id': notice_id}


# ---------------------------------------------------------------------------
# Country config (notice days, cap)
# ---------------------------------------------------------------------------

def propose_country_config(actor, *, country, notice_days=None, max_percent=None,
                           clear_cap=False, reason='', http_request=None):
    from servers.admin_access.bridge import granted
    from servers.admin_access.scoping import scoped_or_none

    country = str(country or '').upper()
    if country not in COUNTRIES:
        raise InvalidRequest(f'Unknown country {country!r}.', code='UNKNOWN_COUNTRY',
                             field='country')
    node = GeoNode.objects.filter(kind='country', code=country).first()
    if node is not None and scoped_or_none(actor, GeoNode.objects.filter(pk=node.pk)) is None:
        raise NotInScope('No such country.', field='country')
    if not granted(actor, 'pricing.write', node)[0]:
        raise NotPermitted('You do not hold pricing.write here.', code='SCOPE_NOT_HELD',
                           field='country')
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    current = CommissionCountryConfig.objects.filter(country=country).first()
    new_notice = current.notice_days if current else 7
    if notice_days not in (None, ''):
        try:
            new_notice = int(notice_days)
        except (TypeError, ValueError):
            raise InvalidRequest('notice_days must be a whole number.', code='INVALID_NOTICE',
                                 field='notice_days')
        if not 0 <= new_notice <= 365:
            raise InvalidRequest('notice_days must be 0 to 365.', code='INVALID_NOTICE',
                                 field='notice_days')
    new_cap = current.max_percent if current else None
    if clear_cap:
        new_cap = None
    elif max_percent not in (None, ''):
        new_cap = _percent(max_percent, 'max_percent')
    payload = {'kind': PAYLOAD_CONFIG, 'country': country, 'notice_days': new_notice,
               'max_percent': str(new_cap) if new_cap is not None else None,
               'base_version': current.version if current else 0}
    key = (f'commission_config:{country}:{new_notice}:{payload["max_percent"] or "-"}:'
           f'{payload["base_version"]}')
    return require_approval_or_execute(
        PRICING_CHANGE, actor=actor, country=country, scope_node=node,
        target_type=PAYLOAD_CONFIG, target_id=country, payload=payload,
        idempotency_key=key, reason=reason, http_request=http_request)


def apply_country_config(request_row):
    p = request_row.payload or {}
    country = p['country']
    with transaction.atomic():
        row = CommissionCountryConfig.objects.select_for_update().filter(country=country).first()
        seen = row.version if row else 0
        if seen != p.get('base_version'):
            raise registry.ExecutionRefused(
                'CONFIG_CHANGED', 'The commission settings changed after this request was '
                'raised; raise it again.')
        before = config_dict(country, row)
        cap = Decimal(p['max_percent']) if p.get('max_percent') is not None else None
        if row is None:
            row = CommissionCountryConfig(country=country)
        row.notice_days, row.max_percent = int(p['notice_days']), cap
        row.version = seen + 1
        row.updated_by_label = request_row.maker_label
        row.reason = request_row.request_reason
        row.save()
        record_admin_action(
            None, action='commission_country_config_changed',
            target_type='commission_country_config', target_id=country, before=before,
            after=config_dict(country, row),
            reason=request_row.decision_reason or request_row.request_reason)
    return {'country': country, 'version': row.version}


# ---------------------------------------------------------------------------
# The one executor for pricing.change
# ---------------------------------------------------------------------------

@registry.register(PRICING_CHANGE)
def execute_pricing_change(request_row):
    payload = request_row.payload or {}
    kind = payload.get('kind')
    if kind == PAYLOAD_RULE:
        return activate_rule(payload.get('rule_id'), request_row)
    if kind == PAYLOAD_CONFIG:
        return apply_country_config(request_row)
    from . import fare_card_workflow

    if kind in fare_card_workflow.KINDS:
        return fare_card_workflow.execute(kind, request_row)
    raise registry.ExecutionRefused('UNKNOWN_PRICING_CHANGE',
                                    f'Unknown pricing change {kind!r}.')


__all__ = ['ApprovalError']
