"""Fraud & Risk (module 13): the review queue, the detail, the review actions and the rules.

  GET  /fraud/admin/flags/                         fraud.read    scoped list + facets
  GET  /fraud/admin/flags/<id>/                    fraud.read    detail: evidence, linked accounts, timeline
  POST /fraud/admin/flags/<id>/assign/             fraud.review  {assignee_id?}  (default: yourself)
  POST /fraud/admin/flags/<id>/unassign/           fraud.review
  POST /fraud/admin/flags/<id>/note/               fraud.review  {note}
  POST /fraud/admin/flags/<id>/confirm/            fraud.review  {reason_code, note}
  POST /fraud/admin/flags/<id>/dismiss/            fraud.review  {reason_code, note}
  POST /fraud/admin/flags/<id>/reopen/             fraud.review  {reason_code, note}
  POST /fraud/admin/flags/<id>/handoff/            fraud.review  {kind}  records a chosen hand-off
  GET  /fraud/admin/flags/<id>/reviewers/          fraud.review  who the flag can be assigned to
  GET  /fraud/admin/rules/                         fraud.read    per-country rules, versions, pending changes
  PUT  /fraud/admin/rules/<country>/<rule>/        fraud.rules   thresholds -> a maker-checker request (202)
  GET  /fraud/admin/options/                       fraud.read    reason codes, kinds, hand-offs

A flag is anchored at its subject's city (else country); out of scope is 404, a missing
capability 403. The subject is a masked reference (`rider #123`), never a name or a contact, and
the evidence is signal names, ids, counts and amounts, never a raw value. Nothing here notifies
the subject, moves money or acts on an account: confirming records a decision and offers links to
actions that already exist, each behind its own gate.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES
from base.utils import error_response, success_response
from servers.admin_access.access import scope_paths
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_access.models import COUNTRY, GeoNode
from servers.admin_access.scoping import ScopeNotHeld, scoped, scoped_to_request
from servers.approvals import service as approvals

from . import config as rule_config
from . import review
from .linked import linked_accounts
from .models import (
    ACTIVE_STATUSES, CONFIRM_REASONS, CONFIRMED, DISMISS_REASONS, DISMISSED, EV_HANDOFF,
    KIND_CHOICES, NOTE_MAX, NOTE_MIN, REOPEN_REASONS, SEVERITY_CHOICES, STATUS_CHOICES, RiskFlag,
)
from .rules import RULES

READ = ScopedPermission('fraud.read')
REVIEW = ScopedPermission('fraud.review')
RULES_CAP = ScopedPermission('fraud.rules')
DEFAULT_LIMIT, MAX_LIMIT = 50, 100

KINDS = {k for k, _ in KIND_CHOICES}
STATUSES = {s for s, _ in STATUS_CHOICES}
SEVERITIES = {s for s, _ in SEVERITY_CHOICES}


def _refuse(exc):
    return error_response(code=exc.code, message=exc.message, field=exc.field,
                          issue=exc.message, status=exc.status)


def _iso(value):
    return value.isoformat() if value else None


def _staff(user):
    if user is None:
        return None
    return {'id': user.pk, 'label': user.full_name or f'staff #{user.pk}'}


def _subject_href(flag):
    subject = flag.subject
    if subject.role == 'driver':
        driver = getattr(subject, 'driver', None)
        return f'/drivers/{driver.pk}' if driver is not None else None
    if subject.role == 'rider':
        return f'/riders/{subject.pk}'
    return None


def flag_dict(flag):
    """The list row. Also what the Promotions tab and the module-12 list have always returned."""
    role = flag.subject.role if flag.subject.role in ('rider', 'driver') else 'user'
    return {
        'id': flag.id, 'kind': flag.kind, 'status': flag.status, 'severity': flag.severity,
        'score': flag.score, 'rule_key': flag.rule_key or flag.kind,
        'rule_version': flag.rule_version, 'hit_count': flag.hit_count,
        'subject': {'ref': f'{role} #{flag.subject_id}', 'role': role,
                    'user_id': flag.subject_id, 'href': _subject_href(flag)},
        'evidence': flag.evidence, 'source_ref': flag.source_ref,
        'scope': flag.anchor_node.code if flag.anchor_node_id else None,
        'scope_name': flag.anchor_node.name if flag.anchor_node_id else None,
        'assigned_to': _staff(flag.assigned_to), 'assigned_at': _iso(flag.assigned_at),
        'decision_reason': flag.decision_reason, 'decision_note': flag.decision_note,
        'decided_by': _staff(flag.decided_by), 'decided_at': _iso(flag.decided_at),
        'created_at': flag.created_at.isoformat(), 'last_seen_at': _iso(flag.last_seen_at),
        'age_hours': int((timezone.now() - flag.created_at).total_seconds() // 3600),
    }


def _base_queryset(request):
    return scoped_to_request(request, RiskFlag.objects.select_related(
        'subject', 'anchor_node', 'assigned_to', 'decided_by'))


def _flag_for(request, flag_id):
    """The flag if it is in scope, else None (a 404 either way)."""
    try:
        return scoped(request.user, RiskFlag.objects.select_related(
            'subject', 'anchor_node', 'assigned_to', 'decided_by')).filter(pk=flag_id).first()
    except ValueError:
        return None


def _not_found(flag_id):
    return error_response(code='NOT_FOUND', message='Flag not found.', field='flag_id',
                          issue=f'No flag {flag_id}', status=status.HTTP_404_NOT_FOUND)


def _bad_filter(field, value):
    return error_response(code='INVALID_FILTER', message=f'Unknown {field}', field=field,
                          issue=str(value), status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def flag_list(request):
    try:
        qs = _base_queryset(request)
    except ScopeNotHeld as exc:
        return error_response(code='SCOPE_NOT_HELD', message=str(exc), field='scope',
                              issue=str(exc), status=status.HTTP_403_FORBIDDEN)
    params = request.query_params
    kind = (params.get('kind') or '').strip()
    if kind:
        if kind not in KINDS:
            return _bad_filter('kind', kind)
        qs = qs.filter(kind=kind)
    assigned = (params.get('assigned') or '').strip()
    if assigned == 'me':
        qs = qs.filter(assigned_to=request.user)
    elif assigned == 'unassigned':
        qs = qs.filter(assigned_to__isnull=True)
    elif assigned:
        try:
            qs = qs.filter(assigned_to_id=int(assigned))
        except ValueError:
            return _bad_filter('assigned', assigned)
    older = (params.get('older_than_hours') or '').strip()
    if older:
        try:
            qs = qs.filter(created_at__lte=timezone.now() - timedelta(hours=int(older)))
        except ValueError:
            return _bad_filter('older_than_hours', older)

    facets = {
        'status': {r['status']: r['n'] for r in qs.values('status').annotate(n=Count('id'))},
        'severity': {r['severity']: r['n'] for r in qs.filter(status__in=ACTIVE_STATUSES)
                     .values('severity').annotate(n=Count('id'))},
    }

    state = (params.get('status') or '').strip()
    if state:
        wanted = [s for s in state.split(',') if s]
        if any(s not in STATUSES and s != 'active' for s in wanted):
            return _bad_filter('status', state)
        expanded = [x for s in wanted for x in (ACTIVE_STATUSES if s == 'active' else (s,))]
        qs = qs.filter(status__in=expanded)
    severity = (params.get('severity') or '').strip()
    if severity:
        wanted = [s for s in severity.split(',') if s]
        if any(s not in SEVERITIES for s in wanted):
            return _bad_filter('severity', severity)
        qs = qs.filter(severity__in=wanted)

    rank = Case(When(severity='high', then=Value(0)), When(severity='medium', then=Value(1)),
                default=Value(2), output_field=IntegerField())
    if (params.get('sort') or '') == 'newest':
        rows_qs = qs.order_by('-created_at', '-id')
    else:
        # Severity first, then the longest waiting. Decided flags sink below the active ones.
        decided_last = Case(When(status__in=ACTIVE_STATUSES, then=Value(0)), default=Value(1),
                            output_field=IntegerField())
        rows_qs = qs.annotate(_rank=rank, _decided=decided_last).order_by(
            '_decided', '_rank', 'created_at', 'id')
    try:
        limit = max(1, min(int(params.get('limit', DEFAULT_LIMIT)), MAX_LIMIT))
        offset = max(0, int(params.get('offset', 0)))
    except (TypeError, ValueError):
        limit, offset = DEFAULT_LIMIT, 0
    total = qs.count()
    return success_response({
        'count': total, 'limit': limit, 'offset': offset, 'facets': facets,
        'may_review': granted(request.user, 'fraud.review')[0],
        'results': [flag_dict(f) for f in rows_qs[offset:offset + limit]],
    }, status.HTTP_200_OK)


def _handoff_options(flag, caller):
    out = []
    for kind, (label, capability, _role, _path) in review.HANDOFFS.items():
        href = review.handoff_target(flag, kind)
        if href is None:
            continue
        taken = [h for h in (flag.handoffs or []) if h.get('kind') == kind]
        out.append({'kind': kind, 'label': label, 'capability': capability, 'href': href,
                    'available': flag.status == CONFIRMED
                    and granted(caller, capability, flag.anchor_node)[0],
                    'taken_at': taken[-1]['at'] if taken else None})
    return out


def _related(flag):
    refs = (flag.evidence or {}).get('refs') or {}
    trips = [int(t) for t in refs.get('trip_ids') or []][-20:]
    campaigns = []
    ids = [int(c) for c in refs.get('campaign_ids') or []]
    if flag.evidence.get('campaign_id'):
        ids.append(int(flag.evidence['campaign_id']))
    if ids:
        from servers.promotions.models import Campaign

        tab = {'referral': '/promotions/referrals', 'driver_incentive': '/promotions/incentives'}
        campaigns = [{'id': c.pk, 'name': c.name, 'kind': c.kind,
                      'href': tab.get(c.kind, '/promotions')}
                     for c in Campaign.objects.filter(pk__in=set(ids))[:10]]
    return {'trips': [{'id': t, 'href': f'/trips/{t}'} for t in trips],
            'trip_count': len(refs.get('trip_ids') or []), 'campaigns': campaigns}


def _timeline(flag):
    out = []
    for e in flag.events.all():
        out.append({'id': e.pk, 'action': e.action, 'from_status': e.from_status,
                    'to_status': e.to_status, 'reason': e.reason, 'note': e.note,
                    'actor': {'id': e.actor_id, 'label': e.actor_label} if e.actor_id else None,
                    'data': e.data, 'at': e.at.isoformat()})
    return out


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def flag_detail(request, flag_id):
    flag = _flag_for(request, flag_id)
    if flag is None:
        return _not_found(flag_id)
    may_review = granted(request.user, 'fraud.review', flag.anchor_node)[0]
    return success_response({
        **flag_dict(flag),
        'may_review': may_review,
        'linked': linked_accounts(flag, request.user),
        'related': _related(flag),
        'timeline': _timeline(flag),
        'handoffs': _handoff_options(flag, request.user),
        'handoffs_taken': flag.handoffs or [],
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------

def _review_write(request, flag_id, action):
    """Shared shell: scope (404), review capability at the flag's place (403), run, answer."""
    flag = _flag_for(request, flag_id)
    if flag is None:
        return _not_found(flag_id)
    if not granted(request.user, 'fraud.review', flag.anchor_node)[0]:
        return error_response(code='PERMISSION_DENIED',
                              message='You do not hold fraud.review here.', field='flag_id',
                              issue='fraud.review', status=status.HTTP_403_FORBIDDEN)
    try:
        action(flag)
    except review.ReviewError as exc:
        return _refuse(exc)
    fresh = _flag_for(request, flag_id)
    return success_response({**flag_dict(fresh), 'timeline': _timeline(fresh),
                             'handoffs': _handoff_options(fresh, request.user),
                             'handoffs_taken': fresh.handoffs or []}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, REVIEW])
