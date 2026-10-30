"""Changing a support SLA value is a maker-checker action.

`support.sla_change` always needs a checker (policy rows are `always`, so
`maker_limit` is null) in every country that has a policy row for it; a
country with none is refused `THRESHOLD_NOT_CONFIGURED` like every other
action. Two kinds of change travel under it:

* `sla_policy`: first-response / resolution minutes for (country, priority);
* `country_config`: `SupportCountryConfig` (waiting reminder, auto-close,
  refund review) for a country.

The maker (a `support.sla` holder over the country: Super Admin, or a City
Ops Manager inside their scope) raises a request; a different `support.sla`
holder with `approval.check` approves it and THIS executor applies the
change and writes the audit row. Nothing is applied before approval.
"""

from django.db import transaction

from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import SUPPORT_SLA_CHANGE

from .models import SupportCountryConfig, SupportSlaPolicy

SLA_TARGET = 'support_sla'
CONFIG_TARGET = 'support_country_config'
MAX_MINUTES = 60 * 24 * 90

KIND_POLICY = 'sla_policy'
KIND_CONFIG = 'country_config'


def check_policy(first, resolution):
    """An error message, or None."""
    if first < 1 or resolution < 1:
        return 'Minutes must be at least 1.'
    if resolution < first:
        return 'Resolution cannot be shorter than first response.'
    if max(first, resolution) > MAX_MINUTES:
        return 'Minutes must be at most 90 days.'
    return None


def check_config(reminder_days, autoclose_days, refund_hours):
    """An error message, or None."""
    if not 1 <= reminder_days <= 90 or not 1 <= autoclose_days <= 365:
        return 'Days must be between 1 and 90 (reminder) and 1 and 365 (auto-close).'
    if autoclose_days <= reminder_days:
        return 'Auto-close must come after the reminder.'
    if not 1 <= refund_hours <= 24 * 60:
        return 'Refund review hours must be between 1 and 1440.'
    return None


def policy_dict(country, priority, row):
    from .sla import DEFAULT_MINUTES

    first, resolution = (DEFAULT_MINUTES[priority] if row is None
                         else (row.first_response_minutes, row.resolution_minutes))
    return {'country': country, 'priority': priority,
            'first_response_minutes': first, 'resolution_minutes': resolution,
            'source': 'default' if row is None else 'policy'}


def config_dict(country, row):
    if row is None:
        return {'country': country, 'configured': False, 'waiting_reminder_days': None,
                'waiting_autoclose_days': None, 'refund_review_hours': None, 'version': 0}
    return {'country': country, 'configured': True,
            'waiting_reminder_days': row.waiting_reminder_days,
            'waiting_autoclose_days': row.waiting_autoclose_days,
            'refund_review_hours': row.refund_review_hours, 'version': row.version}


@registry.register(SUPPORT_SLA_CHANGE)
def execute_sla_change(request_row):
    """Apply the approved change. Idempotent on the current values."""
    p = request_row.payload or {}
    checker_reason = request_row.decision_reason or request_row.request_reason \
        or 'SLA change approved'
    maker = {'maker_id': request_row.maker_id, 'approval_request_id': request_row.id}
    if p.get('kind') == KIND_POLICY:
        country, priority = p['country'], p['priority']
        first, resolution = int(p['first_response_minutes']), int(p['resolution_minutes'])
        error = check_policy(first, resolution)
        if error:
            raise registry.ExecutionRefused('INVALID_MINUTES', error)
        with transaction.atomic():
            row = SupportSlaPolicy.objects.select_for_update().filter(
                country=country, priority=priority).first()
            before = policy_dict(country, priority, row)
            if row is not None and (row.first_response_minutes, row.resolution_minutes) == (
                    first, resolution):
                return {'changed': False, **before}
            if row is None:
                row = SupportSlaPolicy(country=country, priority=priority)
            row.first_response_minutes, row.resolution_minutes = first, resolution
            row.updated_by_id = request_row.maker_id
            row.save()
            after = policy_dict(country, priority, row)
            record_admin_action(
                None, action='support_sla_changed', target_type=SLA_TARGET,
                target_id=f'{country}:{priority}', before=before, after={**after, **maker},
                reason=checker_reason)
        return {'changed': True, **after}
    if p.get('kind') == KIND_CONFIG:
        country = p['country']
        values = (int(p['waiting_reminder_days']), int(p['waiting_autoclose_days']),
                  int(p['refund_review_hours']))
        error = check_config(*values)
        if error:
            raise registry.ExecutionRefused('INVALID_CONFIG', error)
        with transaction.atomic():
            row = SupportCountryConfig.objects.select_for_update().filter(
                country=country).first()
            before = config_dict(country, row)
            if row is not None and (row.waiting_reminder_days, row.waiting_autoclose_days,
                                    row.refund_review_hours) == values:
                return {'changed': False, **before}
            if row is None:
                row = SupportCountryConfig(country=country, version=0)
            (row.waiting_reminder_days, row.waiting_autoclose_days,
             row.refund_review_hours) = values
            row.version += 1
            row.updated_by_id = request_row.maker_id
            row.save()
            after = config_dict(country, row)
            record_admin_action(
                None, action='support_country_config_changed', target_type=CONFIG_TARGET,
                target_id=country, before=before, after={**after, **maker},
                reason=checker_reason)
        return {'changed': True, **after}
    raise registry.ExecutionRefused('UNKNOWN_CHANGE', 'Unknown SLA change.')
