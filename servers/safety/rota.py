"""The hourly safety rota (owner ruling, 25 Nov): who is on call in a city, hour by hour, every day.

A city's ACTIVE plan is a list of shifts `{weekday 0-6 (Monday 0), start 0-23, end 1-24,
primary: staff user id, backup: staff user id or null}` in the city's own time. It is changed
through the Edit pattern: preview -> reason -> a second person approves (`safety.rota_change`,
always a checker) -> it replaces the plan. The people are staff accounts; their phone numbers come
from their accounts and never appear in the API.

Used by:
* the SOS escalation chain (`oncall.contact_for`): the primary and backup on shift NOW; the city head
  and any hour no shift covers fall back to the city's fixed rota (`OnCallRota`);
* the cover sweep (`sweep_gaps`): a live city with a plan and no primary on shift this hour raises one
  critical `safety_rota_gap` alert, cleared when someone is on shift again.
A city with no plan behaves exactly as before.
"""

from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from base.words import plural
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.service import InvalidRequest, require_approval_or_execute

SAFETY_ROTA_CHANGE = 'safety.rota_change'
KIND = 'safety_rota'
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
GAP_KIND = 'safety_rota_gap'
GAP_SOURCE = 'safety_rota_gap'


def city_tz(node):
    from base.countries import get_country

    from servers.pricing.models import ServiceZone

    zone = ServiceZone.objects.filter(geo_node=node, is_active=True).exclude(timezone_name='').first()
    tz = getattr(zone, 'timezone_name', '') or get_country(node.code.split('-')[0]).default_timezone
    return ZoneInfo(tz)


def active_plan(node):
    from .models import OnCallShiftPlan

    seen = 0
    while node is not None and seen < 8:
        plan = OnCallShiftPlan.objects.filter(city=node, status=OnCallShiftPlan.ACTIVE).first()
        if plan is not None:
            return plan
        node, seen = node.parent, seen + 1
    return None


def shift_at(plan, when):
    local = when.astimezone(city_tz(plan.city))
    for s in plan.shifts or []:
        if s['weekday'] == local.weekday() and s['start'] <= local.hour < s['end']:
            return s
    return None


def eligible_staff(node):
    """Staff who may be rostered here: an active assignment granting safety.respond over the city."""
    from servers.admin_access.models import AdminAssignment

    rows = (AdminAssignment.objects.filter(is_active=True, role__is_active=True, admin__is_active=True,
                                           admin__is_staff=True, admin__is_break_glass=False)
            .select_related('role', 'scope', 'admin'))
    people = {}
    for r in rows:
        if r.role.grants('safety.respond') and r.scope.covers(node):
            people[r.admin_id] = r.admin
    return people


def clean_shifts(node, raw):
    if not isinstance(raw, list):
        raise InvalidRequest('shifts is a list.', code='INVALID_SHIFTS', field='shifts')
    staff = eligible_staff(node)
    out, seen = [], {}
    for s in raw:
        try:
            day, start, end = int(s['weekday']), int(s['start']), int(s['end'])
            primary = int(s['primary'])
            backup = int(s['backup']) if s.get('backup') not in (None, '') else None
        except (KeyError, TypeError, ValueError):
            raise InvalidRequest('Each shift needs a day, a start and end hour and a primary.',
                                 code='INVALID_SHIFTS', field='shifts')
        if not (0 <= day <= 6 and 0 <= start < end <= 24):
            raise InvalidRequest('Hours run 0 to 24 and a shift ends after it starts.', code='INVALID_SHIFTS',
                                 field='shifts')
        for who in (primary, backup):
            if who is not None and who not in staff:
                raise InvalidRequest('Only staff who cover safety in this city can be on the rota.',
                                     code='NOT_ELIGIBLE', field='shifts')
        if backup == primary:
            raise InvalidRequest('The backup must be someone else.', code='INVALID_SHIFTS', field='shifts')
        for h in range(start, end):
            if (day, h) in seen:
                raise InvalidRequest(f'{DAYS[day]} {h}:00 is in two shifts.', code='OVERLAP', field='shifts')
            seen[(day, h)] = True
        out.append({'weekday': day, 'start': start, 'end': end, 'primary': primary, 'backup': backup})
    return sorted(out, key=lambda s: (s['weekday'], s['start']))


def hours_by_day(shifts):
    covered = [0] * 7
    for s in shifts or []:
        covered[s['weekday']] += s['end'] - s['start']
    return covered


def display(node, before, after):
    staff = eligible_staff(node)

    def who(uid):
        u = staff.get(uid)
        return (u.full_name or u.email) if u else 'someone no longer on the rota'

    def day_words(shifts, d):
        parts = [f"{s['start']:02d}–{s['end']:02d} {who(s['primary'])}" for s in shifts if s['weekday'] == d]
        return '; '.join(parts) or 'nobody'
    changes = [{'label': DAYS[d], 'from': day_words(before, d), 'to': day_words(after, d)}
               for d in range(7) if day_words(before, d) != day_words(after, d)]
    a, b = hours_by_day(before), hours_by_day(after)
    rows = [[DAYS[d], f'{a[d]} of 24', f'{b[d]} of 24'] for d in range(7)]
    gaps = sum(24 - h for h in b)
    return {'changes': changes,
            'impact': {'title': 'Hours with someone on call', 'columns': ['Day', 'Today', 'After the change'],
                       'rows': rows, 'note': (f'{plural(gaps, "hour")} a week with nobody on shift: those hours fall back '
                                              "to the city's fixed on-call list, and a cover alert is raised.")
                       if gaps else 'Every hour is covered.'},
            'effective': 'From approval. SOS paging and the cover alerts use it at once.'}