def flag_assign(request, flag_id):
    raw = request.data.get('assignee_id')

    def run(flag):
        if raw in (None, '', 'me'):
            assignee = request.user
        else:
            try:
                assignee = get_user_model().objects.filter(pk=int(raw)).first()
            except (TypeError, ValueError):
                assignee = None
        review.assign(flag.pk, request.user, assignee, request)

    return _review_write(request, flag_id, run)


@api_view(['POST'])
@permission_classes([IsAuthenticated, REVIEW])
def flag_unassign(request, flag_id):
    return _review_write(request, flag_id,
                         lambda flag: review.unassign(flag.pk, request.user, request))


@api_view(['POST'])
@permission_classes([IsAuthenticated, REVIEW])
def flag_note(request, flag_id):
    return _review_write(request, flag_id, lambda flag: review.add_note(
        flag.pk, request.user, request.data.get('note'), request))


@api_view(['POST'])
@permission_classes([IsAuthenticated, REVIEW])
def flag_confirm(request, flag_id):
    return _review_write(request, flag_id, lambda flag: review.decide(
        flag.pk, request.user, CONFIRMED, (request.data.get('reason_code') or '').strip(),
        request.data.get('note'), request))


@api_view(['POST'])
@permission_classes([IsAuthenticated, REVIEW])
def flag_dismiss(request, flag_id):
    return _review_write(request, flag_id, lambda flag: review.decide(
        flag.pk, request.user, DISMISSED, (request.data.get('reason_code') or '').strip(),
        request.data.get('note'), request))


