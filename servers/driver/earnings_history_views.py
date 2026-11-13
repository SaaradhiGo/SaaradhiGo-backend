"""Earnings history for the driver web and app (10 Nov v2). See servers.driver.earnings_history.

  GET earnings/history/?period=day|week|month|year|taxyear|custom&date=YYYY-MM-DD[&start=&end=]
  GET earnings/months/                     every month since joining, newest first
  GET earnings/statement/?month=YYYY-MM&as=json|csv|pdf
  GET trips/<id>/                          one of the driver's trips: places, times, fare lines (11 Nov)

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


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_trip_detail(request, trip_id):
    """One of the driver's own trips (11 Nov): places and times, distance and duration, the class,
    the fare lines from the ledger (fare, platform fee, any refund of it, tips, earned), how it was
    paid, and the rider's first name and rating. Another driver's trip is not found."""
    from base.market_time import driver_timezone
    from base.money import from_minor
    from servers.payments.models import TripSettlement
    from servers.pricing.fee_plans import REFUND_PURPOSE
    from servers.ride.models import Trip
    from servers.rider.models import WalletTransaction
    from .earnings import SETTLEMENT_PURPOSES, _row_for
    from .services import driver_currency

    driver = request.user.driver
    trip = (Trip.objects.select_related('user_id', 'requested_vehicle_type', 'status_id', 'zone')
            .filter(pk=trip_id, driver_id=driver).first())
    if trip is None:
        return error_response('NOT_FOUND', 'Trip not found.', 'trip_id', 'not found', status.HTTP_404_NOT_FOUND)
    tz = driver_timezone(driver)
    ref = f'TRIP_{trip.id}'
    entry = (WalletTransaction.objects.filter(user_id=driver.user_id, reference_id=ref, status='completed',
                                              purpose__in=SETTLEMENT_PURPOSES).order_by('id').first())
    settlement = TripSettlement.objects.filter(trip_id=trip.id).order_by('-version').first()
    row = _row_for(entry, trip, trip.id, settlement) if entry is not None else None
    cur = driver_currency(driver)
    refund = sum(WalletTransaction.objects.filter(user_id=driver.user_id, reference_id=ref, status='completed',
                                                  purpose=REFUND_PURPOSE).values_list('amount_minor', flat=True))
    refund = from_minor(refund, cur)
    rider = trip.user_id
    first = (rider.full_name or '').split()[0] if rider is not None and rider.full_name else ''
    rating = getattr(getattr(rider, 'rider', None), 'rating', None)
    minutes = trip.actual_duration_min
    if minutes is None and trip.started_at and trip.completed_at:
        minutes = round((trip.completed_at - trip.started_at).total_seconds() / 60)
    at = lambda t: t.astimezone(tz).isoformat() if t else None  # noqa: E731
    s = lambda v: None if v is None else str(v)  # noqa: E731
    earned = (row.net + refund) if row is not None and row.net is not None else None
    return success_response({
        'id': trip.id, 'status': getattr(trip.status_id, 'status_code', ''), 'currency': cur,
        'pickup_address': trip.pickup_address or '', 'destination_address': trip.destination_address or '',
        'pickup_label': trip.pickup_label, 'destination_label': trip.destination_label,  # 13 Nov
        'requested_at': at(trip.requested_at), 'started_at': at(trip.started_at), 'completed_at': at(trip.completed_at),
        'distance_km': s(trip.actual_distance_km if trip.actual_distance_km is not None else trip.estimated_distance_km),
        'duration_min': s(minutes if minutes is not None else trip.estimated_duration_min),
        'ride_class': getattr(trip.requested_vehicle_type, 'type', '') or '',
        'payment_method': row.method if row is not None else (trip.payment_method or ''),
        # The fee net of its refund, as the Trips row shows it; the refund itself is tagged.
        'fare': s(row.gross) if row else None,
        'platform_fee': s(row.commission - refund) if row and row.commission is not None else None,
        'platform_fee_refunded': s(refund) if refund else None, 'tips': '0.00', 'earned': s(earned),
        'cash_in_hand': s(row.gross) if row is not None and row.driver_holds_cash else None,
        'rider': {'first_name': first, 'rating': s(rating)},
    }, status.HTTP_200_OK)
