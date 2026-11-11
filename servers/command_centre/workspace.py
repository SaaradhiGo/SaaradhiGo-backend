"""The console's workspaces: one row per place, what needs a person, usual ranges.

Owner, 2026-11-03: the console must work for 100+ cities in several countries.
Three reads replace the board-per-city Command Centre:

PLACES (`places`)
-----------------
One row per CHILD of the place being looked at -- Global's countries, a
country's states, a state's cities -- each the sum of its visible cities'
five-minute rollups (`rollups.totals`, a handful of queries however many
cities) plus a few live counts (drivers online, active SOS, open alerts, KYC
waiting, stuck trips), one grouped query each. Rows come sorted worst first;
places with no activity fold into one "quiet" line. Every city also comes back
as a small health square for the Global view's map of the platform.

NEEDS ACTION (`needs_action`)
-----------------------------
One ranked list across everything the caller may act on: SOS, stuck trips,
approvals waiting for them, KYC waiting, failed payouts, open alerts. Each kind
appears only for a caller holding its capability, within their scope, and each
item opens the exact record. More than three of a kind in one place become one
grouped item ("6 trips stuck in Dallas") that opens the filtered list.

USUAL RANGE (`usual_range`)
---------------------------
The same weekday and hour over the last four weeks, from the rollups. Shown
only when at least three of those weeks have data; otherwise nothing is shown,
never a guess.

Permissions and scope are the existing ones, asked here exactly as the lists
ask them; this module adds no access of its own.
"""

import logging
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.db.models import Count, Min, Q
from django.utils import timezone

from base import refs
from servers.admin_access.bridge import granted
from servers.admin_access.models import ACTIVE, CITY, COUNTRY, GLOBAL, STATE, GeoNode

from . import rollups
from .overview import city_contexts

logger = logging.getLogger(__name__)

WEEK = timedelta(days=7)
CRITICAL, HIGH, WAITING = 'critical', 'high', 'waiting'
SEVERITY_RANK = {CRITICAL: 0, HIGH: 1, WAITING: 2}
#: Within a severity, the owner's order (2026-11-03).
KIND_RANK = {'sos': 0, 'stuck_trip': 1, 'approval': 2, 'payout_failed': 3, 'kyc': 4, 'alert': 5}
GROUP_OVER = 3
#: A rate is shown only over at least this many requests (console "Not enough data yet").
MIN_REQUESTS = 20


# ---------------------------------------------------------------------------
# Windows per city
# ---------------------------------------------------------------------------

def _today(now, tz):
    local = now.astimezone(tz)
    return datetime.combine(local.date(), time.min, tzinfo=tz).astimezone(dt_timezone.utc)


def windows_for(contexts, window, now):
    """{city_id: (start, end)} for `window` (today | 24h | 7d) and the same
    window a week earlier."""
    current, previous = {}, {}
    for city_id, ctx in contexts.items():
        if window == 'today':
            start = _today(now, ctx.tz)
        elif window == '24h':
            start = now - timedelta(days=1)
        else:
            start = now - WEEK
        current[city_id] = (start, now)
        previous[city_id] = (start - WEEK, now - WEEK)
    return current, previous


# ---------------------------------------------------------------------------
# Live counts, one grouped query each
# ---------------------------------------------------------------------------