@api_view(['POST'])
@permission_classes([IsAuthenticated, REVIEW])
def flag_reopen(request, flag_id):
    return _review_write(request, flag_id, lambda flag: review.reopen(
        flag.pk, request.user, (request.data.get('reason_code') or '').strip(),
        request.data.get('note'), request))


@api_view(['POST'])
@permission_classes([IsAuthenticated, REVIEW])
def flag_handoff(request, flag_id):
    return _review_write(request, flag_id, lambda flag: review.record_handoff(
        flag.pk, request.user, (request.data.get('kind') or '').strip(), request))


@api_view(['GET'])
@permission_classes([IsAuthenticated, REVIEW])
def flag_reviewers(request, flag_id):
    """Who this flag can be assigned to: operators holding fraud.review who can see it."""
    from servers.admin_access.models import AdminAssignment, AdminRole

    flag = _flag_for(request, flag_id)
    if flag is None:
        return _not_found(flag_id)
    role_ids = [r.pk for r in AdminRole.objects.filter(is_active=True)
                if 'fraud.review' in (r.permissions or [])]
    users = {a.admin_id: a.admin for a in AdminAssignment.objects.filter(
        is_active=True, role_id__in=role_ids).select_related('admin')}
    users[request.user.pk] = request.user
    out = [{'id': u.pk, 'label': u.full_name or f'staff #{u.pk}', 'me': u.pk == request.user.pk}
           for u in sorted(users.values(), key=lambda u: u.pk)
           if review.assignee_may_review(u, flag)]
    return success_response({'results': out}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Options and rules
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def options(request):
    return success_response({
        'kinds': [{'key': k, 'label': v} for k, v in KIND_CHOICES],
        'severities': [{'key': k, 'label': v} for k, v in SEVERITY_CHOICES],
        'statuses': [{'key': k, 'label': v} for k, v in STATUS_CHOICES],
        'confirm_reasons': [{'key': k, 'label': v} for k, v in CONFIRM_REASONS.items()],
        'dismiss_reasons': [{'key': k, 'label': v} for k, v in DISMISS_REASONS.items()],
        'reopen_reasons': [{'key': k, 'label': v} for k, v in REOPEN_REASONS.items()],
        'note_min': NOTE_MIN, 'note_max': NOTE_MAX,
        'handoffs': [{'kind': k, 'label': v[0], 'capability': v[1]}
                     for k, v in review.HANDOFFS.items()],
        'may_review': granted(request.user, 'fraud.review')[0],
        'may_change_rules': granted(request.user, 'fraud.rules')[0],
    }, status.HTTP_200_OK)


def _country_visible(user, node):
    paths = scope_paths(user)
    if paths is None:
        return True
    return any(node.path.startswith(p) or p.startswith(node.path) for p in paths)


def _param_row(rule, param, config):
    value = rule_config.thresholds_of(rule, config).get(param.key)
    return {'key': param.key, 'label': param.label, 'unit': param.unit, 'value': value,
            'default': rule.defaults(config.country)[param.key], 'low': param.low,
            'high': param.high, 'hint': param.hint}


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def rules_list(request):
    from .models import RiskRuleConfig

    since = timezone.now() - timedelta(days=7)
    visible_flags = scoped(request.user, RiskFlag.objects.filter(created_at__gte=since))
    out, pending_all = [], rule_config.pending_requests()
    for code in sorted(COUNTRIES):
        node = GeoNode.objects.filter(kind=COUNTRY, code=code).first()
        configs = {c.rule_key: c for c in RiskRuleConfig.objects.filter(country=code,
                                                                         is_active=True)}
        if node is None or not _country_visible(request.user, node) or not (
                configs or code in ('IN', 'US')):
            continue
        counts = {r['rule_key']: r['n'] for r in visible_flags.filter(
            anchor_node__path__startswith=node.path).values('rule_key').annotate(n=Count('id'))}
        rules = []
        for key, rule in RULES.items():
            config = configs.get(key)
            row = {'rule_key': key, 'title': rule.title, 'summary': rule.summary,
                   'data_used': rule.data_used, 'data_missing': rule.data_missing,
                   'subject_role': rule.subject_role,
                   'mode': 'scheduled' if rule.params else 'event',
                   'flags_7d': counts.get(key, 0)}
            if rule.params:
                row.update(
                    configured=config is not None, enabled=bool(config and config.enabled),
                    version=config.version if config else 0,
                    last_run_at=_iso(config.last_run_at) if config else None,
                    thresholds=[_param_row(rule, p, config) for p in rule.params]
                    if config else [],
                    pending=[p for p in pending_all
                             if p['country'] == code and p['rule_key'] == key])
            else:
                row.update(configured=True, enabled=True, version=1, last_run_at=None,
                           thresholds=[], pending=[])
            rules.append(row)
        out.append({'country': code, 'country_name': COUNTRIES[code].name,
                    'may_edit': granted(request.user, 'fraud.rules', node)[0], 'rules': rules})
    return success_response({'results': out}, status.HTTP_200_OK)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, RULES_CAP])
