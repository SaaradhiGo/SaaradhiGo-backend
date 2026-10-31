"""Reading, proposing and versioning the detection rule settings (`RiskRuleConfig`).

NO FALLBACK
-----------
`config_for(country, rule_key)` returns the active row or None. A country with no row does not run
that rule; nothing reaches for another country's numbers. `seed_fraud_rules` writes version 1 from
each rule's standard defaults (India and United States), and never overwrites.

CHANGES GO THROUGH A CHECKER, ALWAYS
------------------------------------
`propose_change` raises a `fraud.rule_change` request (policy rows are `always`, India and United
States); nothing is applied until a different `fraud.rules` holder with `approval.check` approves,
and then `execute_rule_change` closes the active row and opens the next version in one transaction
and writes one audit row with before and after. A request that changes nothing writes nothing. A
new version only affects flags raised from then on: `RiskFlag.rule_version` says which numbers
raised an older one.
"""

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from base.countries import COUNTRIES
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import FRAUD_RULE_CHANGE

from .models import RiskRuleConfig
from .rules import RULES, SCHEDULED, rule_for

TARGET = 'fraud_rule_config'
AUDIT_ACTION = 'fraud_rule_changed'


class ConfigError(Exception):
    def __init__(self, code, message, field='general'):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


def config_for(country, rule_key):
    return RiskRuleConfig.objects.filter(country=(country or '').upper(), rule_key=rule_key,
                                         is_active=True).first()


def thresholds_of(rule, config):
    """The numbers a run uses: the rule's defaults for the country under the stored ones."""
    values = rule.defaults(config.country)
    values.update({k: v for k, v in (config.thresholds or {}).items() if rule.param(k)})
    return values


def clean_thresholds(rule, data, current=None):
    """The full threshold dict after applying `data` to `current`; raises on a bad value."""
    if not rule.params:
        raise ConfigError('NO_THRESHOLDS', f'{rule.title} has no thresholds to change.')
    if not isinstance(data, dict):
        raise ConfigError('INVALID_THRESHOLDS', 'thresholds must be an object.', 'thresholds')
    values = dict(current or {})
    for key, raw in data.items():
        param = rule.param(key)
        if param is None:
            raise ConfigError('UNKNOWN_THRESHOLD', f'{key} is not a threshold of {rule.key}.', key)
        if isinstance(raw, bool) or raw is None or str(raw).strip() == '':
            raise ConfigError('INVALID_VALUE', f'{param.label} must be a whole number.', key)
        try:
            number = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigError('INVALID_VALUE', f'{param.label} must be a whole number.', key)
        if not param.low <= number <= param.high:
            raise ConfigError('INVALID_VALUE',
                              f'{param.label} must be between {param.low} and {param.high}.', key)
        values[key] = number
    return values


def write_version(*, country, rule_key, thresholds, enabled, reason, created_by=None,
                  created_by_snapshot=None, changed_via=None):
    """Close the active row and open the next version. Returns `(row, superseded_or_None)`."""
    with transaction.atomic():
        current = (RiskRuleConfig.objects.select_for_update()
                   .filter(country=country, rule_key=rule_key, is_active=True).first())
        version = 1
        if current is not None:
            current.is_active, current.valid_to = False, timezone.now()
            current.save(update_fields=['is_active', 'valid_to'])
            version = current.version + 1
        row = RiskRuleConfig(country=country, rule_key=rule_key, thresholds=thresholds,
                             enabled=enabled, version=version, reason=(reason or '')[:2000],
                             changed_via=changed_via,
                             last_run_at=current.last_run_at if current else None)
        if created_by_snapshot is not None:
            row.set_created_by_snapshot(created_by_snapshot)
        elif created_by is not None:
            row.set_created_by(created_by)
        row.save()
    return row, current


def seed_defaults(actor, reason):
    """Version 1 of every scheduled rule for the seeded countries, only where none exists."""
    created, skipped = [], []
    for country in ('IN', 'US'):
        for key in SCHEDULED:
            if RiskRuleConfig.objects.filter(country=country, rule_key=key).exists():
                skipped.append(f'{country}:{key}')
                continue
            row, _ = write_version(country=country, rule_key=key,
                                   thresholds=RULES[key].defaults(country), enabled=True,
                                   reason=reason, created_by=actor)
            created.append(row)
    return created, skipped


# ---------------------------------------------------------------------------
# Proposing a change
# ---------------------------------------------------------------------------

