import logging

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from base.money import charged_fare, sum_minor, to_major_string
from base.utils import success_response, error_response
from base.permissions import (
    CAP_DRIVER_KYC, CAP_FINANCE_PAYOUT, IsAdmin, RequiresCapability,
)
from servers.admin_access.bridge import ScopedPermission, in_scope
from servers.admin_access.scoping import (
    ScopeNotHeld, scoped, scoped_or_none, scoped_to_request,
)
from servers.driver.models import Driver, DriverSuspension, Vehicle
from servers.driver.serializers import DriverAdminListSerializer, DriverAdminDetailSerializer, KYCApprovalSerializer, VehicleSerializer
from rest_framework.pagination import PageNumberPagination

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# "KYC sees only KYC queues" (owner, QA survey 2026-10-28)
# ---------------------------------------------------------------------------
#
# A driver is IN KYC until a document review has approved them: `approved`
# is False, or their documents are pending or rejected. An approved driver
# with approved documents is an operations matter, not a KYC one.
from django.db.models import Q

# A SUSPENDED driver is not in KYC: suspension is an operations decision
# (driver.write), and a KYC-only operator approving them would be a
# reinstatement by the back door. The subquery is lazy, evaluated per use.
IN_KYC = (Q(approved=False) | ~Q(doc_status='approved')) & ~Q(
    id__in=DriverSuspension.objects.filter(lifted_at__isnull=True).values('driver_id'))


def kyc_only(user):
    """Is this operator's driver authority KYC and nothing else?

    True when the caller holds `driver.kyc` but neither `driver.write` nor
    `trip.read` -- the KYC Verifier template, and nobody else's. Such an
    operator's driver list is the KYC queue, and an already-approved
    driver answers NOT FOUND to them, the same as a driver outside their
    city. Decided through `granted`, so an unmigrated legacy `driver_ops`
    account (which still holds `trip.read` through the bridge) is
    unaffected until it is migrated.
    """
    from servers.admin_access.bridge import granted

    if not granted(user, 'driver.kyc')[0]:
        return False
    return not (granted(user, 'driver.write')[0] or granted(user, 'trip.read')[0])