def preview(node, raw):
    plan = active_plan_here(node)
    return display(node, plan.shifts if plan else [], clean_shifts(node, raw))


def active_plan_here(node):
    from .models import OnCallShiftPlan

    return OnCallShiftPlan.objects.filter(city=node, status=OnCallShiftPlan.ACTIVE).first()


def propose(actor, node, raw, reason, http_request=None):
    if len((reason or '').strip()) < 5:
        raise InvalidRequest('Say why the rota changes.', code='REASON_REQUIRED', field='reason')
    shifts = clean_shifts(node, raw)
    plan = active_plan_here(node)
    body = display(node, plan.shifts if plan else [], shifts)
    if not body['changes']:
        raise InvalidRequest('Nothing differs from the rota in force.', code='NO_CHANGE', field='shifts')
    import hashlib
    import json

    digest = hashlib.sha256(json.dumps(shifts, sort_keys=True).encode()).hexdigest()[:16]
    return require_approval_or_execute(
        SAFETY_ROTA_CHANGE, actor=actor, country=node.code.split('-')[0], scope_node=node,
        target_type=KIND, target_id=node.code,
        payload={'kind': KIND, 'city': node.code, 'shifts': shifts, 'base_version': plan.version if plan else 0,
                 'display': body},
        idempotency_key=f'{KIND}:{node.code}:{digest}', reason=reason, http_request=http_request)


@registry.register(SAFETY_ROTA_CHANGE)
def execute(request_row):
    from servers.admin_access.models import GeoNode

    from .models import OnCallShiftPlan

    p = request_row.payload or {}
    node = GeoNode.objects.filter(code=p.get('city')).first()
    if node is None:
        raise registry.ExecutionRefused('UNKNOWN_CITY', 'The city is gone.')
    with transaction.atomic():
        current = OnCallShiftPlan.objects.select_for_update().filter(city=node, status=OnCallShiftPlan.ACTIVE).first()
        if (current.version if current else 0) != p.get('base_version', 0):
            raise registry.ExecutionRefused('ROTA_CHANGED', 'The rota changed after this request was raised; raise it again.')
        if current is not None:
            current.status = OnCallShiftPlan.ARCHIVED
            current.save(update_fields=['status'])
        plan = OnCallShiftPlan.objects.create(
            city=node, version=(current.version if current else 0) + 1, shifts=p.get('shifts') or [],
            status=OnCallShiftPlan.ACTIVE, approval_request_id=request_row.id,
            reason=request_row.request_reason or '', created_by_label=request_row.maker_label or '')
        record_admin_action(None, action='safety_rota_changed', target_type=KIND, target_id=node.code,
                            before={'version': current.version if current else 0},
                            after={'version': plan.version, 'shifts': len(plan.shifts)},
                            reason=request_row.decision_reason or request_row.request_reason)
    return {'city': node.code, 'version': plan.version}


def contact_for(node, role, now=None):
    """(name, phone) for `role` now: the hourly rota's primary or backup on shift, else the fixed rota."""
    from django.contrib.auth import get_user_model

    from .oncall import rota_for

    now = now or timezone.now()
    if role in ('primary', 'backup'):
        plan = active_plan(node)
        shift = shift_at(plan, now) if plan else None
        uid = shift.get(role) if shift else None
        if uid:
            u = get_user_model().objects.filter(pk=uid, is_active=True).first()
            if u is not None and getattr(u, 'phone_number', ''):
                return (u.full_name or u.email or ''), u.phone_number
    rota = rota_for(node)
    return rota.contact(role) if rota else ('', '')


def sweep_gaps(now=None):
    """A live city with an hourly plan and nobody on shift now: one critical alert, cleared when covered."""
    from servers.admin_access.models import ACTIVE, CITY, GeoNode
    from servers.command_centre.models import CRITICAL, OperatorAlert
    from servers.command_centre.services import clear_alerts, raise_alert

    from .models import OnCallShiftPlan

    now = now or timezone.now()
    raised = cleared = 0
    open_codes = set(OperatorAlert.objects.filter(kind=GAP_KIND, acknowledged_at__isnull=True)
                     .values_list('source_id', flat=True))
    gaps = set()
    for plan in OnCallShiftPlan.objects.filter(status=OnCallShiftPlan.ACTIVE, city__kind=CITY,
                                               city__status=ACTIVE).select_related('city'):
        if shift_at(plan, now) is not None:
            continue
        gaps.add(plan.city.code)
        if plan.city.code in open_codes:
            continue
        local = now.astimezone(city_tz(plan.city))
        raise_alert(GAP_KIND, severity=CRITICAL,
                    title=f'{plan.city.name}: nobody on the safety rota at {local:%H}:00 {DAYS[local.weekday()]}'[:200],
                    payload={'city': plan.city.code, 'path': '/safety-ops/config'},
                    source_type=GAP_SOURCE, source_id=plan.city.code, city=plan.city)
        raised += 1
    for code in open_codes - gaps:
        cleared += clear_alerts(GAP_SOURCE, code, note='someone is on the safety rota again')
    return {'raised': raised, 'cleared': cleared}


def week_grid(plan):
    """For the console: 7 x 24 of {primary, backup} user ids or null."""
    grid = [[None] * 24 for _ in range(7)]
    for s in (plan.shifts if plan else []):
        for h in range(s['start'], s['end']):
            grid[s['weekday']][h] = {'primary': s['primary'], 'backup': s.get('backup')}
    return grid

