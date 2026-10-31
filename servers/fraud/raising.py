"""Turning a rule's hit into a flag, and the alerts that follow.

IDEMPOTENT BY CONSTRUCTION
--------------------------
A hit names a subject and the ids that support it (`refs`: trip ids, campaign ids, counterpart
user ids). `raise_hit` then does exactly one of:

* no active flag for (rule, subject): create one (`created`), unless a DECIDED flag for the same
  rule and subject already covers every one of these ids (`skipped`: a dismissed flag does not
  come back for the same evidence);
* an active flag exists and the hit adds a new id, or a higher score or severity: append to it
  (`appended`: hit count, last seen, merged refs, a timeline line);
* otherwise change nothing (`unchanged`).

So the beat task running twice over the same data writes nothing the second time. The database
allows one active flag per `dedupe_key`, so two workers racing converge on one row.

NOTHING IS SENT TO THE SUBJECT
------------------------------
A flag, and every line after it, is internal. The only outputs are the flag, its timeline and a
Command Centre alert for the operators (a count and a city, never a person).
"""

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from servers.command_centre.models import FRAUD_FLAG_HIGH, FRAUD_QUEUE_AGING, INFO, WARNING
from servers.command_centre.services import acknowledge, raise_alert

from .models import (
    ACTIVE_STATUSES, DECIDED_STATUSES, EV_HIT, EV_RAISED, HIGH, SEVERITY_RANK, FlagEvent, RiskFlag,
)

logger = logging.getLogger(__name__)

MAX_REFS = 100
#: One `fraud.flag_high` alert per city per this long, however many high flags a rule storm makes.
ALERT_WINDOW_MINUTES = 30
#: `fraud.queue_aging`: open flags older than this, at least this many in one city.
AGING_HOURS = 48
AGING_MIN_COUNT = 5

CREATED, APPENDED, UNCHANGED, SKIPPED = 'created', 'appended', 'unchanged', 'skipped'


def _ref_set(refs):
    return {(k, int(v)) for k, values in (refs or {}).items() for v in (values or [])}


def _merge_refs(old, new):
    merged = {}
    for key in {*old, *new}:
        values = sorted({int(v) for v in (old.get(key) or [])} | {int(v) for v in (new.get(key) or [])})
        merged[key] = values[-MAX_REFS:]
    return merged


def _anchor(subject):
    from servers.notifications import recipients

    return recipients.city_of(subject)


def raise_hit(rule, config, hit, now=None):
    """Create or extend the flag for `hit`. Returns `(flag_or_None, outcome)`."""
    now = now or timezone.now()
    key = f'{rule.key}:{hit.subject.pk}'
    new_refs = _ref_set(hit.refs)
    for _attempt in range(2):
        try:
            with transaction.atomic():
                flag = (RiskFlag.objects.select_for_update()
                        .filter(dedupe_key=key, status__in=ACTIVE_STATUSES).first())
                if flag is not None:
                    return flag, _append(flag, hit, new_refs, now)
                if _already_decided(rule.key, hit.subject, new_refs):
                    return None, SKIPPED
                return _create(rule, config, hit, key, now), CREATED
        except IntegrityError:
            continue                          # lost the race to another worker: append instead
    return None, UNCHANGED


def _already_decided(rule_key, subject, new_refs):
    decided = (RiskFlag.objects.filter(subject=subject, rule_key=rule_key,
                                       status__in=DECIDED_STATUSES).order_by('-id').first())
    if decided is None:
        return False
    return new_refs <= _ref_set((decided.evidence or {}).get('refs'))


def _evidence(hit, now, count, first_seen=None):
    return {
        'signals': list(hit.signals), 'refs': _merge_refs({}, hit.refs),
        'measures': dict(hit.measures), 'hit_count': count,
        'first_seen': first_seen or now.isoformat(), 'last_seen': now.isoformat(),
    }


def hold_rewards(flag, now=None):
    """Ruling (a): hold the unpaid reward linked to this ACTIVE flag. Never lets a hold problem
    stop a flag from being raised: a failure is logged and the flag stands."""
    from . import money

    try:
        with transaction.atomic():
            return money.hold_for_flag(flag, now)
    except Exception:  # noqa: BLE001
        logger.exception('reward hold failed for flag %s', flag.pk)
        return []


def _create(rule, config, hit, key, now):
    flag = RiskFlag.objects.create(
        subject=hit.subject, kind=rule.key, rule_key=rule.key,
        rule_version=config.version if config is not None else 0, severity=hit.severity,
        score=hit.score, dedupe_key=key, anchor_node=_anchor(hit.subject),
        evidence=_evidence(hit, now, 1), last_seen_at=now)
    FlagEvent.objects.create(flag=flag, action=EV_RAISED, to_status=flag.status,
                             data={'rule': rule.key, 'rule_version': flag.rule_version,
                                   'score': hit.score, 'severity': hit.severity}, at=now)
    if flag.severity == HIGH:
        transaction.on_commit(lambda: alert_high(flag.pk))
    hold_rewards(flag, now)
    return flag