def live_counts(cities, user):
    """{city_id: {...}} of what is happening now."""
    from servers.driver.models import Driver
    from servers.ride.models import Trip
    from servers.ride.stuck import stuck_trips
    from servers.sos.models import SOSEvent

    from .models import OperatorAlert

    ids = [c.id for c in cities]
    out = {cid: {'drivers_online': 0, 'sos_active': 0, 'sos_unacknowledged': 0,
                 'alerts_critical': 0, 'alerts_warning': 0, 'kyc_waiting': 0, 'stuck_trips': 0}
           for cid in ids}

    # The Drivers list's own "Online now" (its status column), so the overview and
    # the list never disagree about the same drivers.
    from servers.driver.admin_drivers import ONLINE_STATUSES

    drivers = Driver.objects.filter(home_city_id__in=ids, status__in=ONLINE_STATUSES)
    for row in drivers.values('home_city_id').annotate(n=Count('id')):
        out[row['home_city_id']]['drivers_online'] = row['n']
    for row in (Driver.objects.filter(home_city_id__in=ids, approved=False)
                .values('home_city_id').annotate(n=Count('id'))):
        out[row['home_city_id']]['kyc_waiting'] = row['n']

    for anchor, kind, trip_city, st in (SOSEvent.objects.filter(status__in=('open', 'acknowledged'))
                                        .values_list('anchor_node_id', 'anchor_node__kind',
                                                     'trip__zone__geo_node_id', 'status')):
        city = anchor if kind == 'city' else trip_city
        if city in out:
            out[city]['sos_active'] += 1
            if st == 'open':
                out[city]['sos_unacknowledged'] += 1

    for row in (OperatorAlert.objects.filter(acknowledged_at__isnull=True, city_id__in=ids)
                .values('city_id', 'severity').annotate(n=Count('id'))):
        key = 'alerts_critical' if row['severity'] == 'critical' else 'alerts_warning'
        out[row['city_id']][key] += row['n']

    if granted(user, 'trip.read')[0]:
        for trip, _reason, _minutes in stuck_trips(Trip.objects.filter(zone__geo_node_id__in=ids)):
            out[trip.zone.geo_node_id]['stuck_trips'] += 1
    return out


def health(figs, live):
    """critical | watch | normal | quiet, with the reason a person would give."""
    if live['sos_unacknowledged']:
        return 'critical', f"{live['sos_unacknowledged']} SOS not acknowledged"
    if live['alerts_critical']:
        return 'critical', f"{live['alerts_critical']} critical alert{'s' if live['alerts_critical'] > 1 else ''}"
    if live['alerts_warning'] or live['stuck_trips']:
        parts = []
        if live['alerts_warning']:
            parts.append(f"{live['alerts_warning']} alert{'s' if live['alerts_warning'] > 1 else ''}")
        if live['stuck_trips']:
            parts.append(f"{live['stuck_trips']} stuck trip{'s' if live['stuck_trips'] > 1 else ''}")
        return 'watch', ', '.join(parts)
    if not figs['requests'] and not live['drivers_online'] and not live['sos_active']:
        return 'quiet', 'No activity'
    return 'normal', ''


HEALTH_RANK = {'critical': 0, 'watch': 1, 'normal': 2, 'quiet': 3}


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------

def _child_of(node, city):
    """The child of `node` that contains `city` (or the city itself)."""
    parts = [p for p in str(city.path).split('/') if p]
    depth = {GLOBAL: 1, COUNTRY: 2, STATE: 3}.get(node.kind)
    if depth is None or len(parts) <= depth:
        return None
    return parts[depth]


def _public_figs(figs, include_money):
    out = dict(figs)
    if not include_money:
        out.pop('collected_minor', None)
    if (out.get('requests') or 0) < MIN_REQUESTS:
        # A rate over a handful of requests reads as a fact and is not one.
        out['rates_reliable'] = False
    else:
        out['rates_reliable'] = True
    return out


