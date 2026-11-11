import logging
from django.core.exceptions import ValidationError
from django.utils import timezone
from base.utils import success_response, error_response
from servers.redis_client import add_driver_location,remove_driver
from rest_framework.decorators import (
    api_view, authentication_classes, parser_classes, permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from .serializers import DriverProfileSerializer
from .utils import update_driver_location
from base.storage_health import save_with_storage_errors
from .permissions import IsDriver
from .models import Vehicle
from base.media import EMPTY_FILE_VALUES, resolve_file_input
logger = logging.getLogger(__name__)



# @api_view(['POST'])
# @permission_classes([IsDriver])
# def add_driver(request):
#     """
#     Add or update driver location.
    
#     Expected request data:
#     {
#         "lng": float,
#         "lat": float
#     }
#     """
#     try:
#         driver_id = request.user.driver.id
#         lng = request.data.get('lng')
#         lat = request.data.get('lat')
        
#         # Validate required fields
#         if lng is None or lat is None:
#             logger.warning(f"Missing coordinates for driver {driver_id}")
#             return error_response(
#                 code='MISSING_FIELDS',
#                 message='Longitude and latitude are required',
#                 field='coordinates',
#                 issue='lng and lat must be provided',
#                 status=status.HTTP_400_BAD_REQUEST
#             )
        
#         # Dual-write: geo (spatial index) + stream (event log)
#         result = add_driver_location(driver_id,lat=lat,lng=lng)
#         if result.get('success'):
#             return success_response(
#                 {'message': result.get('message')},
#                 status.HTTP_200_OK
#             )
#         else:
#             logger.error(f"Failed to add driver location: {result.get('error')}")
#             return error_response(
#                 code='LOCATION_ERROR',
#                 message=result.get('error', 'Failed to add location'),
#                 field='coordinates',
#                 issue='Could not save driver location to cache',
#                 status=status.HTTP_400_BAD_REQUEST
#             )
    
#     except AttributeError as e:
#         logger.error(f"Driver profile error: {str(e)}")
#         return error_response(
#             code='PROFILE_ERROR',
#             message='Driver profile not found',
#             field='user',
#             issue='User does not have a driver profile',
#             status=status.HTTP_400_BAD_REQUEST
#         )
#     except Exception as e:
#         logger.error(f"Unexpected error adding driver location: {str(e)}")
#         return error_response(
#             code='INTERNAL_ERROR',
#             message='An unexpected error occurred',
#             field='general',
#             issue=str(e),
#             status=status.HTTP_500_INTERNAL_SERVER_ERROR
#         )


# @api_view(['POST'])
# @permission_classes([IsDriver])
# def update_location(request):
#     """
#     Update driver location.
    
#     Expected request data:
#     {
#         "lng": float,
#         "lat": float
#     }
#     """
#     try:
#         driver_id = request.user.driver.id
#         lng = request.data.get('lng')
#         lat = request.data.get('lat')
        
#         # Validate required fields
#         if lng is None or lat is None:
#             logger.warning(f"Missing coordinates for driver {driver_id}")
#             return error_response(
#                 code='MISSING_FIELDS',
#                 message='Longitude and latitude are required',
#                 field='coordinates',
#                 issue='lng and lat must be provided',
#                 status=status.HTTP_400_BAD_REQUEST
#             )
        
#         # Call Redis function
#         result = update_driver_location(driver_id, lng=lng, lat=lat)
#         return success_response(
#             {'message': 'Driver location updated successfully'},
#             status.HTTP_200_OK
#         )
        
    
#     except AttributeError as e:
#         logger.error(f"Driver profile error: {str(e)}")
#         return error_response(
#             code='PROFILE_ERROR',
#             message='Driver profile not found',
#             field='user',
#             issue='User does not have a driver profile',
#             status=status.HTTP_400_BAD_REQUEST
#         )
#     except Exception as e:
#         logger.error(f"Unexpected error adding driver location: {str(e)}")
#         return error_response(
#             code='INTERNAL_ERROR',
#             message='An unexpected error occurred',
#             field='general',
#             issue=str(e),
#             status=status.HTTP_500_INTERNAL_SERVER_ERROR
#         )
# @api_view(['DELETE'])
# @permission_classes([IsDriver])
# def remove_driver_view(request):
#     try:
#         driver_id = request.user.driver.id
#         result = remove_driver(driver_id)
#         return success_response(
#             {'message': 'Driver location removed successfully'},
#             status.HTTP_200_OK
#         )
#     except Exception as e:
#         logger.error(f"Unexpected error removing driver location: {str(e)}")
#         return error_response(
#             code='INTERNAL_ERROR',
#             message='An unexpected error occurred',
#             field='general',
#             issue=str(e),
#             status=status.HTTP_500_INTERNAL_SERVER_ERROR
#         )


# ── Driver Earnings ────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsDriver])
def driver_earnings(request):
    """Paginated settlement history for the driver.

    Thin adapter: the definition of an earning lives in `driver.earnings`.

    Previously this filtered `txn_type='credit'`, which excluded every cash trip
    -- cash settlement is recorded as a commission DEBIT -- so a cash-only driver
    saw an empty list. It also reported `commission: 0.0` on every row.

    Query params: ?page=1&page_size=10 (max 50); ?date=YYYY-MM-DD for one local day, or
    ?start=&end= for local days inclusive (11 Nov: Trips has the Earnings periods); ?q= a place
    or a trip number. Each row also carries the trip's places and ride class.
    """
    from rest_framework.pagination import PageNumberPagination

    from servers.driver.earnings import settlement_rows

    driver = request.user.driver
    since, until, bad = _local_days(request, driver)
    if bad:
        return bad
    rows = settlement_rows(driver, since=since, until=until)
    q = (request.query_params.get('q') or '').strip()
    if q:
        rows = [r for r in rows if r.trip_id in _trips_matching(driver, q)]

    paginator = PageNumberPagination()
    paginator.page_size = 10
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = 50
    page = paginator.paginate_queryset(rows, request)

    data = [row.to_api() for row in page]
    _add_places(data)
    logger.info('Driver settlement rows returned: %s', len(data))
    return success_response(paginator.get_paginated_response(data).data, status.HTTP_200_OK)


def _local_days(request, driver):
    """(since, until, error) for ?date= or ?start=&end= (YYYY-MM-DD, inclusive) in the driver's
    market's calendar, the days the earnings history uses; (None, None, None) when absent."""
    from datetime import date, datetime, time, timedelta

    from base.market_time import driver_timezone

    one = request.query_params.get('date')
    first = one or request.query_params.get('start')
    last = one or request.query_params.get('end')
    if not first and not last:
        return None, None, None
    tz = driver_timezone(driver)
    try:
        since = datetime.combine(date.fromisoformat(first), time.min, tzinfo=tz) if first else None
        until = datetime.combine(date.fromisoformat(last) + timedelta(days=1), time.min, tzinfo=tz) if last else None
    except ValueError:
        return None, None, error_response('INVALID_DATE', 'dates are YYYY-MM-DD', 'start', 'invalid',
                                          status.HTTP_400_BAD_REQUEST)
    return since, until, None


def _trips_matching(driver, q):
    """Ids of the driver's trips whose number is `q` (a leading # is fine) or whose pickup or
    drop contains it."""
    from django.db.models import Q

    from servers.ride.models import Trip

    match = Q(pickup_address__icontains=q) | Q(destination_address__icontains=q)
    number = q.lstrip('#').strip()
    if number.isdigit():
        match |= Q(id=int(number))
    return set(Trip.objects.filter(driver_id=driver).filter(match).values_list('id', flat=True))


def _add_places(data):
    """The trip's pickup, drop and ride class on each ledger row, in one query."""
    from servers.ride.models import Trip

    ids = {d['trip_id_val'] for d in data if d.get('trip_id_val')}
    trips = {t.id: t for t in Trip.objects.filter(id__in=ids).select_related(
        'requested_vehicle_type', 'vehicle_id__vehicle_type_id')}
    for d in data:
        t = trips.get(d.get('trip_id_val'))
        vt = None
        if t is not None:
            vt = (t.vehicle_id.vehicle_type_id if t.vehicle_id else None) or t.requested_vehicle_type
        d['pickup_address'] = t.pickup_address if t else None
        d['destination_address'] = t.destination_address if t else None
        d['ride_class'] = str(vt) if vt else None


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_earnings_daily(request):
    """GET /driver/earnings/daily/?days=7  The driver's last N local days, oldest first.

    Read-only aggregation of the same settlement ledger the summary reads (2026-11-06), so a
    day's figures add up to the summary's. Days are the driver's market's calendar days; a day
    with no settled trip is present with zeros, so the chart has every bar. `days` is 1-31,
    default 7. Amounts are decimal strings in the driver's currency.
    """
    from datetime import timedelta

    from base.market_time import driver_timezone, local_day_start
    from servers.driver.earnings import settlement_rows, summarise

    try:
        days = int(request.query_params.get('days', 7))
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 31))

    driver = request.user.driver
    tz = driver_timezone(driver)
    today_start = local_day_start(tz)
    starts = [today_start - timedelta(days=i) for i in range(days - 1, -1, -1)]
    rows = settlement_rows(driver, since=starts[0])

    out = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else None
        day_rows = [r for r in rows if r.created_at is not None and r.created_at >= start
                    and (end is None or r.created_at < end)]
        t = summarise(day_rows)
        out.append({
            'date': start.date().isoformat(),
            'trips': t['total_trips'],
            'gross': str(t['total_gross']),
            'commission': str(t['total_commission']),
            'net': str(t['total_net']),
        })
    # Additive (9 Nov): the day's platform fee under a driver fee plan, with why ("Today: 12 trips x
    # Rs 10 = Rs 120 (cap Rs 149)"). Past days from the ledger's daily row, today as worked out so far.
    _add_platform_fee(driver, out)
    # Additive (2026-11-08): what the amounts are in and whose days these are, so a driver with
    # no trips yet still gets a chart that reads "Mon 2 ... ₹0.00" without another call.
    from servers.driver.services import driver_currency
    return success_response({
        'days': out, 'currency': driver_currency(driver), 'timezone': getattr(tz, 'key', str(tz)),
    }, status.HTTP_200_OK)


