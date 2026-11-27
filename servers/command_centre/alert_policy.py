"""Who owns an alert, how old it is in words, what to do next, and when it escalates or closes.

THE RULE (owner, 22 Nov)
------------------------
Every alert has an owner, an age in human units ("21 days", "3 h") and a next step. Stale alerts
escalate on a rule, or close themselves when their cause is gone; no alert stays open for weeks
unseen. Cities that are not live raise none, and any left open there are closed. System checks
(data integrity, the scheduler, providers, the Maps budget) are Engineering's: they are shown on
the Engineering health page and sent to Sentry, never in ops "Needs attention", the live map or
the dashboard.

ESCALATION
----------
An operational alert nobody has taken:
  * after 24 h a warning becomes critical;
  * after 72 h it is also shown as escalated to the Super Admin.
Both are written into the alert's payload (`escalated`, `escalated_to`) with the time, so the
console says what happened and when.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from .models import (
    ACK_PERMISSION,
    CRITICAL,
    DATA_INTEGRITY,
    PLATFORM_ALERT_KINDS,
    WARNING,
    OperatorAlert,
)

logger = logging.getLogger(__name__)

ESCALATE_TO_CRITICAL = timedelta(hours=24)
ESCALATE_TO_SUPER_ADMIN = timedelta(hours=72)
SWEEP_LABEL = 'alert sweep'

#: The team that owns an alert, by the capability that may take it.
OWNER_BY_CAPABILITY = {
    'safety.respond': 'Safety',
    'finance.read': 'Finance',
    'finance.payout': 'Finance',
    'fraud.review': 'Fraud review',
    'driver.kyc': 'KYC',
    'trip.write': 'City Ops',
    'platform.settings': 'Engineering',
    'promo.write': 'Marketing',
    'incentive.write': 'City Ops',
}


def owner_for(alert):
    return OWNER_BY_CAPABILITY.get(ACK_PERMISSION.get(alert.kind) or '', 'City Ops')


def human_age(seconds):
    """'12 min', '3 h', '21 days' -- never '504.2 h'."""
    seconds = max(int(seconds or 0), 0)
    if seconds < 3600:
        return f'{max(seconds // 60, 1)} min'
    if seconds < 48 * 3600:
        return f'{seconds // 3600} h'
    days = seconds // 86400
    return f'{days} day' if days == 1 else f'{days} days'


def _scoped(link, alert):
    if alert.city_id and 'scope=' not in link:
        return f"{link}{'&' if '?' in link else '?'}scope={alert.city.code}"
    return link


def next_step(alert):
    """`{'text', 'link'}`: the one thing the owner should do, and where."""
    from .models import (
        COMMISSION_ABOVE_CAP,
        FINANCE_PAID_AFTER_SETTLED,
        FINANCE_PAYOUT_CONFLICT,
        FINANCE_PAYOUT_REVERSED,
        FINANCE_PLATFORM_FUNDED_END,
        FINANCE_PROMO_SETTLEMENT_FAILED,
        APPROVAL_ESCALATED,
        FRAUD_FLAG_HIGH,
        FRAUD_PAYOUT_HOLD_EXPIRED,
        FRAUD_QUEUE_AGING,
        KYC_REVIEW_PAST_TARGET,
        MAPS_BUDGET,
        NOTIFY_PROVIDER_FAILING,
        PROMOTION_BUDGET_EXHAUSTED,
        PROMOTION_BUDGET_OVERSHOOT,
        SAFETY_COVERAGE_GAP,
        SAFETY_INCIDENT_UNASSIGNED,
        SAFETY_SIGNAL,
        SAFETY_SOS_ESCALATED,
        SCHEDULER_STOPPED,
        SETTLEMENT_NO_COMMISSION_RULE,
        SOS_UNANCHORED,
        TOLL_MISMATCH,
        TOLL_MISSING,
    )

    payload = alert.payload or {}
    city = alert.city.name if alert.city_id else 'this city'
    incident = payload.get('incident_id') or (alert.source_id if alert.source_type == 'safety_incident' else None)
    metric = payload.get('metric') or ''
    k = alert.kind
    if k in (SAFETY_SOS_ESCALATED, SAFETY_INCIDENT_UNASSIGNED, SAFETY_SIGNAL, SOS_UNANCHORED):
        link = f'/safety-ops/incidents/{incident}' if incident else '/sos'
        return {'text': 'Open the case, take it and call the rider', 'link': link}
    if k == SAFETY_COVERAGE_GAP:
        return {'text': f'Put a safety responder on duty for {city}', 'link': _scoped('/safety-ops/config', alert)}
    if metric == 'withdrawal_pending':
        return {'text': 'Approve or reject the oldest waiting withdrawal', 'link': _scoped('/withdrawals', alert)}
    if metric in ('sos_open', 'sos_unacknowledged'):
        return {'text': 'Open the SOS queue and acknowledge the oldest', 'link': _scoped('/sos', alert)}
    if metric in ('unfulfilled_rate', 'acceptance_rate', 'avg_eta_minutes'):
        return {'text': f'Check drivers online and demand in {city}', 'link': _scoped('/live-ops', alert)}
    if k in (FRAUD_FLAG_HIGH, FRAUD_QUEUE_AGING):
        return {'text': 'Review the open fraud flags, oldest first', 'link': _scoped('/fraud', alert)}
    if k == FRAUD_PAYOUT_HOLD_EXPIRED:
        return {'text': 'Renew the payout hold or let the payout go', 'link': _scoped('/fraud', alert)}
    if k == APPROVAL_ESCALATED:
        return {'text': 'Decide the request or send it back with comments', 'link': '/approvals'}
    if k in (TOLL_MISMATCH, TOLL_MISSING):
        return {'text': 'Check the toll in the trip charges catalogue', 'link': _scoped('/pricing/trip-charges', alert)}
    if k == KYC_REVIEW_PAST_TARGET:
        return {'text': 'Review the drivers waiting for documents', 'link': _scoped('/kyc', alert)}
    if k in (FINANCE_PAYOUT_REVERSED, FINANCE_PAYOUT_CONFLICT):
        return {'text': 'Open the payout and resolve it', 'link': _scoped('/finance/payouts', alert)}
    if k == FINANCE_PAID_AFTER_SETTLED:
        return {'text': 'Check the refund went through', 'link': _scoped('/finance/payments', alert)}
    if k == FINANCE_PROMO_SETTLEMENT_FAILED:
        return {'text': 'Pay the driver the discount they did not collect',
                'link': _scoped('/finance/settlements', alert)}
    if k in (SETTLEMENT_NO_COMMISSION_RULE, COMMISSION_ABOVE_CAP):
        return {'text': 'Check how drivers are charged in this city', 'link': _scoped('/pricing', alert)}
    if k in (FINANCE_PLATFORM_FUNDED_END, PROMOTION_BUDGET_OVERSHOOT, PROMOTION_BUDGET_EXHAUSTED):
        return {'text': 'Open the promotion and decide whether to top it up or end it',
                'link': _scoped('/promotions', alert)}
    if k == DATA_INTEGRITY:
        return {'text': 'Engineering: check the rows listed and fix the cause', 'link': '/settings/engineering'}
    if k == SCHEDULER_STOPPED:
        return {'text': 'Engineering: restart the scheduler and worker', 'link': '/settings/engineering'}
    if k == NOTIFY_PROVIDER_FAILING:
        return {'text': 'Engineering: check the provider, switch to the backup',
                'link': '/settings/engineering'}
    if k == MAPS_BUDGET:
        return {'text': 'Engineering: check Maps spend against the budget', 'link': '/settings/engineering'}
    return {'text': 'Open it and decide', 'link': _scoped('/dashboard', alert)}


def describe(alert, now=None):
    """The words the console shows beside an alert."""
    now = now or timezone.now()
    age = (now - alert.created_at).total_seconds()
    payload = alert.payload or {}
    return {
        'owner': owner_for(alert),
        'age_seconds': int(age),
        'age_text': human_age(age),
        'next_step': next_step(alert),
        'escalated': payload.get('escalated'),
        'escalated_to': payload.get('escalated_to'),
        'engineering': alert.kind in PLATFORM_ALERT_KINDS,
    }


# ---------------------------------------------------------------------------
# The sweep: close what is over, escalate what is stale.
# ---------------------------------------------------------------------------

def _city_not_live(alert):
    from servers.admin_access.models import ACTIVE

    city = alert.city
    return city is not None and getattr(city, 'kind', 'city') == 'city' and city.status != ACTIVE


def _incident_closed(alert):
    if alert.source_type != 'safety_incident' or not alert.source_id:
        return False
    try:
        from servers.safety.models import CLOSED, SafetyIncident

        return SafetyIncident.objects.filter(pk=int(alert.source_id), status=CLOSED).exists()
    except (ValueError, ImportError):
        return False


def _integrity_superseded(alert, latest_id):
    return alert.kind == DATA_INTEGRITY and alert.source_type == 'integrity_scan' and alert.pk != latest_id


def cause_gone(alert, latest_integrity_id=None):
    """Why this alert can close itself now, or None."""
    if _city_not_live(alert):
        return f'{alert.city.name} is not live; no alerts are kept for it.'
    if _incident_closed(alert):
        return 'The safety case is closed.'
    if _integrity_superseded(alert, latest_integrity_id):
        return 'Replaced by the latest integrity scan.'
    return None


def sweep(now=None):
    """Close alerts whose cause is gone; escalate the rest when stale. Returns counts."""
    from servers.admin_audit.context import audit_context
    from servers.admin_audit.services import record_admin_action

    from .services import acknowledge

    now = now or timezone.now()
    counts = {'closed': 0, 'to_critical': 0, 'to_super_admin': 0}
    open_rows = OperatorAlert.objects.filter(acknowledged_at__isnull=True).select_related('city')
    latest = (open_rows.filter(kind=DATA_INTEGRITY, source_type='integrity_scan')
              .order_by('-created_at').values_list('id', flat=True).first())
    with audit_context(actor=None, actor_label=SWEEP_LABEL, reason='alert sweep'):
        for alert in open_rows:
            why = cause_gone(alert, latest)
            if why:
                if acknowledge(alert, by=None, note=f'closed: {why}'):
                    counts['closed'] += 1
                    record_admin_action(None, action='operator_alert_cleared', target_type='operator_alert',
                                        target_id=alert.id, before={'acknowledged': False},
                                        after={'acknowledged': True, 'cleared_by': SWEEP_LABEL,
                                               'kind': alert.kind}, reason=why)
                continue
            if alert.kind in PLATFORM_ALERT_KINDS:
                continue  # Engineering's, on its own page; not escalated through ops
            age = now - alert.created_at
            payload = dict(alert.payload or {})
            changed = []
            if age >= ESCALATE_TO_CRITICAL and alert.severity == WARNING:
                alert.severity = CRITICAL
                payload['escalated'] = f'Raised to critical after {human_age(ESCALATE_TO_CRITICAL.total_seconds())} unanswered'
                payload['escalated_at'] = now.isoformat()
                changed.append('severity')
                counts['to_critical'] += 1
            if age >= ESCALATE_TO_SUPER_ADMIN and not payload.get('escalated_to'):
                payload['escalated_to'] = 'Super Admin'
                payload['escalated_to_at'] = now.isoformat()
                counts['to_super_admin'] += 1
                changed.append('payload')
            if changed:
                alert.payload = payload
                alert.save(update_fields=sorted({*changed, 'payload'}))
                logger.warning('operator_alert_escalated_stale', extra={
                    'event': 'operator_alert_escalated_stale', 'alert_id': alert.id,
                    'kind': alert.kind, 'severity': alert.severity,
                    'escalated_to': payload.get('escalated_to')})
    return counts
