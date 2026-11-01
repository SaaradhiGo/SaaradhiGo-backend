"""The audit-log viewer (module 15): list, detail, chain verification and CSV, all READ-ONLY.

WHO SEES WHICH ENTRY
--------------------
`AdminAuditLog` has no place column and is hash-chained: adding one would change what every row
hashes to. A place is therefore READ from the row, in this order of precedence:

  1. a node code under `scope`, `city_code`, `geo_node_code`, `zone_code`, `home_city_code` or
     `city` in `after` / `before` (approval rows carry `scope`; the city status rows target the
     `geonode` itself);
  2. else the country code under `country`.

An operator holding `audit.read` sees an entry only if its place is a node their scope covers, or
(for a country-only entry) they hold that whole country. An entry with NO readable place is
visible to Super Admin only: it cannot be attributed to a place, and showing it to a city
operator would show them the world. Super Admin (unscoped) sees everything.

MASKING
-------
The response goes through `MaskingJSONRenderer` like every operator route (keys named like a phone,
email or identifier are masked by name). On top of that, `scrub` replaces any string in
`before`/`after` that LOOKS like a phone number, email, UPI handle or IFSC code, because an audit
payload is free-form and a value can sit under a key the masker has no opinion about.
"""

import csv
import io
import re
from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from servers.admin_access.access import scope_paths
from servers.admin_access.masking import BULLET, RAW_PII_PATTERNS, mask_contact
from servers.admin_access.models import COUNTRY, GeoNode

from .hashchain import GENESIS, compute_hash
from .models import AdminAuditLog

NODE_KEYS = ('scope', 'city_code', 'geo_node_code', 'zone_code', 'home_city_code', 'city')
COUNTRY_KEY = 'country'
EXPORT_ACTION = 'audit_log_exported'
EXPORT_MAX_ROWS = 5000
VERIFY_MAX_ROWS = 20000
PAGE_SIZE, MAX_PAGE_SIZE = 50, 100


class FilterError(ValueError):
    def __init__(self, message, field):
        super().__init__(message)
        self.message, self.field = message, field


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def _covered_nodes(user):
    """`None` for an unscoped operator, else the nodes under every path they hold."""
    paths = scope_paths(user)
    if paths is None:
        return None
    if not paths:
        return []
    q = Q()
    for path in paths:
        q |= Q(path__startswith=path)
    return list(GeoNode.objects.filter(q))


def _place_q(codes, country_codes=()):
    q = Q(target_type='geonode', target_id__in=codes)
    for key in NODE_KEYS:
        q |= Q(**{f'after__{key}__in': codes}) | Q(**{f'before__{key}__in': codes})
    if country_codes:
        any_node = Q()
        for key in NODE_KEYS:
            any_node |= Q(**{f'after__{key}__isnull': False}) | Q(**{f'before__{key}__isnull': False})
        q |= (Q(**{f'after__{COUNTRY_KEY}__in': list(country_codes)})
              | Q(**{f'before__{COUNTRY_KEY}__in': list(country_codes)})) & ~any_node
    return q


def scope_queryset(user, queryset):
    """The entries this operator may see. Unscoped: all, including entries with no place."""
    nodes = _covered_nodes(user)
    if nodes is None:
        return queryset
    if not nodes:
        return queryset.none()
    codes = [n.code for n in nodes]
    country_codes = [n.code for n in nodes if n.kind == COUNTRY]
    return queryset.filter(_place_q(codes, country_codes))


def place_of(entry):
    """The place code an entry is attributed to, or None."""
    for blob in (entry.after, entry.before):
        if isinstance(blob, dict):
            for key in NODE_KEYS:
                value = blob.get(key)
                if isinstance(value, str) and value:
                    return value
    if entry.target_type == 'geonode' and entry.target_id:
        return entry.target_id
    for blob in (entry.after, entry.before):
        if isinstance(blob, dict) and isinstance(blob.get(COUNTRY_KEY), str) and blob[COUNTRY_KEY]:
            return blob[COUNTRY_KEY]
    return None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _when(value, field, end=False):
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) == 10:
            d = date.fromisoformat(text)
            moment = datetime.combine(d + timedelta(days=1) if end else d, time.min)
            return timezone.make_aware(moment)
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return moment if timezone.is_aware(moment) else timezone.make_aware(moment)
    except ValueError:
        raise FilterError('Use an ISO date (2026-10-31) or date-time.', field)


def filtered(user, params):
    """The scoped, filtered queryset, newest first. Raises `FilterError` for a bad parameter."""
    qs = scope_queryset(user, AdminAuditLog.objects.all())
    actor = (params.get('actor') or '').strip()
    if actor:
        if actor.isdigit():
            qs = qs.filter(actor_id=int(actor))
        else:
            qs = qs.filter(actor_label__icontains=actor[:80])
    for field in ('action', 'target_type', 'target_id'):
        value = (params.get(field) or '').strip()
        if value:
            qs = qs.filter(**{field: value[:100]})
    start, end = _when(params.get('from'), 'from'), _when(params.get('to'), 'to', end=True)
    if start:
        qs = qs.filter(created_at__gte=start)
    if end:
        qs = qs.filter(created_at__lt=end)
    if start and end and end <= start:
        raise FilterError('"to" must be after "from".', 'to')
    city = (params.get('city') or '').strip().upper()
    if city:
        node = GeoNode.objects.filter(code=city).first()
        if node is None:
            raise FilterError(f'Unknown place {city!r}.', 'city')
        inside = [n.code for n in GeoNode.objects.filter(path__startswith=node.path)]
        qs = qs.filter(_place_q(inside, [node.code] if node.kind == COUNTRY else []))
    return qs.order_by('-id')


