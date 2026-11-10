"""Earnings history for the driver web and app (10 Nov v2). See servers.driver.earnings_history.

  GET earnings/history/?period=day|week|month|year|taxyear|custom&date=YYYY-MM-DD[&start=&end=]
  GET earnings/months/                     every month since joining, newest first
  GET earnings/statement/?month=YYYY-MM&as=json|csv|pdf

`as`, not `format`: DRF reads `format` as a renderer choice.
"""
import re
from datetime import date

from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes

from base.utils import error_response, success_response

from . import earnings_history as eh
from .permissions import IsDriver

MONTH_RE = re.compile(r'^(\d{4})-(\d{2})$')


def _bad(message, field):
    return error_response('INVALID_PERIOD', message, field, 'invalid', status.HTTP_400_BAD_REQUEST)


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return False


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_earnings_history(request):
    q = request.query_params
    period = q.get('period', 'month')
    if period not in eh.PERIODS:
        return _bad('period must be day, week, month, year, taxyear or custom', 'period')
    anchor, start, end = _date(q.get('date')), _date(q.get('start')), _date(q.get('end'))
    if False in (anchor, start, end):
        return _bad('Dates are YYYY-MM-DD.', 'date')
    try:
        body = eh.history(request.user.driver, period, anchor, start, end)
    except eh.BadPeriod as e:
        return _bad(str(e), 'start')
    return success_response(body, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_earnings_months(request):
    return success_response(eh.months(request.user.driver), status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_earnings_statement(request):
    match = MONTH_RE.match(request.query_params.get('month', ''))
    if not match or not 1 <= int(match.group(2)) <= 12:
        return _bad('month is YYYY-MM.', 'month')
    st = eh.statement(request.user.driver, int(match.group(1)), int(match.group(2)))
    kind = request.query_params.get('as', 'json')
    name = f"saaradhigo-statement-{st['month']}"
    if kind == 'csv':
        resp = HttpResponse(eh.statement_csv(st), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="{name}.csv"'
        return resp
    if kind == 'pdf':
        resp = HttpResponse(eh.statement_pdf(st), content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="{name}.pdf"'
        return resp
    return success_response(st, status.HTTP_200_OK)
