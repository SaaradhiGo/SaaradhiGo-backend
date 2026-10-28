"""Alert thresholds: writing a version, proposing a change, evaluating.

THREE WRITERS, ONE FUNCTION
---------------------------
`write_threshold_version` is the only code that creates an `AlertThreshold`
row. The seed command calls it for version 1 of every (city, metric); the
approvals executor (`command_centre.threshold_change`) calls it for every
later version, after a second `platform.settings` holder approved the
maker's proposal. Nothing else may: a threshold is a decision about when a
person gets paged, and the owner's rule is that config changes are
maker-checker actions.

A CHANGE IS ALWAYS CHECKED
--------------------------
`approvals.actions.THRESHOLD_CHANGE` is `always_checker`, like the policy
change it mirrors, so no policy row is consulted and no country can be left
with a maker who changes alert lines alone. The request is anchored at the
CITY, so the checker must hold `platform.settings` over that city (or
above it), not merely anywhere.

EVALUATION
----------
`evaluate_city` measures a city once per distinct window its thresholds
use, compares each metric with its active lines, and reconciles the
matching `OperatorAlert`:

    no alert, line crossed      -> raise (severity = the level crossed)
    alert open, level changed   -> escalate or de-escalate in place
    alert open, back in range   -> clear: acknowledged by nobody, note says why

Idempotent per (city, metric): `raise_alert` keys on (`source_type`,
`source_id`) while open, and a clear is a conditional update, so a task
that runs twice converges on one alert and one clearance.
"""

import logging

from django.db import transaction
from django.utils import timezone

from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import THRESHOLD_CHANGE
from servers.approvals.scope import may_act
from servers.approvals.service import (
    InvalidRequest, NotPermitted, require_approval_or_execute,
)

from .metrics import METRICS, RATE_WINDOWS, WINDOW_NOW, level_for, metric_for
from .models import CRITICAL, WARNING, AlertThreshold, OperatorAlert, plain_number
from .overview import city_contexts, country_code_of, measure
from .services import raise_alert

logger = logging.getLogger(__name__)

SWEEP_LABEL = 'system:alert_thresholds'
THRESHOLD_SOURCE = 'alert_threshold'


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def active_threshold(city, metric):
    return AlertThreshold.objects.filter(city=city, metric=metric, is_active=True).first()


def active_thresholds(cities=None):
    qs = AlertThreshold.objects.filter(is_active=True).select_related('city')
    if cities is not None:
        qs = qs.filter(city__in=list(cities))
    return qs


def validate_fields(metric_key, warn_value, critical_value, window):
    """Normalise and check a proposed threshold before anything is written."""
    try:
        metric = metric_for(metric_key)
    except ValueError as exc:
        raise InvalidRequest(str(exc), code='UNKNOWN_METRIC', field='metric')
    window = (window or '').strip() or metric.default_window
    if metric.is_rate and window not in RATE_WINDOWS:
        raise InvalidRequest(f'{metric_key} is measured over one of {", ".join(RATE_WINDOWS)}.',
                             code='INVALID_WINDOW', field='window')
    if not metric.is_rate and window != WINDOW_NOW:
        raise InvalidRequest(f'{metric_key} is a current value; its window is "now".',
                             code='INVALID_WINDOW', field='window')
    warn_value = _decimal_or_none(warn_value, 'warn_value')
    critical_value = _decimal_or_none(critical_value, 'critical_value')
    if warn_value is None and critical_value is None:
        raise InvalidRequest('A threshold needs a warn line or a critical line.',
                             code='NO_LINE', field='warn_value')
    if warn_value is not None and critical_value is not None:
        beyond = critical_value > warn_value if metric.direction == 'above' else critical_value < warn_value
        if not beyond:
            raise InvalidRequest('The critical line must be beyond the warn line.',
                                 code='LINES_INVERTED', field='critical_value')
    return metric, warn_value, critical_value, window


def _decimal_or_none(value, field):
    from decimal import Decimal, InvalidOperation

    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise InvalidRequest(f'{field} must be a number.', code='NOT_A_NUMBER', field=field)
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InvalidRequest(f'{field} must be a number.', code='NOT_A_NUMBER', field=field)
    if d < 0:
        raise InvalidRequest(f'{field} must not be negative.', code='NEGATIVE', field=field)
    return d.quantize(Decimal('0.01'))