def _append(flag, hit, new_refs, now):
    evidence = dict(flag.evidence or {})
    old_refs = _ref_set(evidence.get('refs'))
    added = new_refs - old_refs
    higher = (hit.score > flag.score
              or SEVERITY_RANK[hit.severity] < SEVERITY_RANK.get(flag.severity, 9))
    if not added and not higher:
        return UNCHANGED
    was_high = flag.severity == HIGH
    evidence['refs'] = _merge_refs(evidence.get('refs') or {}, hit.refs)
    evidence['signals'] = sorted(set(evidence.get('signals') or []) | set(hit.signals))
    evidence['measures'] = dict(hit.measures)
    evidence['hit_count'] = int(evidence.get('hit_count') or flag.hit_count) + 1
    evidence['last_seen'] = now.isoformat()
    flag.evidence = evidence
    flag.hit_count = evidence['hit_count']
    flag.last_seen_at = now
    flag.score = max(flag.score, hit.score)
    if SEVERITY_RANK[hit.severity] < SEVERITY_RANK.get(flag.severity, 9):
        flag.severity = hit.severity
    flag.save(update_fields=['evidence', 'hit_count', 'last_seen_at', 'score', 'severity',
                             'updated_at'])
    FlagEvent.objects.create(flag=flag, action=EV_HIT, from_status=flag.status,
                             to_status=flag.status,
                             data={'new_refs': len(added), 'score': flag.score,
                                   'severity': flag.severity}, at=now)
    if flag.severity == HIGH and not was_high:
        transaction.on_commit(lambda: alert_high(flag.pk))
    hold_rewards(flag, now)
    return APPENDED


# ---------------------------------------------------------------------------
# Alerts (Command Centre)
# ---------------------------------------------------------------------------

def alert_high(flag_id, now=None):
    """`fraud.flag_high`: at most one per city per `ALERT_WINDOW_MINUTES`.

    A rule storm that raises twenty high flags in a city makes one alert; the payload's count is
    kept up to date on the alert that is still open. The alert says how many and where, not who.
    """
    from servers.command_centre.models import OperatorAlert

    now = now or timezone.now()
    flag = RiskFlag.objects.select_related('anchor_node').filter(pk=flag_id).first()
    if flag is None or flag.severity != HIGH:
        return None
    city = flag.anchor_node
    recent = OperatorAlert.objects.filter(
        kind=FRAUD_FLAG_HIGH, city=city, created_at__gte=now - timedelta(minutes=ALERT_WINDOW_MINUTES))
    latest = recent.order_by('-id').first()
    if latest is not None:
        if latest.acknowledged_at is None:
            payload = dict(latest.payload or {})
            payload['new_high_flags'] = int(payload.get('new_high_flags') or 1) + 1
            payload['latest_flag_id'] = flag.pk
            latest.payload = payload
            latest.save(update_fields=['payload'])
        return latest
    where = f' in {city.name}' if city is not None else ''
    return raise_alert(
        FRAUD_FLAG_HIGH, title=f'High-severity fraud flag{where}', severity=WARNING,
        payload={'new_high_flags': 1, 'latest_flag_id': flag.pk, 'rule_key': flag.rule_key,
                 'path': f'/fraud/{flag.pk}'},
        source_type='fraud_flag_high', source_id=f'{city.pk if city else 0}:{flag.pk}',
        city=city)


def check_queue_aging(now=None):
    """`fraud.queue_aging` (info): a city with `AGING_MIN_COUNT` or more active flags older than
    `AGING_HOURS`. One alert per city while it stays true; it clears itself when the count drops."""
    from django.db.models import Count

    from servers.command_centre.models import OperatorAlert

    now = now or timezone.now()
    rows = (RiskFlag.objects.filter(status__in=ACTIVE_STATUSES,
                                    created_at__lte=now - timedelta(hours=AGING_HOURS))
            .values('anchor_node_id').annotate(n=Count('id')))
    over = {r['anchor_node_id']: r['n'] for r in rows if r['n'] >= AGING_MIN_COUNT}
    raised = 0
    from servers.admin_access.models import GeoNode

    for node_id, count in over.items():
        node = GeoNode.objects.filter(pk=node_id).first() if node_id else None
        source_id = f'aging:{node_id or 0}'
        existing = OperatorAlert.objects.filter(source_type='fraud_queue_aging',
                                                source_id=source_id,
                                                acknowledged_at__isnull=True).first()
        if existing is not None:
            if (existing.payload or {}).get('aged_flags') != count:
                existing.payload = {**(existing.payload or {}), 'aged_flags': count}
                existing.save(update_fields=['payload'])
            continue
        where = f' in {node.name}' if node is not None else ''
        raise_alert(
            FRAUD_QUEUE_AGING, title=f'{count} fraud flags waiting over {AGING_HOURS} hours{where}',
            severity=INFO, payload={'aged_flags': count, 'hours': AGING_HOURS,
                                    'path': '/fraud?status=active&older_than_hours=48'},
            source_type='fraud_queue_aging', source_id=source_id, city=node)
        raised += 1
    for alert in OperatorAlert.objects.filter(source_type='fraud_queue_aging',
                                              acknowledged_at__isnull=True):
        node_id = int(alert.source_id.split(':')[1]) or None
        if node_id not in over:
            acknowledge(alert, by=None, note='Cleared: the queue is back under the line.')
    return raised