def for_kyc_view(user, drivers):
    """`drivers`, narrowed to the KYC queue when the caller is KYC-only."""
    return drivers.filter(IN_KYC) if kyc_only(user) else drivers


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
def list_drivers_admin(request):
    """
    List drivers IN THE CALLER'S SCOPE. Can be filtered by 'approved' and 'status'.

    `scoped()` is what restricts this to the caller's cities; without it the
    permission check above would pass and the endpoint would still return
    every driver in both countries.
    """
    try:
        drivers = for_kyc_view(request.user, scoped_to_request(
            request, Driver.objects.select_related('user_id', 'home_city')
        )).order_by('-id')
    except ScopeNotHeld as exc:
        return error_response(
            code='SCOPE_NOT_HELD', message=str(exc), field='scope',
            issue='Requested scope is outside your assignments',
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Filters, sort and pages: the Drivers module (admin_drivers). The rows
    # are masked on the way OUT, by the renderer (admin_access.rendering),
    # not in the console: no operator is SENT the number. The reveal
    # endpoint is the way to it, and is logged.
    from servers.driver.admin_drivers import list_response
    return list_response(request, drivers)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
def retrieve_driver_admin(request, driver_id):
    """
    Retrieve one driver by ID, IF they are in the caller's scope.

    Fetched through `scoped_or_404` rather than by bare id: this used to be
    `Driver.objects.get(id=driver_id)`, so any operator could read any
    driver in any country by incrementing the id.
    """
    driver = scoped_or_none(
        request.user,
        for_kyc_view(request.user, Driver.objects.prefetch_related('user_id', 'vehicle_set')),
        id=driver_id,
    )
    if driver is None:
        # Same answer whether the driver does not exist or is another
        # city's: a distinguishable 403 would let an out-of-scope operator
        # enumerate ids. See `scoped_or_none`.
        return error_response(
            code="NOT_FOUND",
            message="Driver not found",
            field="driver_id",
            issue=f"No driver matches id {driver_id}",
            status=status.HTTP_404_NOT_FOUND
        )
    serializer = DriverAdminDetailSerializer(driver)
    return success_response(serializer.data, status.HTTP_200_OK)


def _compliance_payload(driver):
    """The review checklist: what this driver's country requires, and what
    is on file.

    Built by `servers/driver/compliance.py` -- the same functions that build
    the driver's own onboarding checklist -- so an operator and the driver
    they are reviewing cannot be looking at different lists, and a country
    whose rules change changes both screens at once.
    """
    from servers.driver import compliance

    vehicle = driver.active_vehicle or driver.vehicle_set.order_by('id').first()

    def rows(checks):
        return [
            {
                'key': key,
                'label': check['label'],
                'description': compliance.document_description(key),
                'present': check['present'],
                # False where the requirement has no column to store it in.
                # Reported rather than hidden: treating an unrecordable
                # document as satisfied is how an unverified driver gets
                # approved.
                'recordable': check['applicable'],
            }
            for key, check in checks.items()
        ]

    driver_rows = rows(compliance.evaluate_driver(driver))
    vehicle_rows = rows(compliance.evaluate_vehicle(vehicle, driver)) if vehicle else []

    return {
        'country': compliance.driver_country_code(driver),
        'driver_documents': driver_rows,
        'vehicle_documents': vehicle_rows,
        'missing': [r['key'] for r in driver_rows + vehicle_rows
                    if not (r['present'] and r['recordable'])],
    }


def _document_url(field):
    """A time-limited link to a private document, or None.

    `FileField.url` on the private storage produces a PRESIGNED url that
    expires (AWS_QUERYSTRING_EXPIRE, 15 minutes by default). It signs
    locally and makes no network call, so a broken bucket cannot make this
    raise -- but a misconfigured one can, and an operator losing the whole
    driver page because one document link could not be built would be a
    worse outcome than a missing link.
    """
    if not field:
        return None
    try:
        return field.url
    except Exception:
        logger.exception('document_url_failed name=%s', getattr(field, 'name', '?'))
        return None



@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
def driver_full_detail_admin(request, driver_id):
    """Driver aggregate for the ops console driver-detail page.

    One endpoint, one round-trip. Mirrors the admin_trip_detail
    pattern: ops opens this page when there's a question about a
    driver and we'd rather return everything than fan out to N
    endpoints.

    Aggregates across nine surfaces:
      core driver row (rating, status, fatigue lockout, license expiry)
      user_id (phone, full_name, email)
      vehicles[]  -- every vehicle on file with all four credential
                     expiries; active_vehicle is flagged
      earnings_summary: lifetime earned, today, current-month, last
                        withdrawal date
      sessions[] (last 20)  -- online/offline ledger for the fatigue
                               audit story
      cancellations_24h / cancellations_7d / cancellations_30d counts
      withdrawals[] (last 10)
      recent_trips[] (last 20)
      fatigue_status -- computed via driver.fatigue.get_fatigue_status

    Heavier paginated views (full trip history, full withdrawal log)
    stay on their existing endpoints.
    """
    from datetime import timedelta
    from decimal import Decimal
    from django.db.models import Sum
    from django.utils import timezone

    # Scoped: this endpoint returns the driver's licence images, every
    # vehicle credential and their earnings. Out of scope answers NOT_FOUND,
    # the same as a driver who does not exist.
    driver = scoped_or_none(
        request.user,
        for_kyc_view(request.user, Driver.objects.select_related(
            'user_id', 'active_vehicle', 'active_vehicle__vehicle_type_id',
        )),
        id=driver_id,
    )
    if driver is None:
        return error_response(
            code='NOT_FOUND', message='Driver not found.',
            field='driver_id', issue=f'No driver {driver_id}',
            status=status.HTTP_404_NOT_FOUND,
        )

    user = driver.user_id
    now = timezone.now()
    # Day and month boundaries in the DRIVER's market, not the deployment's.
    # `now.replace(hour=0, ...)` measured both against Asia/Kolkata for every
    # driver on the platform.
    from base.market_time import (
        driver_timezone, local_day_start, local_month_start,
    )

    from base.money import single_currency_or_none
    from servers.driver.services import driver_currency

    _dtz = driver_timezone(driver)
    today_start = local_day_start(_dtz, now=now)
    month_start = local_month_start(_dtz, now=now)
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)
    last_30d = now - timedelta(days=30)

    # ---- vehicles ----
    vehicles_payload = []
    for v in driver.vehicle_set.select_related('vehicle_type_id').all():
        vehicles_payload.append({
            'id': v.id,
            'type': v.vehicle_type_id.type if v.vehicle_type_id else None,
            'brand': v.brand,
            'model': v.model,
            'color': v.color,
            'year': v.year,
            'vehicle_number': v.vehicle_number,
            'status': v.status,
            'insurance_expiry': v.insurance_expiry.isoformat() if v.insurance_expiry else None,
            'permit_expiry': v.permit_expiry.isoformat() if v.permit_expiry else None,
            'fitness_expiry': v.fitness_expiry.isoformat() if v.fitness_expiry else None,
            'puc_expiry': v.puc_expiry.isoformat() if v.puc_expiry else None,
            'is_active_vehicle': bool(driver.active_vehicle_id and driver.active_vehicle_id == v.id),
            # THE DOCUMENTS THEMSELVES. The console showed expiry DATES and
            # no way to open the paperwork, so an operator was asked to
            # approve a driver to carry paying passengers while looking at
            # four em-dashes. Each link is presigned and short-lived.
            'rc_doc_url': _document_url(v.rc_doc),
            'insurance_doc_url': _document_url(v.insurance_doc),
            'permit_doc_url': _document_url(v.permit_doc),
            'fitness_doc_url': _document_url(v.fitness_doc),
            'puc_doc_url': _document_url(v.puc_doc),
            'inspection_doc_url': _document_url(v.inspection_doc),
            'vehicle_pic_url': _document_url(v.vehicle_pic),
        })

    # ---- fatigue status (computes live + persists lockout if breached) ----
    fatigue_payload = None
    try:
        from servers.driver.fatigue import get_fatigue_status
        fatigue_payload = get_fatigue_status(driver).to_dict()
    except Exception:
        pass

    # ---- driver sessions (last 20) ----
    sessions_payload = []
    try:
        from servers.driver.models import DriverSession
        for s in DriverSession.objects.filter(driver=driver).order_by('-started_at')[:20]:
            sessions_payload.append({
                'id': s.id,
                'started_at': s.started_at.isoformat() if s.started_at else None,
                'ended_at': s.ended_at.isoformat() if s.ended_at else None,
                'duration_seconds': s.duration_seconds,
                'end_reason': s.end_reason,
            })
    except Exception:
        pass

    # ---- cancellation counts ----
    cancellations_payload = {'last_24h': 0, 'last_7d': 0, 'last_30d': 0}
    try:
        from servers.driver.models import DriverCancellation
        cancellations_payload['last_24h'] = DriverCancellation.objects.filter(
            driver=driver, created_at__gte=last_24h,
        ).count()
        cancellations_payload['last_7d'] = DriverCancellation.objects.filter(
            driver=driver, created_at__gte=last_7d,
        ).count()
        cancellations_payload['last_30d'] = DriverCancellation.objects.filter(
            driver=driver, created_at__gte=last_30d,
        ).count()
    except Exception:
        pass

    # ---- withdrawals (last 10) ----
    withdrawals_payload = []
    try:
        from servers.driver.models import WithdrawalRequest
        for w in WithdrawalRequest.objects.filter(driver=driver).order_by('-requested_at')[:10]:
            withdrawals_payload.append({
                'id': w.id,
                'amount': str(w.amount),
                'status': w.status,
                'payout_method': w.payout_method,
                'requested_at': w.requested_at.isoformat() if w.requested_at else None,
                'processed_at': w.processed_at.isoformat() if w.processed_at else None,
                'payout_status': w.payout_status,
                'failure_count': w.failure_count,
            })
    except Exception:
        pass

    # ---- recent trips (last 20) ----
    recent_trips = []
    try:
        from servers.ride.models import Trip
        for t in Trip.objects.filter(driver_id=driver).select_related('status_id').order_by('-requested_at')[:20]:
            recent_trips.append({
                'id': t.id,
                'status': t.status_id.status_code if t.status_id else None,
                'pickup_address': t.pickup_address,
                'destination_address': t.destination_address,
                'estimated_fare': str(t.estimated_fare) if t.estimated_fare is not None else None,
                'final_fare': str(t.final_fare) if t.final_fare is not None else None,
                'requested_at': t.requested_at.isoformat() if t.requested_at else None,
                'completed_at': t.completed_at.isoformat() if t.completed_at else None,
            })
    except Exception:
        pass

    # ---- earnings summary ----
    earnings_payload = {
        'lifetime': '0.00', 'today': '0.00', 'this_month': '0.00',
        'last_withdrawal_at': driver.last_withdrawal_at.isoformat() if driver.last_withdrawal_at else None,
        'wallet_balance': '0.00',
    }
    try:
        from servers.rider.models import Wallet
        try:
            w = Wallet.objects.get(user_id=user, scope=Wallet.SCOPE_DRIVER)
            earnings_payload['wallet_balance'] = str(w.balance)
        except Wallet.DoesNotExist:
            pass
    except Exception:
        pass
    try:
        # Driver earnings are recorded as completed trips' final_fare
        # net of platform commission inside the payments flow. For the
        # ops "how much has this driver earned" question, summing
        # completed Trip.final_fare is a reasonable approximation that
        # matches how the dashboard's GMV is computed. The full ledger
        # lives in TransactionHistory + DriverEarning rows (see
        # /driver/earnings/ for the authoritative view).
        from servers.ride.models import Trip
        completed_qs = Trip.objects.filter(
            driver_id=driver, status_id__status_code='completed',
        )
        # `charged_fare()`, not 'final_fare'. That column is written only when
        # a promo is applied, so these three totals read 0.00 for a driver
        # whose every trip was an ordinary one -- which is most drivers.
        #
        # NOTE this is GROSS FARE COLLECTED on the driver's trips, not the
        # driver's take-home: commission is deducted elsewhere, and these two
        # are deliberately different numbers.
        fare = charged_fare()
        # The currency these three are in. A driver operates in one market, so
        # one code is the honest answer here -- but without it the console is
        # rendering three bare numbers and choosing a symbol for them itself.
        # Taken from the driver's trips rather than the deployment default.
        payload_currency = single_currency_or_none(completed_qs) or driver_currency(driver)
        lifetime = sum_minor(completed_qs, fare)
        today_amt = sum_minor(completed_qs.filter(completed_at__gte=today_start), fare)
        month_amt = sum_minor(completed_qs.filter(completed_at__gte=month_start), fare)
        earnings_payload['lifetime'] = to_major_string(lifetime, payload_currency)
        earnings_payload['today'] = to_major_string(today_amt, payload_currency)
        earnings_payload['this_month'] = to_major_string(month_amt, payload_currency)
        earnings_payload['currency'] = payload_currency
    except Exception:
        pass

    payload = {
        'id': driver.id,
        'status': driver.status,
        'approved': driver.approved,
        'ratings': str(driver.ratings),
        'total_trips': driver.total_trips,
        'license_expiry': driver.license_expiry.isoformat() if driver.license_expiry else None,
        'license_doc_url': _document_url(driver.license_doc),
        'license_doc_back_url': _document_url(driver.license_doc_back),
        'upi_id': driver.upi_id,
        'fatigue_lockout_until': driver.fatigue_lockout_until.isoformat() if driver.fatigue_lockout_until else None,
        'user': {
            'id': user.id if user else None,
            'phone_number': getattr(user, 'phone_number', None) if user else None,
            'full_name': getattr(user, 'full_name', None) if user else None,
            'email': getattr(user, 'email', None) if user else None,
            'is_active': getattr(user, 'is_active', None) if user else None,
        },
        'active_vehicle_id': driver.active_vehicle_id,
        'vehicles': vehicles_payload,
        'fatigue': fatigue_payload,
        'sessions': sessions_payload,
        'cancellations': cancellations_payload,
        'withdrawals': withdrawals_payload,
        'recent_trips': recent_trips,
        'earnings': earnings_payload,
        # WHAT THIS DRIVER'S COUNTRY REQUIRES, and which of it is on file.
        # Computed by servers/driver/compliance.py -- the same functions
        # that build the driver's own checklist -- so the operator and the
        # driver cannot be looking at different lists.
        'compliance': _compliance_payload(driver),
        'doc_status': driver.doc_status,
        'doc_rejection_reason': driver.doc_rejection_reason or '',
    }
    # The Drivers module's sections: per-document review, approval blockers,
    # ratings, SOS / ticket counts, suspension, history, notes, audit trail
    # and the caller's actions. Added keys only; nothing above changes.
    from servers.driver.admin_drivers import detail_extras
    payload.update(detail_extras(request, driver))
    # This payload carries the person at several depths -- the driver, the
    # user, the documents, the recent trips' addresses -- which is why the
    # renderer's masker recurses rather than masking only the top level.
    return success_response(payload, status.HTTP_200_OK)