def write_threshold_version(*, city, metric, warn_value, critical_value, window,
                            created_by=None, created_by_snapshot=None, reason='',
                            changed_via=None):
    """Close the active row for (city, metric) and open the next version.

    Returns `(new_threshold, superseded_or_None)`.
    """
    _metric, warn_value, critical_value, window = validate_fields(
        metric, warn_value, critical_value, window)
    now = timezone.now()
    with transaction.atomic():
        current = (
            AlertThreshold.objects.select_for_update()
            .filter(city=city, metric=metric, is_active=True).first()
        )
        latest = (
            AlertThreshold.objects.filter(city=city, metric=metric)
            .order_by('-version').values_list('version', flat=True).first()
        ) or 0
        if current is not None:
            current.is_active = False
            current.valid_to = now
            current.save(update_fields=['is_active', 'valid_to'])
        row = AlertThreshold(
            city=city, metric=metric, warn_value=warn_value, critical_value=critical_value,
            window=window, version=latest + 1, valid_from=now,
            reason=(reason or '')[:10000], changed_via=changed_via,
        )
        if created_by_snapshot is not None:
            row.set_created_by_snapshot(created_by_snapshot)
        else:
            row.set_created_by(created_by)
        row.save()
    return row, current


# ---------------------------------------------------------------------------
# Proposing a change (maker) and carrying it out (executor)
# ---------------------------------------------------------------------------

def propose_threshold_change(actor, *, city, metric, warn_value, critical_value,
                             window=None, reason='', http_request=None):
    """Raise the maker-checker request that, once approved, writes a version.

    Needs `platform.settings` over the city; a city outside the caller's
    scope answers 403 SCOPE_NOT_HELD, because there is no object here to
    be "not found". A reason is required.
    """
    metric_def, warn_value, critical_value, window = validate_fields(
        metric, warn_value, critical_value, window)
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required to change a threshold.',
                             code='REASON_REQUIRED', field='reason')
    if not may_act(actor, 'platform.settings', city):
        raise NotPermitted(f'You do not hold platform.settings over {city.code}.',
                           code='SCOPE_NOT_HELD', field='city')
    country = country_code_of(city) or 'IN'
    payload = {
        'city': city.code, 'metric': metric_def.key, 'window': window,
        'warn_value': plain_number(warn_value), 'critical_value': plain_number(critical_value),
    }
    key = (f'threshold_change:{city.code}:{metric_def.key}:{window}:'
           f'{plain_number(warn_value) or "-"}:{plain_number(critical_value) or "-"}')
    return require_approval_or_execute(
        THRESHOLD_CHANGE, actor=actor, country=country, scope_node=city,
        target_type=THRESHOLD_SOURCE, target_id=f'{city.code}:{metric_def.key}',
        payload=payload, idempotency_key=key, reason=reason, http_request=http_request,
    )


