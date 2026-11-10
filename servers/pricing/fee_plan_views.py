"""Pricing > Driver fee plans, the simulator and Reports > Cost per trip (9 Nov).

    GET    /pricing/admin/fee-plans/                    zone.read
    POST   /pricing/admin/fee-plans/                    pricing.write: a draft
    PATCH  /pricing/admin/fee-plans/<id>/               pricing.write: edit a draft
    POST   /pricing/admin/fee-plans/<id>/submit/        pricing.write -> 202 (maker-checker)
    POST   /pricing/admin/fee-plans/simulate/           zone.read: never activates anything
    GET    /pricing/admin/cost-per-trip/?country&month  finance.read
    PUT    /pricing/admin/fixed-costs/<country>/<YYYY-MM>/  finance.config
"""

from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Avg, Sum
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES, get_country
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_audit.services import record_admin_action
from servers.approvals.service import ApprovalError

from . import fee_plan_workflow as flow
from . import fee_plans
from .fee_plan_models import (
    DAILY_PASS, FAIR_SHARE, PER_TRIP_FEE, PERCENT_COMMISSION, DriverFeePlan, MonthlyFixedCost,
)

READ = ScopedPermission('zone.read')
WRITE = ScopedPermission('pricing.write')
FIN_READ = ScopedPermission('finance.read')
FIN_CONFIG = ScopedPermission('finance.config')


def _label(user):
    from servers.admin_audit.models import actor_snapshot

    return actor_snapshot(user).get('actor_label') or f'admin {getattr(user, "pk", "")}'


def _approval_body(outcome):
    row = outcome.request
    return {'approval_request': row.as_dict(), 'duplicate': outcome.duplicate,
            'state': 'pending_approval' if row.status == 'requested' else row.status}


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, READ])
def fee_plans_list(request):
    if request.method == 'POST':
        if not WRITE.has_permission(request, None):
            return error_response(code='PERMISSION_DENIED', message=WRITE.message, field='general',
                                  issue='pricing.write', status=status.HTTP_403_FORBIDDEN)
        try:
            plan = flow.create_draft(request.user, request.data or {}, label=_label(request.user))
        except ApprovalError as exc:
            return exc.response()
        return success_response(flow.plan_dict(plan), status.HTTP_201_CREATED)
    plans = DriverFeePlan.objects.order_by('country', 'scope_code', '-version')[:300]
    return success_response({'results': [flow.plan_dict(p) for p in plans],
                             'plan_types': [PERCENT_COMMISSION, PER_TRIP_FEE, DAILY_PASS, FAIR_SHARE],
                             'presets': fee_plans.PRESETS}, status.HTTP_200_OK)


def _plan_or_404(pk):
    return DriverFeePlan.objects.filter(pk=pk).first()


@api_view(['PATCH'])
@permission_classes([IsAuthenticated, WRITE])
def fee_plan_detail(request, plan_id):
    plan = _plan_or_404(plan_id)
    if plan is None:
        return error_response(code='NOT_FOUND', message='No such plan', field='id', issue='No such plan',
                              status=status.HTTP_404_NOT_FOUND)
    try:
        flow.update_draft(request.user, plan, request.data or {})
    except ApprovalError as exc:
        return exc.response()
    return success_response(flow.plan_dict(plan), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, WRITE])
