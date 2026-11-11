"""Safety coverage: is anyone on the hook for this city?

WHAT COUNTS
-----------
A city's coverage is the number of DISTINCT people who hold an active
`safety_officer` assignment whose scope covers the city (Global, country,
state or the city itself), where the person is an active staff account and
not the break-glass account.

Decision (owner asked for "Safety Officer assignments"): ONLY the
`safety_officer` role template counts. Super Admin and Global assignments do
not, and neither does a custom role that happens to carry `safety.respond`.
A Super Admin is a name on every alert, not a person on shift; counting them
would let a city go live with nobody rostered. This leaves no seeded QA city
at zero: `seed_admin_access --test-admins` assigns `qa.safety` as a Safety
Officer at the live QA city, and the US city is paused (the guard does not apply to
a city that is not live). A deployment that has not assigned anyone to a live
city SHOULD see the alert; that is the guard working.

NO CONFIG, NO PASS
------------------
The minimum is `SafetyCountryConfig.min_safety_officers` for the city's
country. A country with no active config, or with the minimum left blank, is
"not configured": the readiness item fails and the sweep raises the alert.
There is no default minimum and no fallback to another country.

THE SWEEP
---------
`sweep()` runs on the beat. For every ACTIVE city below its minimum it raises
ONE critical `safety_coverage_gap` Command Centre alert (idempotent per city
while the gap lasts); for every open gap alert whose city has recovered or is
no longer live it clears the alert (acknowledged by nobody, with a note), the
same pattern as the threshold sweep. The alert names a city and two counts and
carries no personal data. It is deliberately NOT acknowledgeable: it goes away
when the gap is fixed, and acknowledging it would only hide a live gap.
"""

import logging

from django.utils import timezone

from servers.admin_access.models import ACTIVE, CITY, AdminAssignment, GeoNode
from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action
from servers.command_centre.models import CRITICAL, SAFETY_COVERAGE_GAP, OperatorAlert
from servers.command_centre.services import raise_alert

from .config import active_configs, config_for

logger = logging.getLogger(__name__)

#: The only role template whose assignments count as coverage.
COVERING_ROLE = 'safety_officer'
SOURCE_TYPE = 'safety_coverage'
SWEEP_LABEL = 'system:safety_coverage_sweep'


def country_code(node):
    country = node.country
    return country.code if country is not None else None


def _covering_assignments():
    return (AdminAssignment.objects
            .filter(is_active=True, role__is_active=True, role__code=COVERING_ROLE,
                    admin__is_active=True, admin__is_staff=True, admin__is_break_glass=False)
            .select_related('role', 'scope'))


def officers_covering(node, rows=None):
    """Distinct covering people for `node`. `rows` lets a sweep reuse one query."""
    rows = _covering_assignments() if rows is None else rows
    return len({r.admin_id for r in rows
                if r.role.grants('safety.respond') and r.scope.covers(node)})


def minimum_for(node, configs=None):
    """`(minimum or None, country)`; None means not configured."""
    code = country_code(node)
    config = (configs.get(code) if configs is not None else config_for(code)) if code else None
    return (config.min_safety_officers if config is not None else None), code


def coverage(node, configs=None, rows=None):
    """`{count, minimum, country, ok, configured}` for one city."""
    minimum, code = minimum_for(node, configs)
    count = officers_covering(node, rows)
    return {'count': count, 'minimum': minimum, 'country': code,
            'configured': minimum is not None,
            'ok': minimum is not None and count >= minimum}


def _open_gap_alerts():
    return {a.source_id: a for a in OperatorAlert.objects.filter(
        kind=SAFETY_COVERAGE_GAP, acknowledged_at__isnull=True).select_related('city')}


def _clear(alert, now, why):
    updated = OperatorAlert.objects.filter(pk=alert.pk, acknowledged_at__isnull=True).update(
        acknowledged_at=now, acknowledged_by=None, acknowledged_note=f'cleared: {why}')
    if not updated:
        return False
    with audit_context(actor=None, actor_label=SWEEP_LABEL, reason='safety coverage sweep'):
        record_admin_action(
            None, action='operator_alert_cleared', target_type='operator_alert',
            target_id=alert.id, before={'acknowledged': False, 'severity': alert.severity},
            after={'acknowledged': True, 'cleared_by': SWEEP_LABEL, 'kind': alert.kind,
                   'source_type': alert.source_type, 'source_id': alert.source_id},
            reason=why)
    return True


def sweep(now=None):
    """One pass. Returns `{checked, raised, cleared}`."""
    now = now or timezone.now()
    configs = active_configs()
    rows = list(_covering_assignments())
    open_alerts = _open_gap_alerts()
    summary = {'checked': 0, 'raised': 0, 'cleared': 0}
    gaps = set()

    for city in GeoNode.objects.filter(kind=CITY, status=ACTIVE):
        summary['checked'] += 1
        result = coverage(city, configs, rows)
        if result['ok']:
            continue
        gaps.add(city.code)
        if city.code in open_alerts:
            continue
        reason = ('not configured' if not result['configured']
                  else f"{result['count']} of {result['minimum']}")
        raise_alert(
            SAFETY_COVERAGE_GAP, severity=CRITICAL,
            title=f'{city.name}: no safety cover ({reason})'[:200],
            payload={'city': city.code, 'country': result['country'],
                     'officers': result['count'], 'minimum': result['minimum'],
                     'configured': result['configured']},
            source_type=SOURCE_TYPE, source_id=city.code, city=city)
        summary['raised'] += 1

    for code, alert in open_alerts.items():
        if code not in gaps and _clear(alert, now, 'coverage restored or city no longer live'):
            summary['cleared'] += 1

    if summary['raised'] or summary['cleared']:
        logger.warning('safety coverage sweep: %s', summary)
    return summary