def places(user, cities, node, window='today', now=None, include_money=False):
    """The place table for `node` (None = Global) over the visible `cities`."""
    now = now or timezone.now()
    if node is None:
        node = home_node(cities)
    cities = [c for c in cities if node is None or str(c.path).startswith(node.path)]
    contexts = city_contexts(cities)
    current, previous = windows_for(contexts, window, now)
    now_t, before_t = rollups.totals(current), rollups.totals(previous)
    live = live_counts(cities, user)

    per_city = {}
    for city in cities:
        figs = rollups.figures(now_t[city.id])
        h, why = health(figs, live[city.id])
        per_city[city.id] = {
            'code': city.code, 'name': city.name, 'status': city.status,
            'timezone': str(contexts[city.id].tz), 'currency': contexts[city.id].currency,
            'health': h, 'health_reason': why,
        }

    groups = defaultdict(list)
    for city in cities:
        child = _child_of(node, city) if node.kind != CITY else None
        if child:
            groups[child].append(city)
    names = {n.code: n for n in GeoNode.objects.filter(code__in=list(groups))}

    rows, quiet = [], []
    for code, members in groups.items():
        t_now, t_before = rollups.empty(), rollups.empty()
        live_sum = defaultdict(int)
        for c in members:
            t_now = rollups.add(t_now, now_t[c.id])
            t_before = rollups.add(t_before, before_t[c.id])
            for k, v in live[c.id].items():
                live_sum[k] += v
        figs, last = rollups.figures(t_now), rollups.figures(t_before)
        h, why = health(figs, live_sum)
        worst = sorted(members, key=lambda c: (HEALTH_RANK[per_city[c.id]['health']],
                                               -(now_t[c.id]['unfulfilled'])))[0]
        child_node = names.get(code)
        row = {
            'code': code,
            'name': child_node.name if child_node else code,
            'kind': child_node.kind if child_node else None,
            'status': child_node.status if child_node else None,
            'live_cities': sum(1 for c in members if c.status == ACTIVE),
            'cities': len(members),
            **_public_figs(figs, include_money),
            'last_week': {'requests': last['requests'], 'unfulfilled_rate': last['unfulfilled_rate']},
            **dict(live_sum),
            'health': h, 'health_reason': why,
            'worst_city': ({'code': worst.code, 'name': worst.name,
                            'health': per_city[worst.id]['health'],
                            'reason': per_city[worst.id]['health_reason']}
                           if child_node is None or child_node.kind != CITY else None),
        }
        (quiet if h == 'quiet' else rows).append(row)
    rows.sort(key=lambda r: (HEALTH_RANK[r['health']], -(r['sos_unacknowledged'] or 0),
                             -(r['unfulfilled_rate'] or 0) if r.get('rates_reliable') else 0,
                             -r['requests']))
    quiet.sort(key=lambda r: r['name'])

    total = rollups.empty()
    total_live = defaultdict(int)
    for c in cities:
        total = rollups.add(total, now_t[c.id])
        for k, v in live[c.id].items():
            total_live[k] += v
    total_figs = rollups.figures(total)
    counts = defaultdict(int)
    for c in per_city.values():
        counts[c['health']] += 1

    return {
        'node': {'code': node.code, 'name': node.name, 'kind': node.kind} if node else None,
        'breadcrumb': _breadcrumb(node),
        'window': window,
        'generated_at': now.isoformat(),
        'totals': {**_public_figs(total_figs, include_money), **dict(total_live),
                   'cities': len(cities),
                   'live_cities': sum(1 for c in cities if c.status == ACTIVE),
                   'paused_cities': sum(1 for c in cities if c.status != ACTIVE)},
        'health_counts': dict(counts),
        'children': rows,
        'quiet': quiet,
        'cities': [{**per_city[c.id], 'country': _country_code(c)}
                   for c in sorted(cities, key=lambda c: (HEALTH_RANK[per_city[c.id]['health']], c.name))],
    }