def propose_change(actor, *, country, rule_key, thresholds=None, enabled=None, reason='',
                   http_request=None):
    """Raise the maker-checker request for a change. Returns `(outcome, changed)`.

    Needs `fraud.rules` over the country. Nothing is applied here.
    """
    from servers.admin_access.bridge import granted
    from servers.admin_access.models import COUNTRY, GeoNode
    from servers.approvals import service as approvals

    country = (country or '').upper()
    if country not in COUNTRIES:
        raise ConfigError('UNKNOWN_COUNTRY', f'{country!r} is not a supported country.', 'country')
    try:
        rule = rule_for(rule_key)
    except ValueError:
        raise ConfigError('UNKNOWN_RULE', f'{rule_key!r} is not a rule.', 'rule_key')
    reason = (reason or '').strip()
    if len(reason) < 5:
        raise ConfigError('REASON_REQUIRED', 'A reason of at least 5 characters is required.',
                          'reason')
    current = config_for(country, rule_key)
    if current is None:
        raise ConfigError('NOT_CONFIGURED', f'{rule.title} is not configured for {country}.')
    node = GeoNode.objects.filter(kind=COUNTRY, code=country).first()
    if not granted(actor, 'fraud.rules', node)[0]:
        raise ConfigError('SCOPE_NOT_HELD', f'You do not hold fraud.rules over {country}.',
                          'country')
    merged = clean_thresholds(rule, thresholds or {}, thresholds_of(rule, current))
    new_enabled = current.enabled if enabled is None else bool(enabled)
    if merged == thresholds_of(rule, current) and new_enabled == current.enabled:
        return None, False
    payload = {'country': country, 'rule_key': rule_key, 'thresholds': merged,
               'enabled': new_enabled, 'base_version': current.version}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    outcome = approvals.require_approval_or_execute(
        FRAUD_RULE_CHANGE, actor=actor, country=country, scope_node=node,
        target_type=TARGET, target_id=f'{country}:{rule_key}', payload=payload,
        idempotency_key=f'fraud_rule:{country}:{rule_key}:{digest}', reason=reason,
        http_request=http_request)
    return outcome, True


def pending_requests(countries=None):
    """Open `fraud.rule_change` requests, newest first, as plain dicts."""
    from servers.approvals.models import REQUESTED, ApprovalRequest

    rows = []
    for r in ApprovalRequest.objects.filter(action_key=FRAUD_RULE_CHANGE,
                                            status=REQUESTED).order_by('-id')[:100]:
        if countries is not None and r.country not in countries:
            continue
        p = r.payload or {}
        rows.append({
            'id': r.id, 'country': r.country, 'rule_key': p.get('rule_key'),
            'thresholds': p.get('thresholds') or {}, 'enabled': p.get('enabled'),
            'base_version': p.get('base_version'), 'reason': r.request_reason,
            'maker_label': r.maker_label, 'maker_id': r.maker_id,
            'requested_at': r.requested_at.isoformat() if r.requested_at else None,
            'expires_at': r.expires_at.isoformat() if r.expires_at else None,
        })
    return rows


@registry.register(FRAUD_RULE_CHANGE)
def execute_rule_change(request):
    """Write the version the maker proposed and the checker approved."""
    p = request.payload or {}
    country, rule_key = p.get('country'), p.get('rule_key')
    try:
        rule = rule_for(rule_key)
        thresholds = clean_thresholds(rule, p.get('thresholds') or {})
    except (ValueError, ConfigError) as exc:
        raise registry.ExecutionRefused('INVALID_RULE_CHANGE', str(exc))
    current = config_for(country, rule_key)
    if current is None:
        raise registry.ExecutionRefused('NOT_CONFIGURED',
                                        f'{rule_key} is not configured for {country}.')
    enabled = bool(p.get('enabled'))
    if thresholds == thresholds_of(rule, current) and enabled == current.enabled:
        return {'changed': False, 'version': current.version}
    before = current.as_dict()
    row, _superseded = write_version(
        country=country, rule_key=rule_key, thresholds=thresholds, enabled=enabled,
        reason=request.request_reason, changed_via=request,
        created_by_snapshot={
            'actor_id': request.maker_id, 'actor_label': request.maker_label,
            'actor_email': request.maker_email, 'actor_role': request.maker_role})
    record_admin_action(
        None, action=AUDIT_ACTION, target_type=TARGET, target_id=f'{country}:{rule_key}',
        before=before, after=row.as_dict(),
        reason=request.decision_reason or request.request_reason)
    return {'changed': True, 'version': row.version, 'config_id': row.id}