@api_view(['PATCH'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.kyc')])
def update_kyc_status_admin(request, driver_id):
    """
    Approve or reject KYC for a driver (updates 'approved' and/or 'status').
    """
    from servers.admin_audit.services import record_admin_action

    try:
        driver = scoped_or_none(request.user, Driver.objects.all(), id=driver_id)
        if driver is None:
            raise Driver.DoesNotExist
        before = {
            'approved': driver.approved,
            'status': driver.status,
        }

        serializer = KYCApprovalSerializer(driver, data=request.data, partial=True)
        if serializer.is_valid():
            if serializer.validated_data.get('approved') is True and (
                    DriverSuspension.objects.filter(driver=driver, lifted_at__isnull=True)
                    .exists()):
                # Approval would lift a suspension that only driver.write may lift.
                return error_response(
                    code='DRIVER_SUSPENDED', message='Suspended: reinstate before approving.',
                    field='approved', issue='driver is suspended',
                    status=status.HTTP_409_CONFLICT)
            serializer.save()

            # THE REVIEW OUTCOME, WHERE THE DRIVER CAN READ IT.
            #
            # The reason an operator typed went to the audit log and nowhere
            # else: `KYCApprovalSerializer` only writes `approved` and
            # `status`, so `doc_status` stayed 'pending' forever and
            # `doc_rejection_reason` was never written at all. The driver's
            # screen showed "not approved" with no reason, which is
            # identical to what a driver who has uploaded nothing sees --
            # and the operator believed they had explained themselves.
            approved = serializer.validated_data.get('approved')
            if approved is not None:
                driver.doc_status = 'approved' if approved else 'rejected'
                driver.doc_rejection_reason = (
                    '' if approved else (request.data.get('reason') or '').strip()
                )
                driver.doc_status_updated_at = timezone.now()
                driver.save(update_fields=[
                    'doc_status', 'doc_rejection_reason', 'doc_status_updated_at',
                ])

            driver.refresh_from_db()
            after = {
                'approved': driver.approved,
                'status': driver.status,
            }
            record_admin_action(
                request,
                action='kyc_update',
                target_type='driver',
                target_id=driver.id,
                before=before,
                after=after,
                reason=request.data.get('reason', ''),
            )
            return success_response(serializer.data, status.HTTP_200_OK)

        return error_response(
            code="VALIDATION_ERROR",
            message="Invalid update data",
            field=list(serializer.errors.keys())[0],
            issue=str(serializer.errors),
            status=status.HTTP_400_BAD_REQUEST
        )
    except Driver.DoesNotExist:
        return error_response(
            code="NOT_FOUND",
            message="Driver not found",
            field="driver_id",
            issue=f"No driver matches id {driver_id}",
            status=status.HTTP_404_NOT_FOUND
        )

@api_view(['DELETE'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.deactivate')])
def delete_driver_admin(request, driver_id):
    """
    Permanently DEACTIVATE a driver (the route keeps its old name).

    This used to be `driver.delete()`: a hard delete that cascaded through
    the driver's vehicles, sessions and withdrawals and orphaned their trips
    and audit history, by one operator with no second pair of eyes. It now
    raises a `driver.deactivate` request (always a checker, AUDIT section 10);
    on approval the account is locked and the driver suspended for good,
    and every row is kept. Scoped: `scoped_or_none` inside the helper.
    """
    from servers.driver.admin_drivers import deactivate_response

    if scoped_or_none(request.user, Driver.objects.all(), id=driver_id) is None:
        return error_response(
            code="NOT_FOUND",
            message="Driver not found",
            field="driver_id",
            issue=f"No driver matches id {driver_id}",
            status=status.HTTP_404_NOT_FOUND
        )
    return deactivate_response(request, driver_id)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
def get_vehicle_details(request, driver_id):
    """
    Get details of a vehicle by its ID.
    """
    try:
        # Scoped through the driver: a vehicle's documents are the
        # driver's documents.
        if scoped_or_none(request.user, for_kyc_view(request.user, Driver.objects.all()),
                          id=driver_id) is None:
            raise Vehicle.DoesNotExist
        vehicle_objects = Vehicle.objects.filter(driver_id=driver_id)
        serializer = VehicleSerializer(vehicle_objects, many=True)
        return success_response(serializer.data, status.HTTP_200_OK)
    except Vehicle.DoesNotExist:
        return error_response(
            code="NOT_FOUND",
            message="Vehicle not found",
            field="vehicle_id",
            issue=f"No vehicle for driver {driver_id}",
            status=status.HTTP_404_NOT_FOUND
        )


# ── Admin Withdrawal Management ──────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.read')])
def list_withdrawals_admin(request):
    """
    GET /api/admin/withdrawals/
    List all withdrawal requests with filtering (status, driver, date range).
    """
    from .models import WithdrawalRequest
    from .serializers import WithdrawalRequestSerializer
    from django.db.models import Q
    from django.utils import timezone
    from datetime import timedelta

    try:
        # The one money-scope rule (servers/payments/scope.py): the driver's
        # home city, narrowed by ?scope= and to where finance.read is held.
        from servers.admin_access.scoping import requested_node
        from servers.payments.scope import money_visible_to

        withdrawals = money_visible_to(
            request.user, WithdrawalRequest, node=requested_node(request),
            permission='finance.read',
            queryset=WithdrawalRequest.objects.select_related(
                'driver', 'driver__user_id', 'driver__home_city',
            ),
        ).order_by('-requested_at')
    except ScopeNotHeld as exc:
        return error_response(
            code='SCOPE_NOT_HELD', message=str(exc), field='scope',
            issue='Requested scope is outside your assignments',
            status=status.HTTP_403_FORBIDDEN,
        )

    # Filter by status
    status_filter = request.query_params.get('status')
    if status_filter:
        withdrawals = withdrawals.filter(status=status_filter)

    # Filter by driver ID
    driver_id = request.query_params.get('driver_id')
    if driver_id:
        withdrawals = withdrawals.filter(driver_id=driver_id)

    # Filter by date range (start_date, end_date)
    start_date = request.query_params.get('start_date')
    end_date = request.query_params.get('end_date')
    if start_date:
        try:
            start = timezone.datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            withdrawals = withdrawals.filter(requested_at__gte=start)
        except ValueError:
            pass
    if end_date:
        try:
            end = timezone.datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            withdrawals = withdrawals.filter(requested_at__lte=end)
        except ValueError:
            pass

    paginator = PageNumberPagination()
    paginator.page_size = 10
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = 50
    page = paginator.paginate_queryset(withdrawals, request)
    serializer = WithdrawalRequestSerializer(page, many=True)
    # A payout queue names the person being paid. The renderer masks the
    # phone and the UPI handle for everybody; Finance reveals one payee at
    # a time through /admin-access/reveal/ when a payout needs checking.
    return success_response(
        paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def approve_withdrawal_admin(request, withdrawal_id):
    """
    POST /api/admin/withdrawals/{id}/approve/

    Ask to release a payout. This is the MAKER half of maker-checker: the
    caller must hold `finance.payout` in the withdrawal's scope, and the
    policy for the driver's country decides what happens next. "Payout
    runs always a checker" is the seeded India rule, so in practice this
    creates an `ApprovalRequest` (202) and a DIFFERENT finance admin
    holding `approval.check` approves it at
    `/api/v1/approvals/admin/requests/<id>/approve/`; only then does the
    withdrawal flip to approved and the payout dispatch happen -- in
    `servers.approvals.executors.execute_payout`, which is this view's old
    body moved, lock and all. A country with no policy is refused with
    THRESHOLD_NOT_CONFIGURED (409), by ruling.

    The lock-then-scope lookup is kept exactly as it was: a 404 that is
    indistinguishable from a missing row, decided before any policy is
    read, so an out-of-scope id leaks nothing about which country it is in.
    """
    from .models import WithdrawalRequest
    from django.db import transaction
    from servers.approvals import service as approvals
    from servers.approvals.actions import FINANCE_PAYOUT
    from servers.approvals.executors import payout_country, withdrawal_key

    try:
        with transaction.atomic():
            try:
                withdrawal = (
                    WithdrawalRequest.objects.select_for_update()
                    .select_related('driver', 'driver__user_id', 'driver__home_city')
                    .get(id=withdrawal_id)
                )
                # Scope checked AFTER the lock, not in the query: see
                # `in_scope`. Answers NOT_FOUND, identical to a missing
                # row, so another city's withdrawal ids cannot be probed.
                if not in_scope(request.user, withdrawal, 'finance.payout'):
                    raise WithdrawalRequest.DoesNotExist
            except WithdrawalRequest.DoesNotExist:
                return error_response(
                    code="NOT_FOUND",
                    message="Withdrawal request not found",
                    field="withdrawal_id",
                    issue=f"No withdrawal matches id {withdrawal_id}",
                    status=status.HTTP_404_NOT_FOUND,
                )

            if withdrawal.status != 'pending':
                return error_response(
                    code="INVALID_STATUS",
                    message="Only pending withdrawals can be approved",
                    field="status",
                    issue=f"Current status is {withdrawal.status}",
                    status=status.HTTP_400_BAD_REQUEST,
                )
    except Exception as e:
        logger.error(f"approve_withdrawal_admin lookup failed: {e}")
        return error_response(
            code="INTERNAL_ERROR",
            message="Failed to approve withdrawal",
            field="general",
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    country = payout_country(withdrawal)
    if country is None:
        return error_response(
            code="COUNTRY_UNKNOWN",
            message="This withdrawal cannot be placed in a country",
            field="withdrawal_id",
            issue="The driver has no home city and the currency names no market",
            status=status.HTTP_409_CONFLICT,
        )

    # Outside the lock: within-limit execution (never, for payouts, under
    # the seeded policy -- but the framework decides, not this view) would
    # otherwise make the gateway call while holding the row.
    try:
        outcome = approvals.require_approval_or_execute(
            FINANCE_PAYOUT,
            actor=request.user,
            country=country,
            amount=withdrawal.amount_minor,
            currency=withdrawal.currency,
            scope_node=withdrawal.driver.home_city,
            target=withdrawal,
            target_type='withdrawal_request',
            payload={'withdrawal_id': withdrawal.id},
            idempotency_key=withdrawal_key(withdrawal.id),
            reason=str(request.data.get('reason', '') or '') if hasattr(request, 'data') else '',
            http_request=request,
        )
    except approvals.ApprovalError as exc:
        return exc.response()

    from .serializers import WithdrawalRequestSerializer
    withdrawal.refresh_from_db()
    serializer = WithdrawalRequestSerializer(withdrawal)
    if outcome.executed:
        # Within the maker's limit: the old single-step answer.
        return success_response(serializer.data, status.HTTP_200_OK)
    return success_response(
        {
            'withdrawal': serializer.data,
            'approval_request': outcome.request.as_dict(),
            'duplicate': outcome.duplicate,
        },
        status.HTTP_202_ACCEPTED,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def reject_withdrawal_admin(request, withdrawal_id):
    """
    POST /api/admin/withdrawals/{id}/reject/

    Reject a pending withdrawal and refund the amount to the
    driver's wallet exactly once.
    """
    from .models import WithdrawalRequest
    from django.db import transaction
    from django.utils import timezone
    from servers.rider.models import Wallet, WalletTransaction
    from servers.admin_audit.services import record_admin_action

    admin_notes = request.data.get('admin_notes', '').strip()

    try:
        with transaction.atomic():

            # Lock the withdrawal so two admins cannot reject
            # the same withdrawal at the same time.
            try:
                withdrawal = (
                    WithdrawalRequest.objects
                    .select_for_update()
                    .select_related('driver')
                    .get(id=withdrawal_id)
                )
                # Scope checked AFTER the lock, not in the query: see
                # `in_scope`. Answers NOT_FOUND, identical to a missing
                # row, so another city's withdrawal ids cannot be probed.
                if not in_scope(request.user, withdrawal, 'finance.payout'):
                    raise WithdrawalRequest.DoesNotExist
            except WithdrawalRequest.DoesNotExist:
                return error_response(
                    code="NOT_FOUND",
                    message="Withdrawal request not found",
                    field="withdrawal_id",
                    issue=f"No withdrawal matches id {withdrawal_id}",
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Only pending withdrawals can be rejected.
            if withdrawal.status != 'pending':
                return error_response(
                    code="INVALID_STATUS",
                    message="Only pending withdrawals can be rejected",
                    field="status",
                    issue=f"Current status is {withdrawal.status}",
                    status=status.HTTP_400_BAD_REQUEST,
                )

            driver = withdrawal.driver
            amount = withdrawal.amount

            # Lock the driver's wallet.
            try:
                wallet = (
                    Wallet.objects
                    .select_for_update()
                    .get(
                        user_id=driver.user_id,
                        scope=Wallet.SCOPE_DRIVER,
                    )
                )
            except Wallet.DoesNotExist:
                logger.error(
                    "Driver wallet not found while rejecting "
                    "withdrawal %s for driver %s",
                    withdrawal.id,
                    driver.id,
                )

                return error_response(
                    code="WALLET_NOT_FOUND",
                    message="Driver wallet not found",
                    field="wallet",
                    issue=f"No driver wallet exists for driver {driver.id}",
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # ---------------------------------------------------------
            # IDEMPOTENT REFUND
            # ---------------------------------------------------------
            #
            # The withdrawal creation flow already deducted this
            # amount from the driver's wallet.
            #
            # Therefore, when an admin rejects the withdrawal,
            # we must return the money exactly once.
            #
            refund_reference = (
                f"refund_rejected_withdrawal_{withdrawal.id}"
            )

            refund_idempotency_key = (
                f"refund_rejected_withdrawal_{withdrawal.id}"
            )

            refund_already_exists = (
                WalletTransaction.objects
                .filter(
                    reference_id=refund_reference,
                    purpose='refund_rejected_withdrawal',
                    txn_type='credit',
                    status='completed',
                )
                .exists()
            )

            if not refund_already_exists:

                # Return the withdrawal amount to the driver wallet.
                wallet.balance += amount

                wallet.save(
                    update_fields=['balance_minor']
                )

                # Create one permanent refund transaction.
                WalletTransaction.objects.create(
                    user_id=driver.user_id,
                    amount=amount,
                    txn_type='credit',
                    status='completed',
                    purpose='refund_rejected_withdrawal',
                    reference_id=refund_reference,
                    idempotency_key=refund_idempotency_key,
                )

                logger.info(
                    "Rejected withdrawal refunded successfully: "
                    "withdrawal=%s driver=%s amount=%s",
                    withdrawal.id,
                    driver.id,
                    amount,
                )

            else:
                logger.info(
                    "Refund already exists for rejected withdrawal=%s. "
                    "Skipping duplicate refund.",
                    withdrawal.id,
                )

            # ---------------------------------------------------------
            # REJECT WITHDRAWAL
            # ---------------------------------------------------------

            before = {
                'status': withdrawal.status,
                'amount': str(amount),
            }

            withdrawal.status = 'rejected'
            withdrawal.admin_notes = admin_notes
            withdrawal.processed_at = timezone.now()

            withdrawal.save(
                update_fields=[
                    'status',
                    'admin_notes',
                    'processed_at',
                ]
            )

            # ---------------------------------------------------------
            # ADMIN AUDIT LOG
            # ---------------------------------------------------------

            record_admin_action(
                request,
                action='withdrawal_rejected',
                target_type='withdrawal_request',
                target_id=withdrawal.id,
                before=before,
                after={
                    'status': 'rejected',
                    'admin_notes': admin_notes,
                    'wallet_refunded': True,
                    'refund_reference': refund_reference,
                    'refund_amount': str(amount),
                },
                reason=admin_notes or 'Withdrawal rejected by admin',
            )

    except Exception as e:
        logger.error(
            f"reject_withdrawal_admin atomic block failed: {e}",
            exc_info=True,
        )

        return error_response(
            code="INTERNAL_ERROR",
            message="Failed to reject withdrawal",
            field="general",
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    from .serializers import WithdrawalRequestSerializer

    serializer = WithdrawalRequestSerializer(withdrawal)

    return success_response(
        serializer.data,
        status.HTTP_200_OK,
    )

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def bulk_action_withdrawal_admin(request):
    """
    POST /api/admin/withdrawals/bulk-action/

    Bulk approve/reject multiple pending withdrawal requests.

    Rejected withdrawals refund the driver's wallet exactly once.
    """
    from .models import WithdrawalRequest
    from django.db import transaction
    from django.utils import timezone
    from servers.rider.models import Wallet, WalletTransaction
    from servers.admin_audit.services import record_admin_action

    # Prevent an admin request from processing an excessive number
    # of withdrawals at once.
    BULK_MAX = 50

    action = request.data.get('action')
    withdrawal_ids = request.data.get('withdrawal_ids', [])
    admin_notes = request.data.get('admin_notes', '').strip()

    if action not in ['approve', 'reject']:
        return error_response(
            code="INVALID_ACTION",
            message="Action must be 'approve' or 'reject'",
            field="action",
            issue=f"Invalid action: {action}",
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not withdrawal_ids:
        return error_response(
            code="EMPTY_LIST",
            message="No withdrawal IDs provided",
            field="withdrawal_ids",
            issue="Provide at least one withdrawal ID",
            status=status.HTTP_400_BAD_REQUEST,
        )

    if len(withdrawal_ids) > BULK_MAX:
        return error_response(
            code="TOO_MANY",
            message=f"At most {BULK_MAX} withdrawals can be processed at once",
            field="withdrawal_ids",
            issue=f"Received {len(withdrawal_ids)} ids; limit is {BULK_MAX}",
            status=status.HTTP_400_BAD_REQUEST,
        )

    approved_for_payout = []
    updated_ids = []
    skipped_ids = []

    for wid in withdrawal_ids:

        try:
            with transaction.atomic():

                # Lock withdrawal so concurrent admin requests
                # cannot process the same withdrawal twice.
                try:
                    w = (
                        WithdrawalRequest.objects
                        .select_for_update()
                        .select_related('driver')
                        .get(id=wid)
                    )
                    if not in_scope(request.user, w, 'finance.payout'):
                        # Indistinguishable from "no such withdrawal".
                        raise WithdrawalRequest.DoesNotExist
                except WithdrawalRequest.DoesNotExist:
                    skipped_ids.append(wid)
                    continue

                # Only pending withdrawals can be processed.
                if w.status != 'pending':
                    skipped_ids.append(wid)
                    continue

                before = {
                    'status': w.status,
                    'amount': str(w.amount),
                }

                # =====================================================
                # APPROVE
                # =====================================================

                if action == 'approve':

                    # Nothing is flipped here any more. A bulk approve is a
                    # payout RUN, and payout runs always need a checker
                    # (the seeded policy); the eligible rows are collected
                    # and raised as ONE approval request below. The flip,
                    # the audit row and the dispatch happen in
                    # `servers.approvals.executors.execute_payout` when a
                    # second finance admin approves.
                    approved_for_payout.append(w)
                    continue

                # =====================================================
                # REJECT + REFUND
                # =====================================================

                else:

                    driver = w.driver
                    amount = w.amount

                    # Lock driver's wallet before modifying the balance.
                    try:
                        wallet = (
                            Wallet.objects
                            .select_for_update()
                            .get(
                                user_id=driver.user_id,
                                scope=Wallet.SCOPE_DRIVER,
                            )
                        )
                    except Wallet.DoesNotExist:

                        logger.error(
                            "Driver wallet not found for bulk rejection: "
                            "withdrawal=%s driver=%s",
                            w.id,
                            driver.id,
                        )

                        # Because this row is inside transaction.atomic(),
                        # nothing for this withdrawal will be committed.
                        skipped_ids.append(wid)
                        continue

                    # -------------------------------------------------
                    # Deterministic refund reference
                    # -------------------------------------------------

                    refund_reference = (
                        f"refund_rejected_withdrawal_{w.id}"
                    )

                    refund_idempotency_key = (
                        f"refund_rejected_withdrawal_{w.id}"
                    )

                    # Check whether this withdrawal was already refunded.
                    refund_already_exists = (
                        WalletTransaction.objects
                        .filter(
                            reference_id=refund_reference,
                            purpose='refund_rejected_withdrawal',
                            txn_type='credit',
                            status='completed',
                        )
                        .exists()
                    )

                    if not refund_already_exists:

                        # Return the withdrawn amount to the driver wallet.
                        wallet.balance += amount

                        wallet.save(
                            update_fields=['balance_minor']
                        )

                        # Record the refund transaction.
                        WalletTransaction.objects.create(
                            user_id=driver.user_id,
                            amount=amount,
                            txn_type='credit',
                            status='completed',
                            purpose='refund_rejected_withdrawal',
                            reference_id=refund_reference,
                            idempotency_key=refund_idempotency_key,
                        )

                        logger.info(
                            "Bulk rejected withdrawal refunded: "
                            "withdrawal=%s driver=%s amount=%s",
                            w.id,
                            driver.id,
                            amount,
                        )

                    else:

                        logger.info(
                            "Bulk rejection refund already exists: "
                            "withdrawal=%s. Skipping duplicate refund.",
                            w.id,
                        )

                    # -------------------------------------------------
                    # Mark withdrawal rejected
                    # -------------------------------------------------

                    w.status = 'rejected'
                    w.admin_notes = admin_notes
                    w.processed_at = timezone.now()

                    w.save(
                        update_fields=[
                            'status',
                            'admin_notes',
                            'processed_at',
                        ]
                    )

                    # -------------------------------------------------
                    # Audit log
                    # -------------------------------------------------

                    record_admin_action(
                        request,
                        action='withdrawal_rejected',
                        target_type='withdrawal_request',
                        target_id=w.id,
                        before=before,
                        after={
                            'status': 'rejected',
                            'admin_notes': admin_notes,
                            'wallet_refunded': True,
                            'refund_reference': refund_reference,
                            'refund_amount': str(amount),
                        },
                        reason=(
                            admin_notes
                            or 'bulk action'
                        ),
                    )

                updated_ids.append(w.id)

        except Exception as e:
            logger.error(
                "bulk_action_withdrawal_admin: "
                "row %s failed: %s",
                wid,
                e,
                exc_info=True,
            )

            skipped_ids.append(wid)

    # =============================================================
    # APPROVE: ONE MAKER-CHECKER REQUEST FOR THE WHOLE BATCH
    # =============================================================
    #
    # Raised outside every row lock. The batch must be one currency and
    # one country: a threshold is per country in local currency, and a
    # sum across currencies is not a number (base/money.py).
    # =============================================================

    if action == 'approve' and approved_for_payout:
        from servers.approvals import service as approvals
        from servers.approvals.actions import FINANCE_PAYOUT
        from servers.approvals.executors import batch_key, payout_country

        currencies = {w.currency for w in approved_for_payout}
        countries = {payout_country(w) for w in approved_for_payout}
        if len(currencies) != 1 or len(countries) != 1 or None in countries:
            return error_response(
                code="MIXED_MARKETS",
                message="A bulk payout must be in one currency and one country",
                field="withdrawal_ids",
                issue=f"currencies={sorted(currencies)} countries={sorted(c or '?' for c in countries)}",
                status=status.HTTP_400_BAD_REQUEST,
            )
        cities = {w.driver.home_city_id for w in approved_for_payout}
        scope_node = approved_for_payout[0].driver.home_city if len(cities) == 1 else None
        ids = sorted(w.id for w in approved_for_payout)
        try:
            outcome = approvals.require_approval_or_execute(
                FINANCE_PAYOUT,
                actor=request.user,
                country=countries.pop(),
                amount=sum(w.amount_minor for w in approved_for_payout),
                currency=currencies.pop(),
                scope_node=scope_node,
                target_type='withdrawal_batch',
                target_id=batch_key(ids).split(':', 1)[1],
                payload={'withdrawal_ids': ids},
                idempotency_key=batch_key(ids),
                reason=admin_notes or 'bulk action',
                http_request=request,
            )
        except approvals.ApprovalError as exc:
            return exc.response()

        if outcome.executed:
            result = outcome.request.result or {}
            updated_ids = list(result.get('approved_ids', []))
            skipped_ids = skipped_ids + list(result.get('skipped_ids', []))
        else:
            return success_response(
                {
                    'action': action,
                    'approval_request': outcome.request.as_dict(),
                    'duplicate': outcome.duplicate,
                    'requested_ids': ids,
                    'skipped_ids': skipped_ids,
                    'requested_count': len(ids),
                    'skipped_count': len(skipped_ids),
                },
                status.HTTP_202_ACCEPTED,
            )

    return success_response(
        {
            'action': action,
            'updated_ids': updated_ids,
            'skipped_ids': skipped_ids,
            'updated_count': len(updated_ids),
            'skipped_count': len(skipped_ids),
        },
        status.HTTP_200_OK,
    )