def rule_update(request, country, rule_key):
    """Does NOT change the rule: raises a `fraud.rule_change` request (always a checker, 202)."""
    try:
        outcome, changed = rule_config.propose_change(
            request.user, country=country, rule_key=rule_key,
            thresholds=request.data.get('thresholds'), enabled=request.data.get('enabled'),
            reason=request.data.get('reason'), http_request=request)
    except rule_config.ConfigError as exc:
        code = {'SCOPE_NOT_HELD': status.HTTP_403_FORBIDDEN,
                'UNKNOWN_RULE': status.HTTP_404_NOT_FOUND,
                'UNKNOWN_COUNTRY': status.HTTP_404_NOT_FOUND}.get(exc.code,
                                                                   status.HTTP_400_BAD_REQUEST)
        return error_response(code=exc.code, message=exc.message, field=exc.field,
                              issue=exc.message, status=code)
    except approvals.ApprovalError as exc:
        return exc.response()
    if not changed:
        return success_response({'changed': False}, status.HTTP_200_OK)
    row = outcome.request
    body = {'approval_request': row.as_dict(), 'duplicate': outcome.duplicate}
    if row.status == 'requested':
        body['state'] = 'pending_approval'
        return success_response(body, status.HTTP_202_ACCEPTED)
    body['state'] = row.status
    body['result'] = row.result
    return success_response(body, status.HTTP_200_OK)
