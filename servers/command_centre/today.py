"""What needs action today, per country and per live city (owner, 22 Nov, items 3 and 11).

GET command-centre/admin/today/?scope=<node>

* `countries`: one tile per country the caller can see, each in its OWN currency and local day:
  requests, completed, completion rate, open SOS, payouts waiting and revenue (fares from
  completed trips). Money is never added across currencies, and a figure the caller may not see
  (money without `finance.read`) is left out, not zeroed.
* `cities`: every LIVE city in scope with what needs a person: open SOS, approvals waiting for
  this caller, payouts waiting, KYC waiting, stuck trips, drivers online, each with the link to
  its list. A city that is not live is not here.
* `all_clear`: nothing waits anywhere in scope, so the console shows one line instead of tiles.

Test trips and test accounts never count (`base.test_data.real_trips`, used by `measure`).
"""

from collections import defaultdict

from django.utils import timezone

from servers.admin_access.bridge import granted
from servers.admin_access.models import ACTIVE, GeoNode

from .overview import city_contexts, measure
from .workspace import live_counts

REFRESH_WORDS = 'Updated every minute; trip figures from five-minute rollups'


def _approvals_by_city(user, cities):
    """{city_id: n} of requests waiting for THIS caller as checker, by the request's place."""
    out = defaultdict(int)
    if not granted(user, 'approval.check')[0]:
        return out
    from servers.approvals.models import REQUESTED, ApprovalRequest
    from servers.approvals.scope import visible
    from servers.approvals.service import checker_problem

    by_path = sorted(((c.path, c.id) for c in cities), key=lambda p: -len(p[0]))
    waiting = (visible(user, ApprovalRequest.objects.select_related('scope_node', 'policy'))
               .filter(status=REQUESTED).exclude(maker_id=user.pk)[:500])
    for r in waiting:
        if checker_problem(r, user) is not None:
            continue
        path = getattr(r.scope_node, 'path', '') or ''
        for city_path, cid in by_path:
            if path.startswith(city_path):
                out[cid] += 1
                break
    return out


def _link(path, code):
    return f"{path}{'&' if '?' in path else '?'}scope={code}"


def today(user, cities, now=None, date_range=None):
    """`date_range`: (first, last) local days (23 Nov date presets); None means today."""
    now = now or timezone.now()
    money = granted(user, 'finance.read')[0]
    contexts = city_contexts(cities)
    window = ('range', *date_range) if date_range else 'today'
    rows = measure(contexts, window, now, previous=False) if cities else {}
    live = live_counts(cities, user) if cities else {}
    approvals = _approvals_by_city(user, cities)

    countries = {}
    names = {n.code: n.name for n in GeoNode.objects.filter(kind='country')}
    out_cities = []
    for city in cities:
        ctx, row, now_live = contexts[city.id], rows.get(city.id), live.get(city.id, {})
        if row is None:
            continue
        code = ctx.country or '—'
        t = countries.setdefault(code, {
            'code': code, 'name': names.get(code, code), 'currency': ctx.currency,
            'requests': 0, 'completed': 0, 'sos_open': 0, 'payouts_waiting': 0,
            'payouts_waiting_minor': 0 if money else None, 'revenue_minor': 0 if money else None,
            'live_cities': 0,
        })
        t['requests'] += row['trips']['requested']['now']
        t['completed'] += row['trips']['completed']['now']
        t['sos_open'] += now_live.get('sos_active', 0)
        if money:
            t['payouts_waiting'] += row['queues'].get('withdrawals_pending', {}).get('now', 0)
            cur = ctx.currency
            t['revenue_minor'] += row['money']['collected'].get(cur, {}).get('now_minor', 0)
            t['payouts_waiting_minor'] += row['money']['withdrawals_pending_sum'].get(cur, {}).get('now_minor', 0)
        if city.status != ACTIVE:
            continue
        t['live_cities'] += 1
        payouts = row['queues'].get('withdrawals_pending', {}).get('now', 0) if money else None
        actions = [
            {'key': 'sos', 'label': 'Open SOS', 'count': now_live.get('sos_active', 0),
             'link': _link('/ops/safety-ops', city.code), 'urgent': True},
            {'key': 'approvals', 'label': 'Approvals waiting for you', 'count': approvals.get(city.id, 0),
             'link': _link('/ops/approvals', city.code)} if granted(user, 'approval.check')[0] else None,
            {'key': 'payouts', 'label': 'Payouts waiting', 'count': payouts,
             'link': _link('/ops/finance/payouts?status=pending', city.code)} if money else None,
            {'key': 'kyc', 'label': 'KYC waiting', 'count': now_live.get('kyc_waiting', 0),
             'link': _link('/ops/kyc', city.code)} if granted(user, 'driver.read')[0] else None,
            {'key': 'kyc_uploading', 'label': 'drivers still uploading documents',
             'count': now_live.get('kyc_uploading', 0),
             'link': _link('/ops/drivers?kyc_state=pending', city.code)} if granted(user, 'driver.read')[0] else None,
            {'key': 'stuck', 'label': 'Stuck trips', 'count': now_live.get('stuck_trips', 0),
             'link': _link('/ops/trips?stuck=1', city.code)} if granted(user, 'trip.read')[0] else None,
        ]
        actions = [a for a in actions if a is not None]
        out_cities.append({
            'code': city.code, 'name': city.name, 'country': code, 'timezone': str(ctx.tz),
            'actions': actions,
            'waiting': sum(a['count'] or 0 for a in actions if a['key'] != 'kyc_uploading'),
            'drivers_online': now_live.get('drivers_online', 0),
            'drivers_link': _link('/ops/drivers?status=online', city.code),
        })

    for t in countries.values():
        t['completion_rate'] = round(100 * t['completed'] / t['requests']) if t['requests'] else None
    out_cities.sort(key=lambda c: (-c['waiting'], c['name']))
    return {
        'generated_at': now.isoformat(),
        'range': ({'from': date_range[0].isoformat(), 'to': date_range[1].isoformat()} if date_range else None),
        'refresh': REFRESH_WORDS,
        'countries': sorted(countries.values(), key=lambda c: (-c['live_cities'], c['name'])),
        'cities': out_cities,
        'all_clear': not any(c['waiting'] for c in out_cities),
        'money': money,
    }
