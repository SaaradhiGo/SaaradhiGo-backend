"""Shared plumbing for the Finance read endpoints.

Nothing here decides who may see what (that is `servers.payments.scope`); it
parses the query string the same way for the JSON endpoints and the CSV
exports, so a filter means the same thing in both, and it owns the one place a
"day" is turned into a time window in the CITY'S OWN timezone.

A DAY BELONGS TO A CITY
-----------------------
"4 March" in Hyderabad starts at 18:30 UTC on the 3rd; in Austin it starts at
06:00 UTC on the 4th. A total taken over UTC days would put a late-evening US
trip on the wrong day, so every date range and every per-day group is computed
in the timezone of the city the money belongs to (the timezone of a service
zone in that city, else the country default). Rows that cannot be placed in a
city (only ever visible to unrestricted callers) use the deployment timezone.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q
from rest_framework.pagination import PageNumberPagination

from base.countries import get_country
from base.money import from_minor
from servers.admin_access.masking import BULLET

#: The longest date range any finance endpoint will aggregate in one request.
MAX_RANGE_DAYS = 92


class FinancePagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100

    def get_paginated_response(self, data):
        response = super().get_paginated_response(data)
        # The console draws "Page 2 of 7" without parsing the next/previous URLs.
        response.data['page'] = self.page.number
        response.data['page_size'] = self.page.paginator.per_page
        response.data['total_pages'] = self.page.paginator.num_pages
        return response


class BadParam(Exception):
    """A query parameter that cannot be used; the view answers 400."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
        self.message = message


def parse_date(request, name):
    raw = (request.query_params.get(name) or '').strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise BadParam(name, f'{name} must be a date like 2026-03-04') from None


def latest_today(tz=None):
    """Today's date in `tz`; with none, the latest "today" any city has reached
    (so a default range ending "today" includes a city already into tomorrow)."""
    now = datetime.now(ZoneInfo('UTC'))
    if tz is not None:
        return now.astimezone(tz).date()
    zones = list(node_timezones().values()) + [ZoneInfo('UTC')]
    return max(now.astimezone(z).date() for z in zones)


def parse_range(request, *, default_days=None, require=False, tz=None):
    """`(from_date, to_date)` inclusive, validated; defaults when asked.

    The default is the last `default_days` days ending today in `tz` (a city's
    own day), or, with no `tz`, ending at the latest "today" in any city.
    """
    d_from, d_to = parse_date(request, 'from'), parse_date(request, 'to')
    if default_days and d_to is None and d_from is None:
        d_to = latest_today(tz)
        d_from = d_to - timedelta(days=default_days - 1)
    if require and (d_from is None or d_to is None):
        raise BadParam('from', 'from and to are both required')
    if d_from and d_to:
        if d_to < d_from:
            raise BadParam('to', 'to must not be before from')
        if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
            raise BadParam('to', f'The range is limited to {MAX_RANGE_DAYS} days')
    return d_from, d_to


def parse_int(request, name):
    raw = (request.query_params.get(name) or '').strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise BadParam(name, f'{name} must be a whole number') from None


def parse_currency(request):
    raw = (request.query_params.get('currency') or '').strip().upper()
    if raw and (len(raw) != 3 or not raw.isalpha()):
        raise BadParam('currency', 'currency must be a 3-letter code')
    return raw


def mask_tail(value, keep=4):
    """A gateway id shows only its last characters: enough to find it at the
    gateway, not enough to use it."""
    if value in (None, ''):
        return value
    text = str(value)
    if len(text) <= keep:
        return BULLET * 4
    return BULLET * 4 + text[-keep:]


def money(minor, currency):
    """The decimal string for a minor-unit amount (display only)."""
    if minor is None:
        return None
    return str(from_minor(minor, currency))


# ---------------------------------------------------------------------------
# A day in the city's own timezone
# ---------------------------------------------------------------------------

def _tz(name):
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a bad name falls back, never 500s a report
        return ZoneInfo(settings.TIME_ZONE)


def node_timezones():
    """`{geo_node_id: ZoneInfo}` for every city, plus every node a zone hangs on."""
    from servers.admin_access.models import CITY, GeoNode
    from servers.pricing.models import ServiceZone

    zones = {}
    for row in (ServiceZone.objects.exclude(geo_node__isnull=True)
                .values('geo_node_id', 'timezone_name', 'country').order_by('id')):
        name = row['timezone_name'] or get_country(row['country']).default_timezone
        zones.setdefault(row['geo_node_id'], name)
    result = {}
    for node in GeoNode.objects.filter(kind=CITY).select_related('parent'):
        if node.id in zones:
            result[node.id] = _tz(zones[node.id])
            continue
        country = node.country
        result[node.id] = _tz(get_country(country.code).default_timezone) if country else _tz(settings.TIME_ZONE)
    for node_id, name in zones.items():
        result.setdefault(node_id, _tz(name))
    return result


def day_bounds(d_from, d_to, tz):
    """UTC-aware `[start, end)` covering local dates `d_from..d_to` inclusive."""
    start = datetime.combine(d_from, time.min, tzinfo=tz)
    end = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def group_by_timezone(tzmap):
    """`{tz_name: (tzinfo, [node ids])}`."""
    groups = {}
    for node_id, tz in tzmap.items():
        key = getattr(tz, 'key', str(tz))
        groups.setdefault(key, (tz, []))[1].append(node_id)
    return groups


def city_day_q(anchor_id_lookup, time_field, d_from, d_to, *, include_unplaced):
    """A `Q` restricting `time_field` to local dates `d_from..d_to`, in each
    city's own day. `anchor_id_lookup` reaches the city's GeoNode id from the
    model (e.g. `trip_id__zone__geo_node_id`).

    `include_unplaced` is True only for unrestricted callers (rows with no
    city), who get the deployment timezone for them.
    """
    if d_from is None and d_to is None:
        return Q()
    d_from = d_from or d_to
    d_to = d_to or d_from
    q = Q(pk__in=[])
    for _name, (tz, ids) in group_by_timezone(node_timezones()).items():
        start, end = day_bounds(d_from, d_to, tz)
        q |= Q(**{f'{anchor_id_lookup}__in': ids, f'{time_field}__gte': start,
                  f'{time_field}__lt': end})
    if include_unplaced:
        start, end = day_bounds(d_from, d_to, _tz(settings.TIME_ZONE))
        q |= Q(**{f'{anchor_id_lookup}__isnull': True, f'{time_field}__gte': start,
                  f'{time_field}__lt': end})
    return q


def csv_safe(value):
    """Neutralise spreadsheet formula injection in a CSV cell."""
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value