def home_node(cities):
    """Where a caller lands: the narrowest place covering every city they may
    see. A Super Admin lands on Global, a country lead on their country, City
    Ops on their city (owner, 2026-11-03)."""
    paths = [[p for p in str(c.path).split('/') if p] for c in cities]
    if not paths:
        return GeoNode.objects.filter(kind=GLOBAL).first()
    common = []
    for parts in zip(*paths, strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    code = common[-1] if common else None
    node = GeoNode.objects.filter(code=code).first() if code else None
    return node or GeoNode.objects.filter(kind=GLOBAL).first()


def _country_code(city):
    parts = [p for p in str(city.path).split('/') if p and p != 'GLOBAL']
    return parts[0] if parts else None


def _breadcrumb(node):
    out = []
    while node is not None:
        out.append({'code': node.code, 'name': node.name, 'kind': node.kind})
        node = node.parent
    return list(reversed(out))


# ---------------------------------------------------------------------------
# Usual range
# ---------------------------------------------------------------------------

def usual_range(city_ids, now=None, hours=1):
    """Requests, fulfilment and ETA in the last `hours` hours, and their usual
    range: the same weekday and hours in each of the last four weeks.

    A usual range needs data in at least three of the four weeks; otherwise
    it is None and the console shows nothing.
    """
    now = now or timezone.now()
    span = timedelta(hours=hours)
    windows = {i: (now - i * WEEK - span, now - i * WEEK) for i in range(0, 5)}

    def measure(bounds):
        t = rollups.totals({cid: bounds for cid in city_ids})
        total = rollups.empty()
        for v in t.values():
            total = rollups.add(total, v)
        return rollups.figures(total)

    current = measure(windows[0])
    history = [measure(windows[i]) for i in range(1, 5)]
    usable = [h for h in history if h['requests']]

    def band(key):
        values = [h[key] for h in usable if h[key] is not None]
        if len(values) < 3:
            return None
        return {'low': min(values), 'high': max(values), 'weeks': len(values)}

    return {
        'window_hours': hours,
        'now': current,
        'usual': {k: band(k) for k in ('requests', 'fulfilment_rate', 'eta_minutes')},
    }


def sparkline(city_ids, tz, now=None):
    """Hourly requests and completed trips since the city's midnight."""
    now = now or timezone.now()
    start = _today(now, tz)
    return [{'hour': hour.isoformat(), 'requests': t['requested'], 'completed': t['completed']}
            for hour, t in rollups.hourly(city_ids, start, now)]


# ---------------------------------------------------------------------------
# Needs action
# ---------------------------------------------------------------------------

def _age_seconds(stamp, now):
    return int((now - stamp).total_seconds()) if stamp else 0


def _item(kind, severity, title, *, where=None, at=None, now=None, link, ref=None, owner=None,
          count=1, detail='', target_seconds=None, actions=()):
    age = _age_seconds(at, now) if at else 0
    return {
        'kind': kind, 'severity': severity, 'title': title, 'detail': detail,
        'ref': ref, 'where': where, 'age_seconds': age, 'count': count, 'link': link,
        'owner': owner,
        'time_left_seconds': (target_seconds - age) if target_seconds else None,
        'actions': list(actions),
    }


def _place(node):
    return {'code': node.code, 'name': node.name} if node is not None else None


def needs_action(request, limit=50, now=None):
    """The ranked list for the caller, within `?scope=`."""
    from servers.admin_access.scoping import requested_node, scoped
    from servers.ride.models import Trip
    from servers.ride.stuck import WORDS, stuck_trips

    now = now or timezone.now()
    user = request.user
    node = requested_node(request)
    items = []

    # ---- SOS: every active case, critical until someone acknowledges it ----
    from .views import sees_live_sos

    if sees_live_sos(user):
        from servers.safety.admin_views import _annotate
        from servers.safety.config import active_configs
        from servers.safety.models import CLOSED, SafetyIncident
        from servers.safety.scope import incidents_visible_to

        configs = active_configs()
        cases = (_annotate(incidents_visible_to(user, node, SafetyIncident.objects.all()))
                 .exclude(status=CLOSED).filter(active_sos__gt=0)
                 .select_related('anchor_node', 'assigned_to')[:200])
        for case in cases:
            case._has_sos = True
            ref = refs.incident_ref(case)
            unacked = case.unack_sos > 0
            cfg = configs.get(case.country or '')
            target = (cfg.sos_ack_minutes * 60) if (cfg and cfg.sos_ack_minutes and unacked) else None
            who = getattr(case.assigned_to, 'full_name', None) or getattr(case.assigned_to, 'email', None)
            items.append(_item(
                'sos', CRITICAL if unacked else HIGH,
                f"{ref} · {'not acknowledged' if unacked else 'being handled'}"
                + (f' · {refs.ref(refs.TRIP, case.trip_id)}' if case.trip_id else ''),
                where=_place(case.anchor_node), at=case.oldest_active or case.opened_at, now=now,
                link=f'/safety-ops/incidents/{case.id}', ref=ref,
                owner={'id': case.assigned_to_id, 'label': who} if case.assigned_to_id else None,
                target_seconds=target,
                detail='Rider reports false alarm' if getattr(case, 'reported_mistake', 0) else '',
                actions=['acknowledge', 'assign_me'] if unacked else ['assign_me']))

    # ---- Stuck trips --------------------------------------------------------
    if granted(user, 'trip.read')[0]:
        trips = scoped(user, Trip.objects.all())
        if node is not None:
            trips = trips.filter(zone__geo_node__path__startswith=node.path)
        by_city = defaultdict(list)
        for trip, reason, minutes in stuck_trips(trips, now):
            by_city[trip.zone.geo_node].append((trip, reason, minutes))
        for city, rows in by_city.items():
            if len(rows) > GROUP_OVER:
                oldest = max(m for _t, _r, m in rows)
                items.append(_item(
                    'stuck_trip', HIGH, f'{len(rows)} trips stuck in {city.name}',
                    where=_place(city), at=now - timedelta(minutes=oldest), now=now,
                    link=f'/trips?stuck=1&scope={city.code}', count=len(rows)))
                continue
            for trip, reason, minutes in rows:
                ref = refs.ref(refs.TRIP, trip.id)
                items.append(_item(
                    'stuck_trip', HIGH, f'{ref} · {WORDS[reason]} for {minutes} min',
                    where=_place(city), at=now - timedelta(minutes=minutes), now=now,
                    link=f'/trips/{trip.id}', ref=ref))

    # ---- Approvals waiting for this checker ------------------------------------
    if granted(user, 'approval.check')[0]:
        from servers.approvals.models import REQUESTED, ApprovalRequest
        from servers.approvals.scope import visible
        from servers.approvals.service import checker_problem
        from servers.approvals.views import shown

        waiting = (visible(user, ApprovalRequest.objects.select_related('scope_node', 'policy'))
                   .filter(status=REQUESTED).exclude(maker_id=user.pk).order_by('requested_at')[:100])
        mine = [r for r in waiting if checker_problem(r, user) is None]
        if len(mine) > GROUP_OVER:
            items.append(_item('approval', WAITING, f'{len(mine)} requests waiting for your approval',
                               at=mine[0].requested_at, now=now, link='/approvals', count=len(mine)))
        else:
            for r in mine:
                row = shown(r, user)
                blocked = f" · {row['blockers'][0]}" if row['blockers'] else ''
                items.append(_item('approval', WAITING, f"{row['ref']} · {row['title']}{blocked}",
                                   where=_place(r.scope_node), at=r.requested_at, now=now,
                                   link=f'/approvals?id={r.id}', ref=row['ref']))

    # ---- KYC waiting -------------------------------------------------------------
    if granted(user, 'driver.read')[0]:
        from servers.driver.models import Driver

        drivers = scoped(user, Driver.objects.filter(approved=False, doc_status='pending',
                                                     uploaded_timestamp__isnull=False))
        if node is not None:
            drivers = drivers.filter(home_city__path__startswith=node.path)
        day_ago = now - timedelta(hours=24)
        for row in (drivers.values('home_city_id', 'home_city__code', 'home_city__name')
                    .annotate(n=Count('id'), old=Count('id', filter=Q(uploaded_timestamp__lt=day_ago)),
                              oldest=Min('uploaded_timestamp'))
                    .order_by('-n')[:50]):
            if not row['n']:
                continue
            where = {'code': row['home_city__code'], 'name': row['home_city__name']} if row['home_city_id'] else None
            items.append(_item(
                'kyc', WAITING, f"{row['n']} driver{'s' if row['n'] > 1 else ''} waiting for KYC"
                + (f" ({row['old']} over 24 h)" if row['old'] else ''),
                where=where, at=row['oldest'], now=now, count=row['n'],
                link='/kyc' + (f"?scope={row['home_city__code']}" if where else '')))

    # ---- Failed payouts today ----------------------------------------------------
    if granted(user, 'finance.read')[0]:
        from servers.driver.models import WithdrawalRequest

        failed = scoped(user, WithdrawalRequest.objects.filter(
            Q(processed_at__gte=now - timedelta(hours=24)) | Q(requested_at__gte=now - timedelta(hours=24)),
            status='failed'))
        if node is not None:
            failed = failed.filter(driver__home_city__path__startswith=node.path)
        n = failed.count()
        if n:
            items.append(_item('payout_failed', HIGH, f"{n} payout{'s' if n > 1 else ''} failed in the last 24 h",
                               now=now, count=n, link='/finance/payouts?status=failed'))

    # ---- Open alerts ---------------------------------------------------------------
    from .views import _visible_alerts

    alerts = _visible_alerts(user).filter(acknowledged_at__isnull=True)
    if node is not None:
        alerts = alerts.filter(Q(city__path__startswith=node.path) | Q(city__isnull=True))
    by_place = defaultdict(list)
    # An SOS alert duplicates the SOS item above, which already says it is late.
    # A threshold on open or late SOS restates the SOS items above; one item per event.
    from .models import THRESHOLD_KINDS
    sos_thresholds = [k for k in THRESHOLD_KINDS if 'sos' in k]
    for alert in (alerts.exclude(source_type='safety_incident').exclude(kind__in=sos_thresholds)
                  .select_related('city')[:300]):
        by_place[alert.city_id].append(alert)
    sev = {'critical': CRITICAL, 'warning': HIGH, 'info': WAITING}
    for rows in by_place.values():
        city = rows[0].city
        if len(rows) > GROUP_OVER:
            worst = min(rows, key=lambda a: SEVERITY_RANK[sev.get(a.severity, WAITING)])
            items.append(_item('alert', sev.get(worst.severity, WAITING),
                               f"{city.name if city else 'Unplaced'}: {len(rows)} alerts",
                               where=_place(city), at=min(a.created_at for a in rows), now=now,
                               count=len(rows),
                               link='/dashboard?alerts=1' + (f'&scope={city.code}' if city else '')))
            continue
        for a in rows:
            items.append(_item('alert', sev.get(a.severity, WAITING), a.title, where=_place(a.city),
                               at=a.created_at, now=now, link=_alert_link(a),
                               actions=['acknowledge']))

    items.sort(key=lambda i: (SEVERITY_RANK[i['severity']], KIND_RANK.get(i['kind'], 9),
                              -i['age_seconds']))
    counts = defaultdict(int)
    for i in items:
        counts[i['severity']] += 1
    viewers = viewing_now([i['link'] for i in items[:limit]], user)
    for i in items[:limit]:
        i['viewers'] = viewers.get(i['link'], [])
    return {'generated_at': now.isoformat(), 'total': len(items), 'counts': dict(counts),
            'items': items[:limit]}


def _alert_link(alert):
    payload = alert.payload or {}
    if payload.get('incident_id'):
        return f"/safety-ops/incidents/{payload['incident_id']}"
    if payload.get('trip_id'):
        return f"/trips/{payload['trip_id']}"
    if payload.get('city') or alert.city_id:
        return f"/dashboard?scope={alert.city.code if alert.city_id else payload.get('city')}"
    return '/dashboard'


# ---------------------------------------------------------------------------
# Who else is viewing
# ---------------------------------------------------------------------------

VIEWING_TTL = 60


def _viewing_key(path):
    return 'console:viewing:' + path[:200]


def mark_viewing(path, user):
    """Note that `user` has `path` open (renewed by the page every 30 s)."""
    key = _viewing_key(path)
    now = timezone.now().timestamp()
    seen = cache.get(key) or {}
    seen = {uid: v for uid, v in seen.items() if now - v[1] < VIEWING_TTL}
    label = (getattr(user, 'full_name', '') or '').strip() or (user.email or '').split('@')[0]
    seen[str(user.pk)] = (label, now)
    cache.set(key, seen, VIEWING_TTL)


def viewing_now(paths, user):
    """{path: [labels]} of OTHER operators with each path open now."""
    now = timezone.now().timestamp()
    out = {}
    keys = {_viewing_key(p): p for p in paths}
    for key, seen in (cache.get_many(list(keys)) or {}).items():
        out[keys[key]] = [label for uid, (label, at) in seen.items()
                          if uid != str(user.pk) and now - at < VIEWING_TTL]
    return out