# ---------------------------------------------------------------------------
# Serialising
# ---------------------------------------------------------------------------

def scrub(value):
    """Replace raw contact-shaped strings anywhere inside a JSON value."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str) and any(rx.search(value) for rx in RAW_PII_PATTERNS.values()):
        return BULLET * 6
    return value


def _actor_label(entry):
    label = entry.actor_label or ''
    if label and ('@' in label or re.sub(r'[\s+\-()]', '', label).isdigit()):
        return mask_contact(label)
    return label or None


def _mask_ip(ip):
    if not ip:
        return None
    if ':' in str(ip):
        return str(ip).rsplit(':', 1)[0] + ':x'
    parts = str(ip).split('.')
    return '.'.join(parts[:3] + ['x']) if len(parts) == 4 else None


def entry_row(entry):
    return {
        'id': entry.id, 'created_at': entry.created_at.isoformat(),
        'actor': {'id': entry.actor_id, 'label': _actor_label(entry), 'role': entry.actor_role},
        'action': entry.action, 'target_type': entry.target_type, 'target_id': entry.target_id,
        'place': place_of(entry), 'reason': (entry.reason or '')[:200],
    }


def entry_detail(entry):
    return {
        **entry_row(entry), 'reason': scrub(entry.reason or ''),
        'before': scrub(entry.before or {}), 'after': scrub(entry.after or {}),
        'ip': _mask_ip(entry.ip_address), 'user_agent': (entry.user_agent or '')[:120],
        'hash': {'previous': (entry.previous_hash or '')[:8],
                 'content': (entry.content_hash or '')[:8],
                 'hashed': bool(entry.content_hash)},
    }


# ---------------------------------------------------------------------------
# Chain verification for a range
# ---------------------------------------------------------------------------

def verify_range(queryset):
    """Verify the hash chain over the contiguous id range covered by `queryset`'s date window.

    Read-only. The window is taken from the SAME filters (`from`/`to`), but the chain is checked
    over every row between the first and last id of that window, scoped or not, because a chain
    is only meaningful when contiguous; the answer is a verdict and ids, never row content.
    """
    ids = list(queryset.order_by('id').values_list('id', flat=True)[:1])
    last = queryset.order_by('-id').values_list('id', flat=True).first()
    if not ids or last is None:
        return {'status': 'empty', 'checked': 0, 'problems': [], 'truncated': False,
                'first_id': None, 'last_id': None, 'pre_chain_rows': 0}
    first = ids[0]
    return verify_ids(first, last)


def verify_ids(first, last):
    previous = (AdminAuditLog.objects.filter(id__lt=first).order_by('-id')
                .values_list('content_hash', flat=True).first())
    expected = previous or GENESIS
    problems, checked, pre_chain, truncated = [], 0, 0, False
    rows = AdminAuditLog.objects.filter(id__gte=first, id__lte=last).order_by('id')
    for entry in rows.iterator():
        if checked >= VERIFY_MAX_ROWS:
            truncated = True
            break
        checked += 1
        if not entry.content_hash:
            if expected != GENESIS or problems:
                problems.append({'id': entry.pk, 'issue': 'unhashed row'})
            else:
                pre_chain += 1
            continue
        if entry.previous_hash != expected and not (pre_chain and expected == GENESIS):
            problems.append({'id': entry.pk, 'issue': 'broken link'})
        if compute_hash(entry.previous_hash, entry) != entry.content_hash:
            problems.append({'id': entry.pk, 'issue': 'content altered'})
        expected = entry.content_hash
    return {'status': 'ok' if not problems else 'broken', 'checked': checked,
            'problems': problems[:50], 'problem_count': len(problems), 'truncated': truncated,
            'first_id': first, 'last_id': last, 'pre_chain_rows': pre_chain}


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def csv_text(entries):
    """`(text, row_count)`: id, time, actor, role, action, target, place, reason. No before/after:
    those are free-form and are read in the viewer, masked, one entry at a time."""
    from servers.payments.finance_common import csv_safe

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['id', 'created_at', 'actor_id', 'actor', 'actor_role', 'action',
                     'target_type', 'target_id', 'place', 'reason'])
    count = 0
    for entry in entries[:EXPORT_MAX_ROWS]:
        writer.writerow([
            entry.id, entry.created_at.isoformat(), entry.actor_id or '',
            csv_safe(_actor_label(entry) or ''), csv_safe(entry.actor_role or ''),
            csv_safe(entry.action), csv_safe(entry.target_type), csv_safe(entry.target_id),
            csv_safe(place_of(entry) or ''), csv_safe(scrub((entry.reason or '')[:500]))])
        count += 1
    return buffer.getvalue(), count