@registry.register(THRESHOLD_CHANGE)
def execute_threshold_change(request):
    """Write the version the maker proposed and the checker approved."""
    from servers.admin_access.models import GeoNode

    p = request.payload or {}
    city = GeoNode.objects.filter(code=p.get('city')).first()
    if city is None:
        raise registry.ExecutionRefused('CITY_UNKNOWN', f'No city {p.get("city")!r} exists any more.')
    row, superseded = write_threshold_version(
        city=city, metric=p['metric'], warn_value=p.get('warn_value'),
        critical_value=p.get('critical_value'), window=p.get('window'),
        created_by_snapshot={
            'actor_id': request.maker_id, 'actor_label': request.maker_label,
            'actor_email': request.maker_email, 'actor_role': request.maker_role,
        },
        reason=request.request_reason, changed_via=request,
    )
    record_admin_action(
        None,
        action='alert_threshold_changed',
        target_type=THRESHOLD_SOURCE,
        target_id=row.id,
        before=superseded.as_dict() if superseded else {},
        after=row.as_dict(),
        reason=request.decision_reason or request.request_reason,
    )
    return {
        'threshold_id': row.id,
        'version': row.version,
        'superseded_threshold_id': superseded.id if superseded else None,
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def current_value(metric, row, now=None):
    """The metric's value from an `overview.measure` row, plus the sample
    size the min-sample rule is judged on."""
    key = metric.key
    if key == 'unfulfilled_rate':
        return row['rates']['unfulfilled_rate']['now'], row['trips']['requested']['now']
    if key == 'acceptance_rate':
        return row['rates']['acceptance_rate']['now'], row['trips']['requested']['now']
    if key == 'avg_eta_minutes':
        return row['rates']['avg_eta_minutes']['now'], row['trips']['requested']['now']
    if key == 'sos_open':
        return row['queues']['sos_open']['now'], None
    if key == 'sos_unacknowledged':
        return row['queues']['sos_oldest_open_minutes'], None
    if key == 'withdrawal_pending':
        return row['queues']['withdrawals_oldest_pending_hours'], None
    raise ValueError(f'no measurement for {key!r}')  # pragma: no cover - catalogue is closed


def evaluate_threshold(threshold, row, now=None):
    """Compare one active threshold with the measured row and reconcile
    its alert. Returns `'raised'`, `'escalated'`, `'cleared'`, `'kept'`
    or `'quiet'` (nothing open, nothing crossed)."""
    now = now or timezone.now()
    metric = threshold.definition
    value, sample = current_value(metric, row, now)
    level = level_for(metric, value, threshold.warn_value, threshold.critical_value)
    if level is not None and sample is not None and sample < metric.min_sample:
        level = None  # too few requests to call it a trend

    city = threshold.city
    source_id = f'{city.code}:{metric.key}'
    existing = OperatorAlert.objects.filter(
        source_type=THRESHOLD_SOURCE, source_id=source_id, acknowledged_at__isnull=True,
    ).first()
    severity = {'critical': CRITICAL, 'warning': WARNING}.get(level)
    line = threshold.critical_value if level == 'critical' else threshold.warn_value
    payload = {
        'city': city.code,
        'city_name': city.name,
        'metric': metric.key,
        'metric_label': metric.label,
        'unit': metric.unit,
        'direction': metric.direction,
        'window': threshold.window,
        'value': value,
        'threshold': plain_number(line) if line is not None else None,
        'warn_value': plain_number(threshold.warn_value),
        'critical_value': plain_number(threshold.critical_value),
        'threshold_version': threshold.version,
        'measured_at': now.isoformat(),
    }
    title = _title(city, metric, value, line, level)

    if level is None:
        if existing is None:
            return 'quiet'
        return 'cleared' if clear_alert(existing, value, now) else 'kept'

    if existing is None:
        raise_alert(metric.alert_kind, title=title, severity=severity, payload=payload,
                    source_type=THRESHOLD_SOURCE, source_id=source_id, city=city)
        return 'raised'

    if existing.severity != severity:
        existing.severity = severity
        existing.title = title[:200]
        existing.payload = payload
        existing.save(update_fields=['severity', 'title', 'payload'])
        logger.warning('operator_alert_escalated', extra={
            'event': 'operator_alert_escalated', 'alert_id': existing.id,
            'kind': existing.kind, 'severity': severity, 'source_id': source_id,
        })
        return 'escalated'

    # Same level: refresh the measured value so the panel shows a live number.
    existing.payload = payload
    existing.save(update_fields=['payload'])
    return 'kept'


def clear_alert(alert, value, now):
    """Close an alert the sweep raised, because the number is back in range.

    Acknowledged by nobody, on purpose: nobody took it, the city recovered.
    The note and the audit row both say so, so the shift's record shows a
    self-clearing alert as what it was.
    """
    updated = OperatorAlert.objects.filter(pk=alert.pk, acknowledged_at__isnull=True).update(
        acknowledged_at=now, acknowledged_by=None,
        acknowledged_note=f'cleared: value {value} back within threshold',
    )
    if not updated:
        return False
    with audit_context(actor=None, actor_label=SWEEP_LABEL, reason='threshold sweep'):
        record_admin_action(
            None, action='operator_alert_cleared',
            target_type='operator_alert', target_id=alert.id,
            before={'acknowledged': False, 'severity': alert.severity},
            after={'acknowledged': True, 'cleared_by': SWEEP_LABEL, 'kind': alert.kind,
                   'source_type': alert.source_type, 'source_id': alert.source_id,
                   'value': value},
            reason='value back within threshold',
        )
    return True


def _title(city, metric, value, line, level):
    unit = {'percent': '%', 'minutes': ' min', 'hours': ' h', 'count': ''}[metric.unit]
    shown = 'n/a' if value is None else f'{value:g}{unit}'
    if level is None:
        return f'{city.name}: {metric.label} {shown}'
    verb = 'above' if metric.direction == 'above' else 'below'
    return f'{city.name}: {metric.label} {shown} ({verb} {plain_number(line)}{unit}, {level})'


def evaluate_city(city, thresholds, now=None):
    """Evaluate every active threshold of one city. One measurement per
    distinct window. Returns `{outcome: count}`."""
    now = now or timezone.now()
    contexts = city_contexts([city])
    rows_by_window = {}
    outcomes = {}
    for threshold in thresholds:
        window = threshold.window if threshold.definition.is_rate else '24h'
        if window not in rows_by_window:
            rows_by_window[window] = measure(contexts, window, now, previous=False)[city.id]
        outcome = evaluate_threshold(threshold, rows_by_window[window], now)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


def evaluate_all(now=None):
    """Every city with an active threshold. Returns a per-city summary."""
    now = now or timezone.now()
    by_city = {}
    for threshold in active_thresholds():
        by_city.setdefault(threshold.city_id, (threshold.city, []))[1].append(threshold)
    summary = {}
    for city_id, (city, rows) in by_city.items():
        try:
            summary[city.code] = evaluate_city(city, rows, now)
        except Exception:  # noqa: BLE001 - one city failing must not silence the rest
            logger.exception('command_centre: threshold evaluation failed for %s', city.code)
            summary[city.code] = {'error': 1}
    return summary


def seed_defaults():
    """`(city, metric) -> kwargs` for every city, from the catalogue."""
    out = []
    for metric in METRICS.values():
        out.append({
            'metric': metric.key,
            'warn_value': metric.default_warn,
            'critical_value': metric.default_critical,
            'window': metric.default_window,
        })
    return out