def fee_plan_submit(request, plan_id):
    plan = _plan_or_404(plan_id)
    if plan is None:
        return error_response(code='NOT_FOUND', message='No such plan', field='id', issue='No such plan',
                              status=status.HTTP_404_NOT_FOUND)
    try:
        outcome = flow.submit(request.user, plan, reason=str((request.data or {}).get('reason') or ''),
                              http_request=request)
    except ApprovalError as exc:
        return exc.response()
    return success_response(_approval_body(outcome), status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# The simulator
# ---------------------------------------------------------------------------

def average_fare_minor(country, vehicle_type, days=30):
    from servers.ride.models import Trip

    since = date.today() - timedelta(days=days)
    avg = (Trip.objects.filter(status_id__status_code='completed', zone__country=country,
                               completed_at__date__gte=since,
                               requested_vehicle_type__type__iexact=vehicle_type)
           .aggregate(a=Avg('final_fare_minor'))['a'])
    return int(avg) if avg else None


def variable_cost_per_trip_minor(country):
    """Maps (metered, list price, at the console's display rate) + SMS + calling per trip, from
    config. Missing config counts 0 and is said so."""
    from servers.ride import maps_budget

    fx = Decimal(str(settings.MAPS_FX_TO_LOCAL.get(country) or 0))
    maps_usd = maps_budget.cost_per_completed_trip(country)
    if maps_usd is None:
        maps_usd = Decimal(str(getattr(settings, 'MAPS_BASELINE_USD_PER_TRIP', '0.0285')))
    maps_minor = int((maps_usd * fx * 100).quantize(Decimal('1')))
    sms = int(getattr(settings, 'COST_SMS_PER_TRIP_MINOR', {}).get(country) or 0)
    call = int(getattr(settings, 'COST_CALL_PER_TRIP_MINOR', {}).get(country) or 0)
    return {'maps_minor': maps_minor, 'sms_minor': sms, 'call_minor': call,
            'total_minor': maps_minor + sms + call,
            'missing': [k for k, v in (('COST_SMS_PER_TRIP_MINOR', sms),
                                       ('COST_CALL_PER_TRIP_MINOR', call)) if not v]}


def payment_fee_minor(fare_minor, country):
    pct = Decimal(str(getattr(settings, 'PAYMENT_GATEWAY_FEE_PERCENT', {}).get(country) or 0))
    share = Decimal(str(getattr(settings, 'ONLINE_PAYMENT_SHARE', {}).get(country) or 0))
    return int((Decimal(fare_minor) * pct / 100 * share).quantize(Decimal('1')))


@api_view(['POST'])
@permission_classes([IsAuthenticated, READ])
def fee_plan_simulate(request):
    """{country, vehicle_type, average_fare_minor?, commission_percent?, daily_pass_minor?,
    per_trip_fee_minor?, trips_per_day?} -> every plan type side by side at the three presets."""
    d = request.data or {}
    country = str(d.get('country') or 'IN').upper()
    vt = str(d.get('vehicle_type') or 'sedan').lower()
    avg = d.get('average_fare_minor')
    source = 'entered'
    if avg in (None, ''):
        avg, source = average_fare_minor(country, vt), 'last_30_days'
    if not avg:
        return error_response(code='NO_FARE_DATA', message='No completed trips in 30 days: enter an average fare.',
                              field='average_fare_minor', issue='no data', status=status.HTTP_400_BAD_REQUEST)
    avg = int(avg)
    costs = variable_cost_per_trip_minor(country)
    cost_per_trip = costs['total_minor'] + payment_fee_minor(avg, country)
    active = fee_plans.resolve(country, [], fee_plans.local_day(country))
    fair = (active if active is not None and active.plan_type == FAIR_SHARE else
            DriverFeePlan.objects.filter(country=country, plan_type=FAIR_SHARE).order_by('-version').first())
    plans = []
    if fair is not None:
        plans.append(('fair_share', fair, {}))
    pct = d.get('commission_percent', 10)
    plans.append(('percent_commission', DriverFeePlan(country=country, plan_type=PERCENT_COMMISSION,
                                                      amounts={vt: {'commission_percent': float(pct)}}),
                  {'commission_percent': pct}))
    plans.append(('daily_pass', DriverFeePlan(country=country, plan_type=DAILY_PASS,
                                              amounts={vt: {'daily_pass_minor': int(d.get('daily_pass_minor') or 19900)}}),
                  {}))
    if d.get('per_trip_fee_minor'):
        plans.append(('per_trip_fee', DriverFeePlan(country=country, plan_type=PER_TRIP_FEE,
                                                    amounts={vt: {'per_trip_fee_minor': int(d['per_trip_fee_minor'])}}),
                      {}))
    for _, p, _ in plans:
        if not p.currency:
            p.currency = get_country(country).currency
    presets = dict(fee_plans.PRESETS)
    if d.get('trips_per_day'):
        presets['custom'] = int(d['trips_per_day'])
    rows = []
    for name, plan, extra in plans:
        for preset, n in presets.items():
            r = fee_plans.simulate(plan, vt, n, avg, cost_per_trip_minor=cost_per_trip,
                                   commission_percent=extra.get('commission_percent'))
            rows.append({'plan': name, 'preset': preset, **r})
    return success_response({'country': country, 'vehicle_type': vt, 'average_fare_minor': avg,
                             'average_fare_source': source, 'cost_per_trip_minor': cost_per_trip,
                             'cost_breakdown': costs, 'currency': plans[0][1].currency, 'rows': rows},
                            status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Open books: cost per trip (B4)
# ---------------------------------------------------------------------------

def cost_per_trip_report(country, month):
    from servers.callbridge.models import CallSession
    from servers.payments.models import TripSettlement
    from servers.ride.models import Trip
    from servers.rider.models import WalletTransaction
    from servers.sharing.models import ContactSms

    end = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    trips = Trip.objects.filter(status_id__status_code='completed', zone__country=country,
                                completed_at__date__gte=month, completed_at__date__lt=end)
    n = trips.count()
    commission = (TripSettlement.objects.filter(trip__in=trips)
                  .aggregate(s=Sum('commission_amount_minor'))['s'] or 0)
    platform_fee = (WalletTransaction.objects.filter(purpose='platform_fee', status='completed',
                                                     created_at__date__gte=month, created_at__date__lt=end,
                                                     currency=get_country(country).currency)
                    .aggregate(s=Sum('amount_minor'))['s'] or 0)
    rider_fee = 0
    try:
        from servers.ride.models import FarePricing
        rider_fee = (FarePricing.objects.filter(trip_id__in=trips)
                     .aggregate(s=Sum('rider_platform_fee_minor'))['s'] or 0)
    except Exception:  # noqa: BLE001 - the field arrives with the rider platform fee
        rider_fee = 0
    costs = variable_cost_per_trip_minor(country)
    sms_count = ContactSms.objects.filter(created_at__date__gte=month, created_at__date__lt=end).count()
    calls = CallSession.objects.filter(started_at__date__gte=month, started_at__date__lt=end).count()
    from servers.ride import maps_budget
    fx = Decimal(str(settings.MAPS_FX_TO_LOCAL.get(country) or 0))
    maps_minor = int((maps_budget.list_cost_usd(maps_budget.counts(month, end - timedelta(days=1), country))
                      * fx * 100).quantize(Decimal('1')))
    sms_unit = int(getattr(settings, 'COST_SMS_UNIT_MINOR', {}).get(country) or 0)
    call_unit = int(getattr(settings, 'COST_CALL_UNIT_MINOR', {}).get(country) or 0)
    fares = trips.aggregate(s=Sum('final_fare_minor'))['s'] or 0
    pay = payment_fee_minor(fares, country)
    variable = maps_minor + sms_count * sms_unit + calls * call_unit + pay
    fixed = MonthlyFixedCost.objects.filter(country=country, month=month).first()
    fixed_minor = fixed.amount_minor if fixed else 0
    income = commission + platform_fee + rider_fee
    reserve_pct = Decimal(str(getattr(settings, 'OPEN_BOOKS_RESERVE_PERCENT', 15)))
    reserve = int((Decimal(income) * reserve_pct / 100).quantize(Decimal('1')))
    return {'country': country, 'month': month.isoformat(), 'completed_trips': n,
            'fee_income_minor': commission + platform_fee, 'commission_minor': commission,
            'platform_fee_minor': platform_fee, 'rider_platform_fee_minor': rider_fee,
            'variable_costs': {'maps_minor': maps_minor, 'sms_minor': sms_count * sms_unit,
                               'sms_count': sms_count, 'calls_minor': calls * call_unit, 'calls': calls,
                               'payment_fees_minor': pay},
            'variable_costs_minor': variable,
            'variable_cost_per_trip_minor': (variable // n) if n else None,
            'fixed_costs_minor': fixed_minor, 'fixed_costs_entered': fixed is not None,
            'reserve_percent': str(reserve_pct), 'reserve_minor': reserve,
            'surplus_minor': income - variable - fixed_minor - reserve,
            # TODO(later prompt, after 6 months of data): a driver rebate from the surplus, behind
            # the `driver_rebate` flag (off). Shown only; nothing is paid.
            'rebate': None, 'missing_config': costs['missing']}


@api_view(['GET'])
@permission_classes([IsAuthenticated, FIN_READ])
def cost_per_trip(request):
    country = str(request.query_params.get('country') or 'IN').upper()
    raw = request.query_params.get('month') or date.today().strftime('%Y-%m')
    try:
        month = date.fromisoformat(f'{raw}-01')
    except ValueError:
        return error_response(code='INVALID_MONTH', message='month is YYYY-MM.', field='month',
                              issue=raw, status=status.HTTP_400_BAD_REQUEST)
    if country not in COUNTRIES:
        return error_response(code='NOT_FOUND', message='No such country.', field='country', issue=country,
                              status=status.HTTP_404_NOT_FOUND)
    return success_response(cost_per_trip_report(country, month), status.HTTP_200_OK)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, FIN_CONFIG])
def fixed_cost_update(request, country, month):
    code = str(country).upper()
    try:
        first = date.fromisoformat(f'{month}-01')
        amount = int((request.data or {}).get('amount_minor'))
        if amount < 0 or code not in COUNTRIES:
            raise ValueError
    except (TypeError, ValueError):
        return error_response(code='INVALID_VALUE', message='A country, a YYYY-MM month and amount_minor >= 0.',
                              field='amount_minor', issue='invalid', status=status.HTTP_400_BAD_REQUEST)
    row, _ = MonthlyFixedCost.objects.get_or_create(country=code, month=first)
    before = {'amount_minor': row.amount_minor}
    row.amount_minor = amount
    row.note = str((request.data or {}).get('note') or '')[:255]
    row.updated_by_label = _label(request.user)
    row.save()
    record_admin_action(request, action='fixed_cost_set', target_type='monthly_fixed_cost',
                        target_id=f'{code}-{month}', before=before, after={'amount_minor': amount},
                        reason=row.note)
    return success_response({'country': code, 'month': first.isoformat(), 'amount_minor': amount},
                            status.HTTP_200_OK)
