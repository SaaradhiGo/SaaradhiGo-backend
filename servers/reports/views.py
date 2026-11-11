"""The Reports operator API (module 14). Read-only: no write to business data.

  GET /api/v1/reports/admin/catalogue/                              report.read -> the reports this caller may open
  GET /api/v1/reports/admin/<key>/?from&to&city&vehicle&granularity&group_by
  GET /api/v1/reports/admin/<key>/export/?...same...               CSV of what the report shows

All under `admin/` so the structural guard finds them by path. The route gate is `report.read`
(the door); the report's own capability is asked inside (see `catalog`), at the places it is held:
403 when the caller holds no way to open the report, 404 for an explicit `?city=` outside their
scope (or unknown), 400 for a bad parameter.

The data is the daily rollups (`models.py`), never a live scan of trips. Money is per currency.
The CSV is exactly the table the console shows (aggregates only: no person, no id), audited with
the report, parameters and row count, rate limited per operator, and refused if it cannot be
logged.
"""

import csv
import io

from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle

from base.money import from_minor
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_audit.services import record_admin_action
from servers.command_centre.overview import city_contexts
from servers.payments.finance_common import csv_safe

from . import service
from .catalog import AREAS, GRANULARITIES, MAX_RANGE_DAYS, REPORT_DOOR, REPORTS
from .service import NotInScope, ParamError

EXPORT_AUDIT_ACTION = 'report_exported'


class ReportExportThrottle(UserRateThrottle):
    scope = 'report_export'

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {'scope': self.scope, 'ident': request.user.pk}


def _bad(exc):
    return error_response(code='VALIDATION_ERROR', message=exc.message, field=exc.field,
                          issue=exc.message, status=status.HTTP_400_BAD_REQUEST)


def _not_found(what='Report'):
    return error_response(code='NOT_FOUND', message=f'{what} not found', field='id',
                          issue=f'No {what.lower()} matches', status=status.HTTP_404_NOT_FOUND)


def _forbidden():
    return error_response(code='PERMISSION_DENIED',
                          message='You do not have permission to do that here.',
                          field='permission', issue='Capability not held at this place',
                          status=status.HTTP_403_FORBIDDEN)


def _city_rows(cities):
    contexts = city_contexts(cities)
    return [{'code': c.code, 'name': c.name, 'currency': contexts[c.id].currency,
             'timezone': str(contexts[c.id].tz)} for c in cities]


def _prepare(request, key):
    """`(report, cities, params, refusal)`; refusal is a ready Response or None."""
    report = REPORTS.get(key)
    if report is None:
        return None, None, None, _not_found()
    if not service.may_open(request.user, report):
        return None, None, None, _forbidden()
    try:
        cities, _node = service.resolve_cities(
            request.user, report, (request.query_params.get('city') or '').strip())
        params = service.parse_params(request.query_params, report,
                                      service.default_to_date(cities))
    except NotInScope:
        return None, None, None, _not_found('City')
    except ParamError as exc:
        return None, None, None, _bad(exc)
    return report, cities, params, None


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission(REPORT_DOOR)])
def report_catalogue(request):
    """The reports the caller may open, by area, plus the filters (cities, vehicle classes)."""
    cards, cities_seen = [], {}
    for report in REPORTS.values():
        if not service.may_open(request.user, report):
            continue
        cities = service.cities_in(service.allowed_paths(request.user, report))
        cards.append(report.as_card())
        cities_seen.update({c.code: c for c in cities})
    scope_cities = sorted(cities_seen.values(), key=lambda c: c.path)
    from .models import RebuildLog

    latest = RebuildLog.objects.filter(city__in=scope_cities).order_by('-built_at').values_list(
        'built_at', flat=True).first()
    return success_response({
        'areas': [{'area': area, 'reports': [c for c in cards if c['area'] == area]}
                  for area in AREAS if any(c['area'] == area for c in cards)],
        'cities': _city_rows(scope_cities),
        'vehicle_types': service.vehicle_types(scope_cities),
        'granularities': list(GRANULARITIES),
        'max_range_days': MAX_RANGE_DAYS,
        'as_of': latest.isoformat() if latest else None,
    }, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission(REPORT_DOOR)])
def report_detail(request, report_key):
    report, cities, params, refusal = _prepare(request, report_key)
    if refusal:
        return refusal
    body = service.build(report, cities, params)
    stamp = service.as_of(cities, params)
    currencies = sorted({r['currency'] for r in body['rows'] if r.get('currency')}
                        | {t['currency'] for t in body['totals'] if t.get('currency')})
    return success_response({
        'report': {**report.as_card(), 'definitions': list(report.definitions)},
        'params': params.as_dict(),
        'scope': {'cities': _city_rows(cities)},
        'currencies': currencies,
        'as_of': stamp.isoformat() if stamp else None,
        **body,
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def _cell(column, row):
    value = row.get(column['key'])
    if value is None:
        return ''
    if column['type'] == 'money':
        return str(from_minor(value, row.get('currency') or 'INR'))
    if column['type'] == 'hours' or column['type'] == 'percent':
        return value
    return value


def csv_for(report, body, params):
    """`(text, row_count)`: the table as the console shows it, money in major units."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if report.key == 'rider_retention':
        width = max((len(c['retained']) for c in body['cohorts']), default=0)
        writer.writerow(['cohort_week', 'riders'] + [f'week_{k}_percent' for k in range(width)])
        for c in body['cohorts']:
            writer.writerow([c['week'], c['size']] + [
                '' if p is None else p for p in c['percent']] + [''] * (width - len(c['percent'])))
        return buffer.getvalue(), len(body['cohorts'])
    columns = body['columns']
    dims = ['period']
    if params.group_by == 'city':
        dims.append('city')
    elif params.group_by == 'vehicle':
        dims.append('vehicle')
    if report.money:
        dims.append('currency')
    writer.writerow(dims + [c['key'].replace('_minor', '') for c in columns])
    for row in body['rows']:
        writer.writerow([csv_safe(row.get(d, '')) for d in dims]
                        + [csv_safe(_cell(c, row)) for c in columns])
    return buffer.getvalue(), len(body['rows'])


def _scope_token(cities, params):
    if params.city:
        return params.city
    return cities[0].code if len(cities) == 1 else 'all'


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission(REPORT_DOOR)])
@throttle_classes([ReportExportThrottle])
def report_export(request, report_key):
    """CSV of the report as shown. Audited; no log, no export."""
    report, cities, params, refusal = _prepare(request, report_key)
    if refusal:
        return refusal
    body = service.build(report, cities, params)
    text, count = csv_for(report, body, params)
    audit = record_admin_action(
        request, action=EXPORT_AUDIT_ACTION, target_type='report', target_id=report.key,
        after={'report': report.key, 'params': params.as_dict(), 'row_count': count,
               'scope': _scope_token(cities, params), 'cities': [c.code for c in cities]},
        reason='')
    if audit is None:
        return error_response(
            code='AUDIT_UNAVAILABLE', message='The export could not be logged, so it was not produced.',
            field='general', issue='audit write failed', status=status.HTTP_503_SERVICE_UNAVAILABLE)
    response = HttpResponse(text, content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = (
        f'attachment; filename="report-{report.key}-{_scope_token(cities, params)}-'
        f'{params.d_from:%Y%m%d}-{params.d_to:%Y%m%d}.csv"')
    response['Cache-Control'] = 'no-store'
    return response