def _add_platform_fee(driver, days):
    from base.money import from_minor
    from servers.pricing.fee_plan_models import DriverDailyFee
    from servers.pricing.fee_plans import today_so_far
    from servers.payments.payout_config import driver_payout_country

    try:
        country = driver_payout_country(driver) or 'IN'
        fees = {r.day.isoformat(): r for r in DriverDailyFee.objects.filter(
            driver=driver, day__gte=days[0]['date'])} if days else {}
        today = today_so_far(driver, country)
        for i, d in enumerate(days):
            row = fees.get(d['date'])
            if row is not None:
                d['platform_fee'] = str(from_minor(row.fee_minor, row.currency))
                d['platform_fee_note'] = row.explanation
            elif i == len(days) - 1 and today is not None:
                d['platform_fee'] = str(from_minor(today['fee_minor'], today['currency']))
                d['platform_fee_note'] = today['explanation']
                d['platform_fee_so_far'] = True
    except Exception:  # noqa: BLE001 - the earnings page shows without the fee line
        import logging
        logging.getLogger(__name__).exception('platform fee line failed driver=%s', driver.id)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_fee_plan(request):
    """GET /driver/fee-plan/  The plan in force for the driver's home city, today's fee so far and
    the values the "How our fee works" sheet is written from."""
    from servers.pricing.fee_plan_workflow import plan_dict
    from servers.pricing.fee_plans import local_day, resolve, today_so_far
    from servers.payments.payout_config import driver_payout_country
    from servers.payments.payout_scope import scope_codes_of_node

    driver = request.user.driver
    country = driver_payout_country(driver) or 'IN'
    home = getattr(driver, 'home_city', None)
    plan = resolve(country, scope_codes_of_node(home) if home else [], local_day(country))
    vt = getattr(getattr(driver.active_vehicle, 'vehicle_type_id', None), 'type', '')         if driver.active_vehicle_id else ''
    body = {'plan': plan_dict(plan) if plan else None,
            'applies_to_my_class': bool(plan and plan.amounts_for(vt)),
            'my_class': vt, 'today': today_so_far(driver, country)}
    if plan:
        body['my_amounts'] = plan.amounts_for(vt)
    return success_response(body, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_earnings_period(request):
    """GET /driver/earnings/period/?period=today|week|month  Totals for one local period.

    Read-only, over the same settlement ledger as /summary/ and /daily/ (2026-11-08), so the
    figures add up to theirs. The week is Monday to Sunday and the month the calendar month,
    both in the driver's market time zone; `start` and `end` are the period's first and last
    local dates, for labels like "Mon 2 - Sun 8 Nov". `net` = `gross` - `commission`, as the
    ledger settled it. Tips do not exist (owner ruling D5), so none are reported.
    """
    from datetime import timedelta

    from base.market_time import driver_timezone, local_day_start, local_month_start
    from servers.driver.earnings import settlement_rows, summarise
    from servers.driver.services import driver_currency

    period = request.query_params.get('period', 'week')
    if period not in ('today', 'week', 'month'):
        return error_response('INVALID_PERIOD', 'period must be today, week or month',
                              'period', 'invalid', status.HTTP_400_BAD_REQUEST)

    driver = request.user.driver
    tz = driver_timezone(driver)
    today = local_day_start(tz)
    if period == 'today':
        start, last = today, today.date()
    elif period == 'week':
        start = today - timedelta(days=today.weekday())
        last = start.date() + timedelta(days=6)
    else:
        start = local_month_start(tz)
        nxt = (start.date().replace(day=28) + timedelta(days=4)).replace(day=1)
        last = nxt - timedelta(days=1)

    t = summarise(settlement_rows(driver, since=start))
    return success_response({
        'period': period,
        'start': start.date().isoformat(),
        'end': last.isoformat(),
        'timezone': getattr(tz, 'key', str(tz)),
        'currency': driver_currency(driver),
        'trips': t['total_trips'],
        'gross': str(t['total_gross']),
        'commission': str(t['total_commission']),
        'net': str(t['total_net']),
        'cash_collected': str(t['cash_collected']),
        'unresolved_trips': t['unresolved_trips'],
    }, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_earnings_summary(request):
    """Aggregate earnings for the driver.

    Thin adapter over `driver.earnings`. Three corrections live in that module
    and are visible in this response:

    * cash trips are counted at all (they were not);
    * `total_earned` is the GROSS fare, which is what the driver app's own
      `netEarned = total_earned - total_commission` requires -- summing the
      credit rows put the NET there and deducted commission twice;
    * `commission_percent` is the rate actually charged, derived from the
      settlement ledger, not a hardcoded 20.

    Response keys are unchanged; new ones are added alongside.
    """
    from django.utils import timezone

    from servers.driver.earnings import settlement_rows, summarise
    from servers.driver.services import driver_currency
    from servers.promotions.incentives import incentive_lines
    from servers.rider.models import Wallet

    driver = request.user.driver
    rows = settlement_rows(driver)
    totals = summarise(rows)

    # Local calendar day, matching what a driver means by "today" -- in THEIR
    # market, not the deployment's.
    #
    # This used to be `timezone.localtime(...)`, which resolves against the
    # single global TIME_ZONE (Asia/Kolkata). For a driver in New York that
    # window opened at 14:30 the previous afternoon, so the morning's rides were
    # filed under yesterday and the figure they reconcile their cash against did
    # not match the shift they had just worked. Nothing crashed; the number was
    # simply about a different day than the person reading it was living in.
    from base.market_time import driver_timezone, local_day_start

    today_start = local_day_start(driver_timezone(driver))
    today = summarise([r for r in rows
                       if r.created_at is not None and r.created_at >= today_start])

    wallet = Wallet.objects.filter(
        user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER,
    ).first()
    wallet_balance = wallet.balance if wallet and wallet.balance is not None else 0

    return success_response({
        # Existing contract.
        'total_earned': str(totals['total_gross']),
        'total_commission': str(totals['total_commission']),
        'total_trips': totals['total_trips'],
        'today_earned': str(today['total_gross']),
        'today_trips': today['total_trips'],
        # THE FEE RULE (AUDIT 27.43): the percent the settlements applied, from the
        # settlement rows themselves. When trips were settled at different rates it
        # is the effective rate and `commission_percent_mixed` says so.
        'commission_percent': str(
            totals['settled_commission_percent']
            if totals['settled_commission_percent'] is not None
            else totals['effective_commission_percent']),
        'commission_percent_mixed': totals['settled_commission_percents'] is not None,
        'wallet_balance': str(wallet_balance),
        # Additive (2026-11-08): the currency of every amount here, so a driver with no
        # settled trips yet sees "₹0.00" rather than an amount the app cannot label.
        'currency': driver_currency(driver),
        # Additions. `cash_collected` is the number a driver reconciles against
        # at the end of a shift; the wallet balance goes negative by the
        # commission they owe on it, which is not the same thing.
        'total_net': str(totals['total_net']),
        'today_net': str(today['total_net']),
        'today_commission': str(today['total_commission']),
        'cash_collected': str(totals['cash_collected']),
        'unresolved_trips': totals['unresolved_trips'],
        # Module 12: rider promos are paid by SaaradhiGo, shown as their own line.
        'promo_paid_by_platform': str(totals['promo_paid_by_platform']),
        'today_promo_paid_by_platform': str(today['promo_paid_by_platform']),
        'promo_paid_label': 'Promo paid by SaaradhiGo',
        # US pass-through (AUDIT 27.62 item 1): sales tax and the regulatory fee are collected from
        # the rider and remitted by SaaradhiGo. NOT in `total_earned`, `total_net` or commission; on a
        # cash trip they are in `cash_collected` (the driver holds them) and owed back.
        'tax_collected': str(totals['tax_collected']),
        'regulatory_fees_collected': str(totals['regulatory_fees_collected']),
        'tax_collected_label': 'Sales tax collected (remitted by SaaradhiGo)',
        # Module 12: incentive and referral rewards are their own lines. They are NOT in
        # `total_earned` (that is trip fares) and carry no commission. Paid in the weekly payout.
        **incentive_lines(driver),
    }, status.HTTP_200_OK)


# ── Driver Withdrawals ──────────────────────────────────

@api_view(['GET'])
@permission_classes([IsDriver])
def driver_withdrawal_balance(request):
    """
    GET /api/driver/withdrawals/balance/
    Returns available balance, block status, fee percentage.
    """
    from .services import (
        calculate_available_balance, driver_currency, get_withdrawal_block_remaining, is_withdrawal_blocked,
    )
    from servers.payments.payout_config import (
        NOT_AVAILABLE_MESSAGE, config_for_driver, fee_display, fee_percent, minimum_withdrawal,
    )

    driver = request.user.driver
    available = calculate_available_balance(driver)
    blocked = is_withdrawal_blocked(driver)
    block_remaining = get_withdrawal_block_remaining(driver)
    # The fee and the minimum come from the driver's country (PayoutCountryConfig),
    # not from literals. A country with no row keeps the balance readable and
    # says so; it never borrows another country's numbers.
    _country, config = config_for_driver(driver)

    response_data = {
        'available_balance': float(available),
        'blocked': blocked,
        'block_remaining_seconds': block_remaining.total_seconds() if block_remaining else None,
        # THE FEE RULE: this is the percentage `withdrawal_fee` charges, read
        # from the same config value; `fee_display` is None when there is no
        # fee, so the app hides the line.
        'fee_percent': float(fee_percent(config)) if config else None,
        'fee_display': fee_display(config) if config else None,
        'minimum_withdrawal': float(minimum_withdrawal(config)) if config else None,
        # The currency the amounts above are in (the driver's country), so the
        # app never has to choose a symbol.
        'currency': config.currency if config else driver_currency(driver),
    }
    # Owner ruling 2026-11-09: paid automatically on a cycle; Withdraw now only where the
    # driver's city has instant cash-out on, at the provider's own fee.
    from base.money import to_minor as _to_minor
    from servers.payments.payout_scope import next_payout
    response_data['next_payout'] = (
        next_payout(driver, config, _to_minor(available, config.currency)) if config else None)
    from servers.payments.payout_scope import instant_ready
    response_data['instant'] = ({
        'available': instant_ready(config),
        'provider': config.instant_fee_provider or None,
        'saaradhigo_fee': '0',
    } if config else {'available': False, 'provider': None, 'saaradhigo_fee': '0'})
    # Payout details are checked before the country's config: a driver can fix them.
    from servers.payments.payout_destination import MISSING_MESSAGE, has_payout_destination
    response_data['payout_details_on_file'] = has_payout_destination(driver)
    if not response_data['payout_details_on_file']:
        response_data['withdrawals_available'] = False
        response_data['message'] = MISSING_MESSAGE
    if config is None:
        response_data['withdrawals_available'] = False
        response_data['message'] = NOT_AVAILABLE_MESSAGE
    return success_response(response_data, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsDriver])
def driver_withdrawal_request(request):
    """
    POST /api/driver/withdrawals/request/
    Creates withdrawal request after validation.
    """
    from .services import (
        calculate_available_balance,
        validate_withdrawal_amount,
        is_withdrawal_blocked,
    )
    from .models import WithdrawalRequest
    from .serializers import WithdrawalRequestCreateSerializer
    from django.utils import timezone
    from servers.payments.payout_config import (
        NOT_AVAILABLE_MESSAGE, config_for_driver, withdrawal_fee,
    )

    driver = request.user.driver

    # A country with no payout configuration cannot withdraw yet (the owner
    # decides its fee and minimum). Refused before anything is read or locked.
    _country, payout_config = config_for_driver(driver)
    if payout_config is None:
        return error_response(
            code='WITHDRAWALS_NOT_AVAILABLE',
            message=NOT_AVAILABLE_MESSAGE,
            field='withdrawal',
            issue='No payout configuration exists for your country',
            status=status.HTTP_400_BAD_REQUEST
        )
    # Withdraw now (owner ruling 2026-11-09): only where instant is on for the driver's city;
    # the fee is the provider's own (`instant_fee`), SaaradhiGo's stays 0.
    instant = bool(request.data.get('instant'))
    from servers.payments.payout_scope import instant_ready
    if instant and not instant_ready(payout_config):
        return error_response(
            code='INSTANT_NOT_AVAILABLE', message='Withdraw now is not available in your city.',
            field='instant', issue='Instant cash-out is off here',
            status=status.HTTP_400_BAD_REQUEST)

    # A full payout hold (a suspended driver, serious fraud, a second admin, a maximum length):
    # nothing is paid out meanwhile. Neutral wording: nothing about why.
    from servers.fraud import money as fraud_money

    from servers.payments.payout_conflict import has_open_conflict

    # A payout waiting for a Finance decision (AUDIT 27.75) holds every other payout too.
    if fraud_money.active_payout_hold(driver) is not None or has_open_conflict(driver.id):
        return error_response(
            code='PAYOUT_ON_HOLD',
            message='Your payouts are on hold right now. Please contact support.',
            field='withdrawal', issue='Payouts on hold',
            status=status.HTTP_400_BAD_REQUEST)

    # Nowhere to send the money: refused before anything is locked (owner, 2026-11-03).
    from servers.payments.payout_destination import (
        MISSING_CODE, MISSING_MESSAGE, has_payout_destination,
    )
    if not has_payout_destination(driver):
        return error_response(
            code=MISSING_CODE, message=MISSING_MESSAGE, field='payout_details',
            issue='No UPI ID or bank account on file',
            status=status.HTTP_400_BAD_REQUEST)
    from servers.payments.payout_destination import HELD_CODE, held_message, payout_hold_until
    held = payout_hold_until(driver)
    if held is not None:
        return error_response(code=HELD_CODE, message=held_message(held), field='payout_details',
                              issue='Payout details changed in the last 24 hours',
                              status=status.HTTP_400_BAD_REQUEST)

    # Validate block period (an instant withdrawal is the driver's own early cash-out: the
    # provider fee is its price, so the 7-day block does not apply to it).
    if not instant and is_withdrawal_blocked(driver):
        from .services import get_withdrawal_block_remaining, format_block_remaining
        remaining = get_withdrawal_block_remaining(driver)
        formatted = format_block_remaining(remaining)
        return error_response(
            code='WITHDRAWAL_BLOCKED',
            message='You cannot withdraw within 7 days of your last withdrawal',
            field='withdrawal',
            issue=f'Block period ends in {formatted}',
            status=status.HTTP_400_BAD_REQUEST
        )

    serializer = WithdrawalRequestCreateSerializer(data=request.data)
    if not serializer.is_valid():
        return error_response(
            code='VALIDATION_ERROR',
            message='Invalid withdrawal data',
            field=list(serializer.errors.keys())[0],
            issue=str(serializer.errors),
            status=status.HTTP_400_BAD_REQUEST
        )

    amount = serializer.validated_data['amount']

    from django.db import transaction
    from servers.rider.models import Wallet, WalletTransaction
    from decimal import Decimal
    import uuid

    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(
            user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER,
        )
        if wallet.balance is None:
            wallet.balance = Decimal('0.00')
            
        # Minimum and balance both come from `validate_withdrawal_amount`,
        # which resolves the threshold and the currency symbol from the
        # driver's market. This block used to be a hardcoded `amount < 500`
        # with the message "Minimum withdrawal amount is ₹500" -- so a US
        # driver was blocked below $500 and shown a rupee sign beside a dollar
        # balance -- while the market-aware function was imported above and
        # never called.
        #
        # The locked balance is passed in rather than re-read: this
        # transaction holds SELECT FOR UPDATE on the wallet row, and the
        # amount validated must be the amount about to be debited.
        # A reward pending review is part of the wallet but not of what can be withdrawn.
        from base.money import from_minor as _from_minor
        free_minor = max(0, wallet.balance_minor
                         - fraud_money.held_minor(driver.user_id, wallet.currency))
        ok, reason = validate_withdrawal_amount(
            driver, amount, available=_from_minor(free_minor, wallet.currency),
        )
        if not ok:
            return error_response(
                code='INSUFFICIENT_BALANCE',
                message=reason,
                field='amount',
                issue=reason,
                status=status.HTTP_400_BAD_REQUEST
            )

        # The daily Withdraw now limit (10 Nov), checked under the wallet lock so two requests at
        # once cannot both fit. The same reason the confirmation screen already showed.
        if instant:
            from base.money import to_minor as _to_minor
            from servers.payments.payout_scope import instant_limit
            limit = instant_limit(driver, payout_config, _to_minor(amount, payout_config.currency))
            if limit is not None:
                return error_response(
                    code=limit['code'], message=limit['message'], field='amount',
                    issue=limit['message'], status=status.HTTP_400_BAD_REQUEST)

        # Create withdrawal request. The fee is stamped from the ONE function
        # that also feeds the balance endpoint's `fee_percent`.
        withdrawal = WithdrawalRequest.objects.create(
            driver=driver,
            amount=amount,
            status='pending',
            origin='instant' if instant else 'driver',
        )
        # A recovery Finance approved comes off this payout: at most `recovery_max_percent` of it,
        # never below the country's minimum; the rest carries to the next payout.
        gross_minor = withdrawal.amount_minor
        deduct_minor, deduct_plan = fraud_money.plan_deduction(
            driver, gross_minor, payout_config.minimum_withdrawal_minor,
            payout_config.recovery_max_percent)
        if deduct_minor:
            withdrawal.amount_minor = gross_minor - deduct_minor
            withdrawal.save(update_fields=['amount_minor'])
        from servers.payments.payout_scope import instant_fee
        fee_minor = (instant_fee(payout_config, withdrawal.amount_minor) if instant
                     else withdrawal_fee(payout_config, withdrawal.amount_minor))
        if fee_minor >= withdrawal.amount_minor:
            transaction.set_rollback(True)
            return error_response(
                code='AMOUNT_BELOW_FEE',
                message='The amount is not more than the transfer fee.',
                field='amount', issue='Choose a larger amount',
                status=status.HTTP_400_BAD_REQUEST)
        if fee_minor:
            withdrawal.fee_minor = fee_minor
            withdrawal.save(update_fields=['fee_minor'])
        
        # Instantly deduct from wallet
        idempotency_key = f"withdrawal_{withdrawal.id}_{uuid.uuid4().hex[:16]}"
        WalletTransaction.objects.create(
            user_id=driver.user_id,
            amount_minor=withdrawal.amount_minor,
            txn_type='debit',
            status='pending',
            purpose='withdrawal',
            reference_id=f"withdrawal_{withdrawal.id}",
            idempotency_key=idempotency_key
        )
        wallet.balance_minor -= gross_minor
        wallet.save(update_fields=['balance_minor'])
        if deduct_plan:
            fraud_money.apply_deduction(driver, deduct_plan, withdrawal, gross_minor,
                                        payout_config.recovery_max_percent)

    # Withdraw now (9 Nov): the driver's own early cash-out of their own balance, after the hold and
    # conflict checks above, is sent at once at the provider fee; a scheduled or on-request payout
    # still waits for Finance.
    if instant:
        WithdrawalRequest.objects.filter(pk=withdrawal.pk, status='pending').update(status='approved')
        withdrawal.refresh_from_db()
        from .services import trigger_payout_creation
        trigger_payout_creation(withdrawal)
        withdrawal.refresh_from_db()

    # Return created withdrawal
    from .serializers import WithdrawalRequestSerializer
    withdrawal_serializer = WithdrawalRequestSerializer(withdrawal)
    return success_response(withdrawal_serializer.data, status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_withdrawal_history(request):
    """
    GET /api/driver/withdrawals/history/
    Lists driver's withdrawal requests with status.
    """
    from .models import WithdrawalRequest
    from .serializers import WithdrawalRequestSerializer
    from rest_framework.pagination import PageNumberPagination

    driver = request.user.driver
    withdrawals = WithdrawalRequest.objects.filter(driver=driver).order_by('-requested_at')
    # 11 Nov: Payouts has the Earnings periods (?start=&end=, local days) and a search (?q=)
    # by payout number, reference or amount.
    since, until, bad = _local_days(request, driver)
    if bad:
        return bad
    if since is not None:
        withdrawals = withdrawals.filter(requested_at__gte=since)
    if until is not None:
        withdrawals = withdrawals.filter(requested_at__lt=until)
    q = (request.query_params.get('q') or '').strip().lstrip('#').strip()
    if q:
        from decimal import Decimal, InvalidOperation

        from django.db.models import Q

        match = Q(payout_reference_id__icontains=q)
        if q.isdigit():
            match |= Q(id=int(q))
        try:
            match |= Q(amount_minor=int(Decimal(q.replace(',', '')) * 100))
        except (InvalidOperation, ValueError):
            pass
        withdrawals = withdrawals.filter(match)

    paginator = PageNumberPagination()
    paginator.page_size = 10
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = 50
    page = paginator.paginate_queryset(withdrawals, request)
    serializer = WithdrawalRequestSerializer(page, many=True)
    return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_withdrawal_block_status(request):
    """
    GET /api/driver/withdrawals/block-status/
    Returns block end time and countdown.
    """
    from .services import is_withdrawal_blocked, get_withdrawal_block_remaining, get_block_end_time, format_block_remaining
    from django.utils import timezone
    from datetime import timedelta

    driver = request.user.driver
    blocked = is_withdrawal_blocked(driver)
    remaining = get_withdrawal_block_remaining(driver)
    last_withdrawal = driver.last_withdrawal_at
    block_end = get_block_end_time(driver)
    formatted_remaining = format_block_remaining(remaining)

    response_data = {
        'blocked': blocked,
        'last_withdrawal_at': last_withdrawal.isoformat() if last_withdrawal else None,
        'block_end_at': block_end.isoformat() if block_end else None,
        'remaining_seconds': remaining.total_seconds() if remaining else None,
        'remaining_human': formatted_remaining,
        'can_withdraw': not blocked,
    }
    return success_response(response_data, status.HTTP_200_OK)


# ── Vehicle CRUD ────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsDriver])
def list_vehicles(request):
    """List all vehicles belonging to the authenticated driver."""
    from .models import Vehicle
    from .serializers import VehicleSerializer

    driver = request.user.driver
    vehicles = Vehicle.objects.filter(driver_id=driver).select_related('vehicle_type_id')
    serializer = VehicleSerializer(vehicles, many=True, context=_own_documents(request))
    return success_response(serializer.data, status.HTTP_200_OK)


#: What identifies the car on the rider's screen; changing any of it is reviewed again.
VEHICLE_IDENTITY = ('vehicle_type', 'brand', 'model', 'color', 'vehicle_number', 'year')


def _send_to_review(driver):
    """Back into the KYC queue as "In review"; may_operate stays off until approved (10 Nov)."""
    driver.doc_status = 'pending'
    driver.approved = False
    driver.doc_rejection_reason = None
    driver.doc_status_updated_at = timezone.now()
    driver.save(update_fields=['doc_status', 'approved', 'doc_rejection_reason', 'doc_status_updated_at'])


@api_view(['POST'])
@permission_classes([IsDriver])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def create_vehicle(request):
    """
    Add a new vehicle for the driver.

    Expected: {
        "vehicle_number": str,
        "vehicle_type": str (e.g. "sedan"),
        "brand": str (optional),
        "model": str (optional),
        "color": str (optional),
        "year": int (optional),
        "capacity": int (optional, default 1)
    }
    """
    from .models import Vehicle, VehicleType
    from .serializers import VehicleCreateSerializer, VehicleSerializer

    # Document keys must be this driver's own uploads (AUDIT 27.78, Low 5).
    serializer = VehicleCreateSerializer(data=request.data,
                                         context={'upload_owner': request.user})
    if not serializer.is_valid():
        return error_response(
            code='VALIDATION_ERROR',
            message='Invalid vehicle data',
            field=list(serializer.errors.keys())[0],
            issue=str(serializer.errors),
            status=status.HTTP_400_BAD_REQUEST
        )

    data = serializer.validated_data
    driver = request.user.driver
    vt = VehicleType.objects.get(type=data['vehicle_type'])

    try:
        vehicle = Vehicle(
            driver_id=driver,
            vehicle_type_id=vt,
            vehicle_number=data['vehicle_number'],
            brand=data.get('brand', ''),
            model=data.get('model', ''),
            color=data.get('color', ''),
            year=data.get('year'),
            capacity=data.get('capacity', 1),
            rc_doc=data.get('rc_doc'),
            permit_doc=data.get('permit_doc'),
            insurance_doc=data.get('insurance_doc'),
            fitness_doc=data.get('fitness_doc'),
            puc_doc=data.get('puc_doc'),
            inspection_doc=data.get('inspection_doc'),
            insurance_expiry=data.get('insurance_expiry'),
            permit_expiry=data.get('permit_expiry'),
            fitness_expiry=data.get('fitness_expiry'),
            puc_expiry=data.get('puc_expiry'),
            vehicle_pic=data.get('vehicle_pic'),
        )
        from .vehicle_rules import VehicleProblem, apply_identity
        try:
            apply_identity(driver, vehicle, request.data, vt.type)
        except VehicleProblem as exc:
            return error_response(code=exc.code, message=exc.message, field=exc.field, issue=exc.message,
                                  status=status.HTTP_400_BAD_REQUEST)
        vehicle.full_clean()
        _, storage_error = save_with_storage_errors(
            vehicle.save, what='vehicle creation')
        if storage_error is not None:
            return storage_error
        if driver.active_vehicle_id is None:
            driver.active_vehicle = vehicle
            driver.save(update_fields=['active_vehicle'])
        _send_to_review(driver)
    except ValidationError as e:
        return error_response(
            code='VALIDATION_ERROR',
            message='Invalid vehicle data',
            field='vehicle',
            issue=str(e.message_dict if hasattr(e, 'message_dict') else e.messages),
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        return error_response(
            code='CREATE_ERROR',
            message='Failed to create vehicle',
            field='vehicle',
            issue=str(e),
            status=status.HTTP_400_BAD_REQUEST
        )

    return success_response(
        VehicleSerializer(vehicle, context=_own_documents(request)).data,
        status.HTTP_201_CREATED
    )


@api_view(['PATCH'])
@permission_classes([IsDriver])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def update_vehicle(request, vehicle_id):
    """Update a vehicle belonging to the driver."""
    from .models import Vehicle
    from .serializers import VehicleSerializer

    driver = request.user.driver
    was_rejected = driver.doc_status == "rejected"
    try:
        vehicle = Vehicle.objects.get(id=vehicle_id, driver_id=driver)
    except Vehicle.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Vehicle not found',
            field='vehicle_id',
            issue=f'Vehicle {vehicle_id} not found or does not belong to you',
            status=status.HTTP_404_NOT_FOUND
        )

    files_set = []
    for field_name in ('rc_doc', 'permit_doc', 'insurance_doc', 'fitness_doc',
                       'puc_doc', 'inspection_doc', 'vehicle_pic',
                       'photo_back', 'photo_left', 'photo_right', 'photo_interior'):
        field_provided, field_value, field_error = resolve_file_input(
            request, field_name, kind=field_name
        )
        if field_error:
            return error_response(
                code='UPLOAD_FAILED',
                message=field_error,
                field=field_name,
                issue=field_error,
                status=status.HTTP_400_BAD_REQUEST
            )
        if field_provided:
            setattr(vehicle, field_name, field_value)
            files_set.append(field_name)

    allowed_fields = ['brand', 'model', 'color', 'year', 'capacity', 'vehicle_number', 'insurance_expiry', 'permit_expiry','fitness_expiry','puc_expiry',]
    # An upload alone writes only its own column (10 Nov v2): two uploads at once used to save the
    # whole row each, and the later one put the earlier file back to what it was before.
    only_files = bool(files_set) and not any(
        k in request.data for k in (*allowed_fields, 'vehicle_type', 'catalogue_entry', 'custom'))
    before = {f: str(getattr(vehicle, f) or '') for f in VEHICLE_IDENTITY if f != 'vehicle_type'}
    before['vehicle_type'] = vehicle.vehicle_type_id_id
    for field in allowed_fields:
        if field in request.data:
            setattr(vehicle, field, request.data[field])
    if request.data.get('vehicle_type'):
        from .models import VehicleType
        vt = VehicleType.objects.filter(type=request.data['vehicle_type']).first()
        if vt is None:
            return error_response(code='VALIDATION_ERROR', message='Unknown vehicle type',
                                  field='vehicle_type', issue='Not in the catalogue',
                                  status=status.HTTP_400_BAD_REQUEST)
        vehicle.vehicle_type_id = vt
    from .vehicle_rules import VehicleProblem, apply_identity
    try:
        apply_identity(driver, vehicle, {k: request.data[k] for k in (
            'catalogue_entry', 'custom', 'brand', 'model', 'year', 'vehicle_number') if k in request.data},
            vehicle.vehicle_type_id.type)
    except VehicleProblem as exc:
        return error_response(code=exc.code, message=exc.message, field=exc.field, issue=exc.message,
                              status=status.HTTP_400_BAD_REQUEST)
    after = {f: str(getattr(vehicle, f) or '') for f in VEHICLE_IDENTITY if f != 'vehicle_type'}
    after['vehicle_type'] = vehicle.vehicle_type_id_id
    identity_changed = before != after

    try:
        vehicle.full_clean()
        # A refused write to the object store is not a bad request. See
        # base/storage_health.py.
        _, storage_error = save_with_storage_errors(
            (lambda: vehicle.save(update_fields=[*files_set, 'file_times'])) if only_files else vehicle.save,
            what='vehicle documents')
        if storage_error is not None:
            return storage_error
        if identity_changed:
            _send_to_review(driver)
        elif was_rejected:
            driver.doc_status = "pending"
            driver.approved = False
            driver.doc_rejection_reason = None
            driver.doc_status_updated_at = timezone.now()

            driver.save(update_fields=[
                "doc_status",
                "approved",
                "doc_rejection_reason",
                "doc_status_updated_at",
            ])

    except ValidationError as e:
        return error_response(
            code='VALIDATION_ERROR',
            message='Invalid vehicle data',
            field='vehicle',
            issue=str(e.message_dict if hasattr(e, 'message_dict') else e.messages),
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        return error_response(
            code='UPDATE_ERROR',
            message='Failed to update vehicle',
            field='vehicle',
            issue=str(e),
            status=status.HTTP_400_BAD_REQUEST
        )

    return success_response(VehicleSerializer(vehicle, context=_own_documents(request)).data,
                            status.HTTP_200_OK)


@api_view(['DELETE'])
@permission_classes([IsDriver])
def delete_vehicle(request, vehicle_id):
    """Delete a vehicle belonging to the driver."""
    from .models import Vehicle

    driver = request.user.driver
    try:
        vehicle = Vehicle.objects.get(id=vehicle_id, driver_id=driver)
    except Vehicle.DoesNotExist:
        return error_response(
            code='NOT_FOUND',
            message='Vehicle not found',
            field='vehicle_id',
            issue=f'Vehicle {vehicle_id} not found or does not belong to you',
            status=status.HTTP_404_NOT_FOUND
        )

    vehicle.delete()
    return success_response({'message': 'Vehicle deleted successfully'}, status.HTTP_200_OK)

@api_view(['PATCH'])
@permission_classes([IsDriver])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def update_driver_profile(request):
    """
    Get or update a driver profile.
    
    GET: Returns current profile data.
    PATCH: Updates profile data (e.g. active_vehicle, license_doc).
    """
    driver = request.user.driver
    update_data = {}
    was_rejected = driver.doc_status == "rejected"
    license_provided, license_value, license_error = resolve_file_input(
        request, 'license_doc', kind='license_doc'
    )
    if license_error:
        return error_response(
            code='UPLOAD_FAILED',
            message=license_error,
            field='license_doc',
            issue=license_error,
            status=status.HTTP_400_BAD_REQUEST
        )
    if license_provided:
        update_data['license_doc'] = license_value

    license_back_provided, license_back_value, license_back_error = resolve_file_input(
        request, 'license_doc_back', kind='license_doc_back'
    )
    if license_back_error:
        return error_response(
            code='UPLOAD_FAILED',
            message=license_back_error,
            field='license_doc_back',
            issue=license_back_error,
            status=status.HTTP_400_BAD_REQUEST
        )
    if license_back_provided:
        update_data['license_doc_back'] = license_back_value

    # MVAG 2025 records (9 Nov): the three documents and their details, through the same
    # upload path and the same reviewer queue as the licence.
    for doc_field in ('health_insurance_doc', 'term_insurance_doc', 'induction_training_certificate'):
        provided, value, err = resolve_file_input(request, doc_field, kind=doc_field)
        if err:
            return error_response(code='UPLOAD_FAILED', message=err, field=doc_field, issue=err,
                                  status=status.HTTP_400_BAD_REQUEST)
        if provided:
            update_data[doc_field] = value
    for text_field in ('health_insurance_policy_number', 'health_insurance_insurer',
                       'health_insurance_sum_insured', 'health_insurance_expiry',
                       'term_insurance_policy_number', 'term_insurance_insurer',
                       'term_insurance_sum_insured', 'term_insurance_expiry',
                       'induction_training_hours', 'induction_training_date'):
        if text_field in request.data:
            value = request.data.get(text_field)
            update_data[text_field] = None if value in EMPTY_FILE_VALUES else value

    if 'license_expiry' in request.data:
        license_expiry = request.data.get('license_expiry')
        update_data['license_expiry'] = None if license_expiry in EMPTY_FILE_VALUES else license_expiry

    if 'active_vehicle' in request.data:
        vehicle_id = request.data.get('active_vehicle')
        if vehicle_id in EMPTY_FILE_VALUES or vehicle_id is None:
            update_data['active_vehicle'] = None
        else:
            try:
                vehicle = Vehicle.objects.get(id=vehicle_id, driver_id=driver)
                update_data['active_vehicle'] = vehicle.id
            except Vehicle.DoesNotExist:
                return error_response(
                    code='NOT_FOUND',
                    message='Vehicle not found',
                    field='active_vehicle',
                    issue=f'Vehicle {vehicle_id} not found or does not belong to you',
                    status=status.HTTP_404_NOT_FOUND
                )

    if not update_data:
        return error_response(
            code='VALIDATION_ERROR',
            message='No driver profile data provided',
            field='data',
            issue='Provide at least one of active_vehicle, license_doc, license_doc_back, license_expiry '
                  'or an insurance or training record',
            status=status.HTTP_400_BAD_REQUEST
        )

    serializer = DriverProfileSerializer(driver, data=update_data, partial=True,
                                         context=_own_documents(request, upload_owner=request.user))
    if not serializer.is_valid():
        return error_response(
            code='VALIDATION_ERROR',
            message='Invalid driver profile data',
            field=list(serializer.errors.keys())[0],
            issue=str(serializer.errors),
            status=status.HTTP_400_BAD_REQUEST
        )

    # A refused write to the object store is not a bad request and must not
    # surface as "an unexpected error". See base/storage_health.py.
    _, storage_error = save_with_storage_errors(
        serializer.save, what='driver documents')
    if storage_error is not None:
        return storage_error

    if was_rejected:
        driver.doc_status = "pending"
        driver.approved = False
        driver.doc_rejection_reason = None
        driver.doc_status_updated_at = timezone.now()

        driver.save(update_fields=[
            "doc_status",
            "approved",
            "doc_rejection_reason",
            "doc_status_updated_at",
        ])

    return success_response(serializer.data, status.HTTP_200_OK)
@api_view(['GET'])
@permission_classes([IsDriver])
def get_driver_profile(request):
    """Get a driver profile."""
    driver = request.user.driver
    serializer = DriverProfileSerializer(driver, context=_own_documents(request))
    return success_response(serializer.data, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_incentives(request):
    """
    Get active incentives/quests and driver's progress.
    """
    from .models import IncentiveQuest
    from .serializers import IncentiveQuestSerializer
    from servers.ride.models import Trip
    from django.utils import timezone
    
    driver = request.user.driver
    now = timezone.now()
    active_quests = IncentiveQuest.objects.filter(is_active=True, start_date__lte=now, end_date__gte=now)
    
    # Calculate progress on the fly for each quest
    # (Trips completed by driver within quest timeframe)
    for quest in active_quests:
        completed_trips = Trip.objects.filter(
            driver_id=driver,
            status_id__status_code='completed',
            requested_at__gte=quest.start_date,
            requested_at__lte=quest.end_date
        ).count()
        quest.progress_trips = completed_trips
        quest.is_completed = completed_trips >= quest.target_trips

    serializer = IncentiveQuestSerializer(active_quests, many=True)
    return success_response(serializer.data, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_elite_status(request):
    """
    Get driver's elite tier status and metrics.
    Tiers based on total trips:
    - Silver: 0-99
    - Gold: 100-499
    - Platinum: 500-999
    - Diamond: 1000+
    """
    driver = request.user.driver
    total_trips = driver.total_trips
    
    tier = 'Silver'
    if total_trips >= 1000:
        tier = 'Diamond'
    elif total_trips >= 500:
        tier = 'Platinum'
    elif total_trips >= 100:
        tier = 'Gold'
        
    response_data = {
        'tier': tier,
        'total_trips': total_trips,
        'rating': str(driver.ratings),
        'acceptance_rate': '100%', # Mock/Placeholder for future real calculation
        'cancellation_rate': '0%' # Mock/Placeholder for future real calculation
    }
    return success_response(response_data, status.HTTP_200_OK)


def _own_documents(request, **extra):
    """Serializer context for the driver's OWN responses: KYC documents render
    as five-minute audited view links, not bucket URLs (AUDIT 27.78)."""
    return {'request': request, 'document_links': True, **extra}


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def driver_document_view(request, ticket):
    """GET /driver/driver/documents/view/<ticket>/  The driver opens their own document.

    The link comes from the driver's own profile or vehicle response; the
    ticket in it is signed, names this driver and this document, and expires
    in `document_access.url_ttl()` seconds, so it is the credential (an image
    widget cannot send a bearer token). It writes the audit row, then
    redirects to a bucket URL signed for the same short time. A ticket for a
    deactivated account, another driver's file or a replaced file opens
    nothing.
    """
    from django.contrib.auth import get_user_model
    from django.http import HttpResponseRedirect, JsonResponse

    from . import document_access
    from .models import Driver

    def refuse(status_code, code):
        return JsonResponse({'success': False, 'error': {'code': code}}, status=status_code)

    payload = document_access.read_ticket(ticket)
    if payload is None:
        return refuse(404, 'NOT_FOUND')
    user = get_user_model().objects.filter(pk=payload.get('u'), is_active=True).first()
    driver = Driver.objects.filter(pk=payload.get('d'), user_id=user).first() if user else None
    if driver is None:
        return refuse(404, 'NOT_FOUND')
    column = payload.get('c')
    field, vehicle = document_access.stored_document(driver, column, payload.get('v'))
    if field is None:
        return refuse(404, 'NOT_FOUND')
    if document_access.record_view(request, actor=user, driver=driver, key=column,
                                   vehicle=vehicle, via='driver_self') is None:
        return refuse(503, 'AUDIT_UNAVAILABLE')
    url = document_access.signed_url(field)
    if url is None:
        return refuse(503, 'DOCUMENT_UNAVAILABLE')
    response = HttpResponseRedirect(url)
    # The redirect target is a credential for five minutes: no caching, no
    # referrer to wherever the image is shown.
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_instant_quote(request):
    """GET /api/driver/withdrawals/instant/quote/?amount=  The Withdraw now confirmation:
    amount, the provider fee, "You receive", the bank (last 4) and the arrival time."""
    from decimal import Decimal, InvalidOperation

    from base.money import to_minor
    from servers.payments.payout_config import config_for_driver
    from servers.payments.payout_scope import instant_quote

    driver = request.user.driver
    _country, config = config_for_driver(driver)
    from servers.payments.payout_scope import instant_ready
    if not instant_ready(config):
        return error_response(
            code='INSTANT_NOT_AVAILABLE', message='Withdraw now is not available in your city.',
            field='instant', issue='Instant cash-out is off here',
            status=status.HTTP_400_BAD_REQUEST)
    try:
        amount = Decimal(str(request.query_params.get('amount')))
        if amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        return error_response(code='VALIDATION_ERROR', message='Enter an amount.',
                              field='amount', issue='amount must be a positive number',
                              status=status.HTTP_400_BAD_REQUEST)
    return success_response(instant_quote(driver, config, to_minor(amount, config.currency)),
                            status.HTTP_200_OK)
