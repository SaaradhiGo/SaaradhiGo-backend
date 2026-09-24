from datetime import datetime, time, timedelta
import csv
from functools import wraps
from django.utils import timezone
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render,get_object_or_404
from django.utils.dateparse import parse_datetime
# from servers.driver.admin_utils import list_drivers_admin
from servers.support.models import SupportTicket,SupportMessage
from django.core.paginator import Paginator
from django.db import models, transaction as db_transaction
from django.db.models.functions import Coalesce
from django.db.models import (Avg,Count, Q,Sum,F,Max,Value,DecimalField,)
from servers.driver.models import Driver, WithdrawalRequest,VehicleType,Vehicle,DriverSession,DriverCancellation,DriverBankAccount,DriverUPIContact
from servers.ride.models import (
    ChatMessage,
    FarePricing,
    Trip,
    PromoCode,
    PromoRedemption,
)
from servers.pricing.services import commission_percent_for_trip
from servers.pricing.models import ServiceZone, RateCard
from decimal import Decimal, InvalidOperation
from django.http import JsonResponse
from django.views.decorators.http import require_POST,require_http_methods
from django.contrib.admin.views.decorators import staff_member_required

from servers.admin_dashboard import login_guard
from servers.payments.models import Payment,TransactionHistory
from servers.rider.models import (
    Rider, FavoritePlace, Wallet, WalletTransaction, Notification,
    NotificationPreference,
)
from servers.ride.models import Receipt
from servers.sos.models import SOSEvent, SOSEventUpdate
from django.contrib import messages
from django.contrib.auth import get_user_model
import json
import logging
logger = logging.getLogger(__name__)
# Re-exported, not reimplemented. The console used to carry its own copy of this
# expression; `base.permissions.is_operator` is now the only definition, and its
# docstring records the four divergent copies that preceded it. Imported under the
# same name so existing call sites and tests are unchanged.
from base import ops_mfa  # noqa: E402
from base.permissions import is_operator  # noqa: E402
from servers.admin_audit.services import record_admin_action  # noqa: E402


def admin_required(view_func):
    """
    Decorator for admin dashboard views.
    - If the user is not authenticated -> redirect to the login page.
    - If the user is authenticated but not an admin -> redirect to the
      login page (they shouldn't be in the admin portal at all).
    - Otherwise allow the view to run.
    """
    @wraps(view_func)
    def _wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        # Two different questions, and both must be yes.
        #
        #   is_operator            -- is this ACCOUNT allowed to operate
        #   console_access_allowed -- and has this SESSION presented its factor
        #
        # Conflating them is how MFA becomes decorative: an account with a device
        # enrolled is not the same as a browser that has ever proved it.
        if not ops_mfa.console_access_allowed(request):
            if is_operator(request.user):
                # Signed in, authority fine, factor not yet presented in this
                # session. Send them to the challenge rather than to the login
                # form, or they would re-enter a password that already worked.
                return redirect("ops_mfa_challenge")
            return redirect("login")
        return view_func(request, *args, **kwargs)
    return _wrapped
def login(request: HttpRequest) -> HttpResponse:
    """
    Admin login view.
    Accepts a phone number (posted as `username`) and password, authenticates
    against the custom user model, and starts a session. Only users with the
    `admin` role (or superusers) are allowed in.
    """
    if is_operator(request.user):
        # Already signed in. Straight to the dashboard only if this session has
        # also cleared MFA; otherwise to the challenge, because a valid session
        # cookie is one factor, not two.
        if ops_mfa.console_access_allowed(request):
            return redirect("fleet_monitor")
        return redirect("ops_mfa_challenge")
    error = None
    if request.method == "POST":
        phone_number = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        if not phone_number or not password:
            error = "Phone number and password are required."
        else:
            # Normalise to E.164 so the lookup matches stored phone numbers.
            if not phone_number.startswith("+"):
                if len(phone_number) == 10:
                    phone_number = f"+91{phone_number}"
                elif phone_number.startswith("91") and len(phone_number) == 12:
                    phone_number = f"+{phone_number}"
            # Brute-force guard. Measured against QA before this existed: 12
            # consecutive failed logins in 7.8 seconds with no throttling at all,
            # on the console that approves KYC and releases payouts.
            #
            # Checked BEFORE authenticate() so a locked account costs no password
            # verification, and the refusal text is the generic one so a lockout
            # cannot be used to discover which phone numbers are real.
            if login_guard.is_locked(request, phone_number):
                error = login_guard.GENERIC_REFUSAL
            else:
                user = authenticate(request, username=phone_number, password=password)
                if user is None:
                    login_guard.record_failure(request, phone_number)
                    error = login_guard.GENERIC_REFUSAL
                elif not is_operator(user):
                    # A real password for a non-operator account. Still a failure
                    # for guard purposes -- otherwise a rider account becomes an
                    # unthrottled oracle for password guessing.
                    login_guard.record_failure(request, phone_number)
                    error = "You do not have admin privileges."
                elif ops_mfa.mfa_required_for(user) and not ops_mfa.has_device(user):
                    # Enforced environment, operator with no factor enrolled.
                    # Refused rather than waved through: "enforced" that yields to
                    # "not enrolled yet" is not enforced, and this is what makes
                    # enrolment happen instead of being deferred forever.
                    #
                    # The generic refusal, so this does not tell an attacker which
                    # operator accounts lack a second factor.
                    login_guard.record_failure(request, phone_number)
                    logger.warning(
                        'ops_login_refused_no_mfa_device user_id=%s', user.id)
                    error = login_guard.GENERIC_REFUSAL
                else:
                    login_guard.clear(request, phone_number)
                    auth_login(request, user)
                    if ops_mfa.mfa_required_for(user):
                        # The password was correct; the session is NOT yet
                        # verified. auth_login started it, OTPMiddleware will
                        # report is_verified() False, and every @admin_required
                        # page therefore bounces to the challenge until a code is
                        # presented.
                        return redirect("ops_mfa_challenge")
                    return redirect("fleet_monitor")
    return render(request, 'admin_pages/login.html', {"error": error})
def ops_mfa_challenge(request: HttpRequest) -> HttpResponse:
    """Present a second factor for a session whose password already succeeded.

    Deliberately NOT decorated with `@admin_required`: that decorator redirects
    here, so decorating it would loop. It does its own narrower check -- a session
    that is signed in as an operator -- which is exactly the state this page exists
    to resolve.

    Rate-limited through the same `login_guard` as the password form. A six-digit
    code is 10^6 possibilities and a TOTP window is 30 seconds wide; without a
    limit on attempts, MFA against an already-authenticated session is a
    thirty-second brute force.
    """
    user = request.user
    if not is_operator(user):
        return redirect("login")
    if ops_mfa.console_access_allowed(request):
        return redirect("fleet_monitor")

    error = None
    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        guard_key = getattr(user, 'phone_number', None) or str(user.pk)
        if login_guard.is_locked(request, guard_key):
            error = "Invalid code."
        elif ops_mfa.verify_code(user, code):
            login_guard.clear(request, guard_key)
            ops_mfa.mark_session_verified(request, user)
            record_admin_action(
                request, action='ops_mfa_verified', target_type='operator',
                target_id=user.pk, after={'method': 'console'},
            )
            return redirect("fleet_monitor")
        else:
            login_guard.record_failure(request, guard_key)
            # One message for a wrong code, a reused code and a lockout.
            error = "Invalid code."

    return render(request, 'admin_pages/ops_mfa_challenge.html', {"error": error})


@admin_required
def dashboard(request: HttpRequest) -> HttpResponse:
    # Pull the same KPI payload the /api/v1/ride/admin/dashboard/ endpoint
    # returns so the server-rendered fleet monitor page can show the
    # day's headline numbers without a second round-trip from the
    # browser. The helper is request-agnostic, so it is safe to call
    # from this non-DRF view.
    from servers.ride.admin_views import build_admin_dashboard_kpis
    try:
        kpis = build_admin_dashboard_kpis()
    except Exception:
        # Never let a stats failure blank the whole map page — the live
        # WebSocket driver feed is still useful on its own.
        kpis = None
    return render(request, 'admin_pages/fleet_monitor.html', {"kpis": kpis})
from urllib.parse import urlencode
from django.db import transaction
@admin_required
def driver_onboarding(request: HttpRequest) -> HttpResponse:
    """
    Driver KYC + Vehicle Fleet Compliance Admin Page.

    No model changes required.

    Approval is allowed only when all mandatory checks pass:

        1. Driving License
        2. Vehicle RC
        3. Commercial Insurance
        4. Commercial Permit
        5. Fitness Certificate
        6. Pollution Under Control (PUC)
    """

    # =========================================================
    # GET PARAMETERS
    # =========================================================

    status_filter = request.GET.get(
        "status",
        "ALL",
    ).upper()

    search = request.GET.get(
        "search",
        "",
    ).strip()

    selected_driver_id = request.GET.get(
        "selected_driver"
    )

    page_number = request.GET.get(
        "page",
        1,
    )

    valid_statuses = {
        "ALL",
        "PENDING",
        "APPROVED",
        "REJECTED",
    }

    if status_filter not in valid_statuses:
        status_filter = "ALL"

    # =========================================================
    # HELPER FUNCTIONS
    # =========================================================

    def get_value(
        obj,
        *names,
        default=None,
    ):
        """
        Safely return the first non-empty attribute.
        """

        for name in names:

            try:
                value = getattr(
                    obj,
                    name,
                    None,
                )
            except Exception:
                value = None

            if value not in (
                None,
                "",
            ):
                return value

        return default

    def has_document(value):
        """
        Safely determine whether a FileField contains a file.
        """

        if value is None:
            return False

        try:
            if hasattr(value, "name"):
                return bool(value.name)
        except Exception:
            pass

        try:
            return bool(
                str(value).strip()
            )
        except Exception:
            return False

    def document_url(value):
        """
        Safely get a document URL.
        """

        if not value:
            return None

        try:
            return value.url
        except Exception:
            return None

    def format_date(value):
        """
        Format date/datetime for the template.
        """

        if not value:
            return "Not Provided"

        try:
            return value.strftime(
                "%d %b %Y"
            )
        except Exception:
            return str(value)

    def get_driver_name(driver):
        """
        EXACT Driver model mapping:

            Driver.user_id -> User
            User.full_name
        """

        try:
            if (
                driver.user_id
                and driver.user_id.full_name
            ):
                return (
                    driver.user_id.full_name
                )
        except Exception:
            pass

        return f"Driver #{driver.pk}"

    def get_driver_phone(driver):
        """
        EXACT Driver model mapping:

            Driver.user_id -> User
            User.phone_number
        """

        try:
            if (
                driver.user_id
                and driver.user_id.phone_number
            ):
                return (
                    driver.user_id.phone_number
                )
        except Exception:
            pass

        return "Not Provided"

    def get_driver_vehicles(driver):
        """
        Vehicle model uses:

            driver_id = ForeignKey(
                Driver,
                on_delete=models.CASCADE
            )

        Therefore Django's default reverse
        relationship is:

            driver.vehicle_set.all()
        """

        try:
            return list(
                driver.vehicle_set.all()
            )
        except Exception:
            return []

    def get_vehicle_type(vehicle):
        """
        EXACT Vehicle model mapping:

            vehicle.vehicle_type_id -> VehicleType
            VehicleType.type
        """

        try:
            if (
                vehicle.vehicle_type_id
                and vehicle.vehicle_type_id.type
            ):
                return (
                    vehicle.vehicle_type_id.type
                )
        except Exception:
            pass

        return "N/A"

    def build_onboarding_url(
        driver_id=None,
    ):
        """
        Preserve current filter/search/page
        when redirecting after POST actions.
        """

        params = {
            "status": status_filter,
            "page": page_number,
        }

        if search:
            params["search"] = search

        if driver_id:
            params["selected_driver"] = driver_id

        return (
            "/driver_onboarding/?"
            + urlencode(params)
        )

    # =========================================================
    # POST ACTIONS
    # =========================================================

    if request.method == "POST":

        action = request.POST.get(
            "action",
            "",
        ).strip().lower()

        driver_id = request.POST.get(
            "driver_id"
        )

        if not driver_id:

            messages.error(
                request,
                "Driver ID is missing.",
            )

            return redirect(
                "driver_onboarding"
            )

        try:

            driver = (
                Driver.objects
                .select_related(
                    "user_id"
                )
                .get(
                    pk=driver_id
                )
            )

        except (
            Driver.DoesNotExist,
            ValueError,
            TypeError,
        ):

            messages.error(
                request,
                "Driver not found.",
            )

            return redirect(
                "driver_onboarding"
            )

        # =====================================================
        # APPROVE DRIVER
        # =====================================================

        if action == "approve":

            # -------------------------------------------------
            # 1. DRIVING LICENSE
            # -------------------------------------------------

            license_front = (
                driver.license_doc
            )

            license_passed = (
                has_document(
                    license_front
                )
            )

            # -------------------------------------------------
            # VEHICLES
            # -------------------------------------------------

            vehicles = (
                get_driver_vehicles(
                    driver
                )
            )

            vehicle_compliance_results = []

            # No vehicle means approval must fail.
            all_vehicle_compliant = bool(
                vehicles
            )

            # -------------------------------------------------
            # CHECK EVERY VEHICLE
            # -------------------------------------------------

            for vehicle in vehicles:

                # EXACT MODEL FIELD
                rc_doc = (
                    vehicle.rc_doc
                )

                # EXACT MODEL FIELD
                insurance_doc = (
                    vehicle.insurance_doc
                )

                # EXACT MODEL FIELD
                permit_doc = (
                    vehicle.permit_doc
                )

                # EXACT MODEL FIELD
                fitness_doc = (
                    vehicle.fitness_doc
                )

                # EXACT MODEL FIELD
                puc_doc = (
                    vehicle.puc_doc
                )

                vehicle_checks = {
                    "rc": has_document(
                        rc_doc
                    ),

                    "insurance": has_document(
                        insurance_doc
                    ),

                    "permit": has_document(
                        permit_doc
                    ),

                    "fitness": has_document(
                        fitness_doc
                    ),

                    "puc": has_document(
                        puc_doc
                    ),
                }

                vehicle_compliance_results.append(
                    vehicle_checks
                )

                if not all(
                    vehicle_checks.values()
                ):
                    all_vehicle_compliant = False

            # -------------------------------------------------
            # SIX MANDATORY CHECKS
            # -------------------------------------------------

            total_checks = 6

            passed_checks = 0

            # Driving License
            if license_passed:
                passed_checks += 1

            # Vehicle documents
            if vehicle_compliance_results:

                if all(
                    item["rc"]
                    for item
                    in vehicle_compliance_results
                ):
                    passed_checks += 1

                if all(
                    item["insurance"]
                    for item
                    in vehicle_compliance_results
                ):
                    passed_checks += 1

                if all(
                    item["permit"]
                    for item
                    in vehicle_compliance_results
                ):
                    passed_checks += 1

                if all(
                    item["fitness"]
                    for item
                    in vehicle_compliance_results
                ):
                    passed_checks += 1

                if all(
                    item["puc"]
                    for item
                    in vehicle_compliance_results
                ):
                    passed_checks += 1

            # -------------------------------------------------
            # FINAL APPROVAL CONDITION
            # -------------------------------------------------

            compliance_complete = (
                license_passed
                and bool(vehicles)
                and all_vehicle_compliant
                and passed_checks
                == total_checks
            )

            # -------------------------------------------------
            # BLOCK APPROVAL
            # -------------------------------------------------

            if not compliance_complete:

                messages.error(
                    request,
                    (
                        f"Driver #{driver.pk} "
                        f"cannot be approved. "
                        f"Mandatory compliance is "
                        f"incomplete: "
                        f"{passed_checks}/"
                        f"{total_checks} "
                        f"checks passed."
                    ),
                )

                return redirect(
                    build_onboarding_url(
                        driver.pk
                    )
                )

            # -------------------------------------------------
            # APPROVE
            # -------------------------------------------------

            try:

                with transaction.atomic():

                    update_fields = []

                    # Driver KYC status
                    driver.doc_status = (
                        "approved"
                    )

                    update_fields.append(
                        "doc_status"
                    )

                    # Driver active status
                    driver.status = "active"

                    update_fields.append(
                        "status"
                    )

                    # Existing approved flag
                    driver.approved = True

                    update_fields.append(
                        "approved"
                    )

                    # Correct timestamp update
                    driver.doc_status_updated_at = (
                        timezone.now()
                    )

                    update_fields.append(
                        "doc_status_updated_at"
                    )

                    driver.save(
                        update_fields=update_fields
                    )

                messages.success(
                    request,
                    (
                        f"Driver #{driver.pk} "
                        "and fleet approved "
                        "successfully."
                    ),
                )

            except Exception as exc:

                messages.error(
                    request,
                    (
                        "Unable to approve driver: "
                        f"{exc}"
                    ),
                )

            return redirect(
                build_onboarding_url(
                    driver.pk
                )
            )

        # =====================================================
        # REJECT DRIVER
        # =====================================================

        elif action == "reject":

            rejection_reason = (
                request.POST.get(
                    "rejection_reason",
                    "",
                ).strip()
            )

            if not rejection_reason:

                messages.error(
                    request,
                    "Please enter a rejection reason.",
                )

                return redirect(
                    build_onboarding_url(
                        driver.pk
                    )
                )

            try:

                with transaction.atomic():

                    # -------------------------------------------------
                    # EXACT MODEL FIELD
                    # Driver.doc_status
                    # -------------------------------------------------

                    driver.doc_status = (
                        "rejected"
                    )

                    # -------------------------------------------------
                    # EXACT MODEL FIELD
                    # Driver.doc_status_updated_at
                    # -------------------------------------------------

                    driver.doc_status_updated_at = (
                        timezone.now()
                    )

                    # -------------------------------------------------
                    # EXACT MODEL FIELD
                    # Driver.doc_rejection_reason
                    # -------------------------------------------------

                    driver.doc_rejection_reason = (
                        rejection_reason
                    )

                    # -------------------------------------------------
                    # Do not mark driver approved
                    # -------------------------------------------------

                    driver.approved = False

                    driver.save(
                        update_fields=[
                            "doc_status",
                            "doc_status_updated_at",
                            "doc_rejection_reason",
                            "approved",
                        ]
                    )

                messages.success(
                    request,
                    (
                        f"Driver #{driver.pk} "
                        "rejected successfully."
                    ),
                )

            except Exception as exc:

                messages.error(
                    request,
                    (
                        "Unable to reject driver: "
                        f"{exc}"
                    ),
                )

            return redirect(
                build_onboarding_url(
                    driver.pk
                )
            )

        # =====================================================
        # INVALID ACTION
        # =====================================================

        else:

            messages.error(
                request,
                "Invalid action.",
            )

            return redirect(
                build_onboarding_url(
                    driver.pk
                )
            )

    # =========================================================
    # DRIVER QUERYSET
    # =========================================================

    drivers_qs = (
        Driver.objects
        .select_related(
            "user_id"
        )
        .prefetch_related(
            "vehicle_set__vehicle_type_id"
        )
        .all()
        .order_by("-pk")
    )

    # =========================================================
    # STATUS FILTER
    # =========================================================

    if status_filter == "PENDING":

        drivers_qs = drivers_qs.filter(
            doc_status__iexact="pending"
        )

    elif status_filter == "APPROVED":

        drivers_qs = drivers_qs.filter(
            doc_status__iexact="approved"
        )

    elif status_filter == "REJECTED":

        drivers_qs = drivers_qs.filter(
            doc_status__iexact="rejected"
        )

    # =========================================================
    # SEARCH
    # =========================================================

    if search:

        search_queries = []

        # Driver ID
        try:

            search_queries.append(
                Q(
                    pk=int(search)
                )
            )

        except ValueError:
            pass

        # Actual User fields
        search_queries.append(
            Q(
                user_id__full_name__icontains=search
            )
        )

        search_queries.append(
            Q(
                user_id__phone_number__icontains=search
            )
        )

        # Vehicle number
        search_queries.append(
            Q(
                vehicle_set__vehicle_number__icontains=search
            )
        )

        combined_query = (
            search_queries[0]
        )

        for query_item in search_queries[1:]:

            combined_query |= query_item

        drivers_qs = (
            drivers_qs
            .filter(
                combined_query
            )
            .distinct()
        )

    # =========================================================
    # COUNTS
    # =========================================================

    total_drivers = (
        Driver.objects.count()
    )

    pending_count = (
        Driver.objects
        .filter(
            doc_status__iexact="pending"
        )
        .count()
    )

    approved_count = (
        Driver.objects
        .filter(
            doc_status__iexact="approved"
        )
        .count()
    )

    rejected_count = (
        Driver.objects
        .filter(
            doc_status__iexact="rejected"
        )
        .count()
    )

    # =========================================================
    # PAGINATION
    # =========================================================

    paginator = Paginator(
        drivers_qs,
        10,
    )

    page_obj = paginator.get_page(
        page_number
    )

    # =========================================================
    # DRIVER QUEUE CARDS
    # =========================================================

    driver_cards = []

    for driver in page_obj.object_list:

        # -----------------------------------------------------
        # ACTUAL DRIVER NAME
        # -----------------------------------------------------

        name = get_driver_name(
            driver
        )

        # -----------------------------------------------------
        # ACTUAL PHONE
        # -----------------------------------------------------

        phone = get_driver_phone(
            driver
        )

        # -----------------------------------------------------
        # KYC STATUS
        # -----------------------------------------------------

        doc_status = str(
            driver.doc_status
            or "pending"
        ).upper()

        # -----------------------------------------------------
        # RATING
        # -----------------------------------------------------

        rating = (
            driver.ratings
            if driver.ratings is not None
            else 0
        )

        # -----------------------------------------------------
        # TOTAL TRIPS
        # -----------------------------------------------------

        total_trips = (
            driver.total_trips
            if driver.total_trips is not None
            else 0
        )

        # -----------------------------------------------------
        # UPDATED DATE
        # -----------------------------------------------------

        updated_at = (
            driver.doc_status_updated_at
            or driver.uploaded_timestamp
        )

        # -----------------------------------------------------
        # VEHICLES
        # -----------------------------------------------------

        vehicles = (
            get_driver_vehicles(
                driver
            )
        )

        vehicle_numbers = []

        for vehicle in vehicles:

            if vehicle.vehicle_number:

                vehicle_numbers.append(
                    str(
                        vehicle.vehicle_number
                    )
                )

        if vehicle_numbers:

            vehicle_display = ", ".join(
                vehicle_numbers
            )

        elif vehicles:

            if len(vehicles) == 1:

                vehicle_display = (
                    "1 Vehicle"
                )

            else:

                vehicle_display = (
                    f"{len(vehicles)} "
                    "Vehicles"
                )

        else:

            vehicle_display = (
                "No Vehicle"
            )

        # -----------------------------------------------------
        # INITIAL
        # -----------------------------------------------------

        try:

            initial = (
                str(name)
                .strip()[0]
                .upper()
            )

        except Exception:

            initial = "D"

        # -----------------------------------------------------
        # CARD DATA
        # -----------------------------------------------------

        driver_cards.append(
            {
                "id": driver.pk,

                "name": name,

                "phone": phone,

                "status": doc_status,

                "rating": rating,

                "total_trips": total_trips,

                "vehicle_display": (
                    vehicle_display
                ),

                "vehicle_count": (
                    len(vehicles)
                ),

                "initial": initial,

                "updated_at": format_date(
                    updated_at
                ),
            }
        )

    # =========================================================
    # SELECTED DRIVER
    # =========================================================

    selected_driver = None

    selected_data = None

    if selected_driver_id:

        try:

            selected_driver = (
                Driver.objects
                .select_related(
                    "user_id"
                )
                .prefetch_related(
                    "vehicle_set__vehicle_type_id"
                )
                .get(
                    pk=selected_driver_id
                )
            )

        except (
            Driver.DoesNotExist,
            ValueError,
            TypeError,
        ):

            selected_driver = None

    # ---------------------------------------------------------
    # Default to first driver on current page
    # ---------------------------------------------------------

    if (
        selected_driver is None
        and page_obj.object_list
    ):

        selected_driver = (
            page_obj.object_list[0]
        )

    # =========================================================
    # SELECTED DRIVER DETAILS
    # =========================================================

    if selected_driver:

        driver = selected_driver

        # -----------------------------------------------------
        # ACTUAL NAME
        # -----------------------------------------------------

        name = get_driver_name(
            driver
        )

        # -----------------------------------------------------
        # ACTUAL PHONE
        # -----------------------------------------------------

        phone = get_driver_phone(
            driver
        )

        # -----------------------------------------------------
        # KYC STATUS
        # -----------------------------------------------------

        doc_status = str(
            driver.doc_status
            or "pending"
        ).upper()

        # -----------------------------------------------------
        # ACTUAL REJECTION REASON
        # -----------------------------------------------------

        rejection_reason = (
            driver.doc_rejection_reason
            or ""
        )

        # -----------------------------------------------------
        # REJECTION DATE
        # -----------------------------------------------------

        rejection_date = (
            driver.doc_status_updated_at
        )

        # -----------------------------------------------------
        # LICENSE
        # -----------------------------------------------------

        license_front = (
            driver.license_doc
        )

        license_back = (
            driver.license_doc_back
        )

        license_expiry = (
            driver.license_expiry
        )

        # -----------------------------------------------------
        # VEHICLES
        # -----------------------------------------------------

        vehicles = (
            get_driver_vehicles(
                driver
            )
        )

        vehicle_details = []

        # =====================================================
        # BUILD VEHICLE DETAILS
        # =====================================================

        for vehicle in vehicles:

            # -------------------------------------------------
            # EXACT VEHICLE DOCUMENT FIELDS
            # -------------------------------------------------

            rc_doc = (
                vehicle.rc_doc
            )

            insurance_doc = (
                vehicle.insurance_doc
            )

            permit_doc = (
                vehicle.permit_doc
            )

            fitness_doc = (
                vehicle.fitness_doc
            )

            puc_doc = (
                vehicle.puc_doc
            )

            # -------------------------------------------------
            # VEHICLE COMPLIANCE CHECKS
            # -------------------------------------------------

            checks = {

                "license": has_document(
                    license_front
                ),

                "rc": has_document(
                    rc_doc
                ),

                "insurance": has_document(
                    insurance_doc
                ),

                "permit": has_document(
                    permit_doc
                ),

                "fitness": has_document(
                    fitness_doc
                ),

                "puc": has_document(
                    puc_doc
                ),
            }

            passed = sum(
                1
                for value in checks.values()
                if value
            )

            # -------------------------------------------------
            # VEHICLE DETAILS
            # -------------------------------------------------

            vehicle_details.append(
                {
                    "id": vehicle.pk,

                    "number": (
                        vehicle.vehicle_number
                        or "N/A"
                    ),

                    "vehicle_type": (
                        get_vehicle_type(
                            vehicle
                        )
                    ),

                    "brand": (
                        vehicle.brand
                        or "N/A"
                    ),

                    "model": (
                        vehicle.model
                        or "N/A"
                    ),

                    "color": (
                        vehicle.color
                        or "N/A"
                    ),

                    "year": (
                        vehicle.year
                        or "N/A"
                    ),

                    "capacity": (
                        vehicle.capacity
                        if vehicle.capacity
                        is not None
                        else 1
                    ),

                    "status": (
                        vehicle.status
                        or "active"
                    ),

                    # -----------------------------------------
                    # RC
                    # -----------------------------------------

                    "rc": {

                        "present": (
                            has_document(
                                rc_doc
                            )
                        ),

                        "url": (
                            document_url(
                                rc_doc
                            )
                        ),

                        "expiry": (
                            format_date(
                                vehicle.rc_expiry
                                if hasattr(
                                    vehicle,
                                    "rc_expiry"
                                )
                                else None
                            )
                        ),
                    },

                    # -----------------------------------------
                    # INSURANCE
                    # -----------------------------------------

                    "insurance": {

                        "present": (
                            has_document(
                                insurance_doc
                            )
                        ),

                        "url": (
                            document_url(
                                insurance_doc
                            )
                        ),

                        "expiry": (
                            format_date(
                                vehicle.insurance_expiry
                            )
                        ),
                    },

                    # -----------------------------------------
                    # PERMIT
                    # -----------------------------------------

                    "permit": {

                        "present": (
                            has_document(
                                permit_doc
                            )
                        ),

                        "url": (
                            document_url(
                                permit_doc
                            )
                        ),

                        "expiry": (
                            format_date(
                                vehicle.permit_expiry
                            )
                        ),
                    },

                    # -----------------------------------------
                    # FITNESS
                    # -----------------------------------------

                    "fitness": {

                        "present": (
                            has_document(
                                fitness_doc
                            )
                        ),

                        "url": (
                            document_url(
                                fitness_doc
                            )
                        ),

                        "expiry": (
                            format_date(
                                vehicle.fitness_expiry
                            )
                        ),
                    },

                    # -----------------------------------------
                    # PUC
                    # -----------------------------------------

                    "puc": {

                        "present": (
                            has_document(
                                puc_doc
                            )
                        ),

                        "url": (
                            document_url(
                                puc_doc
                            )
                        ),

                        "expiry": (
                            format_date(
                                vehicle.puc_expiry
                            )
                        ),
                    },

                    "checks": checks,

                    "passed": passed,
                }
            )

        # =====================================================
        # COMPLIANCE CHECKS
        # =====================================================

        has_vehicles = bool(
            vehicle_details
        )

        def every_vehicle_has(
            key
        ):
            """
            A compliance requirement passes
            only when EVERY vehicle has that
            document.
            """

            if not vehicle_details:
                return False

            return all(
                item["checks"][key]
                for item in vehicle_details
            )

        compliance_checks = [

            # -------------------------------------------------
            # 1. LICENSE
            # -------------------------------------------------

            {
                "name": (
                    "Driving License"
                ),

                "short_name": (
                    "Driving License"
                ),

                "passed": (
                    has_document(
                        license_front
                    )
                ),

                "description": (
                    "Government-issued "
                    "commercial driving "
                    "license verification."
                ),
            },

            # -------------------------------------------------
            # 2. RC
            # -------------------------------------------------

            {
                "name": (
                    "Vehicle RC"
                ),

                "short_name": (
                    "Vehicle RC"
                ),

                "passed": (
                    every_vehicle_has(
                        "rc"
                    )
                ),

                "description": (
                    "Vehicle ownership and "
                    "registration document "
                    "issued by RTO."
                ),
            },

            # -------------------------------------------------
            # 3. INSURANCE
            # -------------------------------------------------

            {
                "name": (
                    "Commercial Insurance"
                ),

                "short_name": (
                    "Commercial Insurance"
                ),

                "passed": (
                    every_vehicle_has(
                        "insurance"
                    )
                ),

                "description": (
                    "Valid commercial "
                    "motor insurance."
                ),
            },

            # -------------------------------------------------
            # 4. PERMIT
            # -------------------------------------------------

            {
                "name": (
                    "Commercial Permit"
                ),

                "short_name": (
                    "Commercial Permit"
                ),

                "passed": (
                    every_vehicle_has(
                        "permit"
                    )
                ),

                "description": (
                    "Valid commercial "
                    "transport permit."
                ),
            },

            # -------------------------------------------------
            # 5. FITNESS
            # -------------------------------------------------

            {
                "name": (
                    "Fitness Certificate (FC)"
                ),

                "short_name": (
                    "Fitness Certificate (FC)"
                ),

                "passed": (
                    every_vehicle_has(
                        "fitness"
                    )
                ),

                "description": (
                    "Valid vehicle fitness "
                    "certificate."
                ),
            },

            # -------------------------------------------------
            # 6. PUC
            # -------------------------------------------------

            {
                "name": (
                    "Pollution Under "
                    "Control (PUC)"
                ),

                "short_name": (
                    "Pollution Under "
                    "Control (PUC)"
                ),

                "passed": (
                    every_vehicle_has(
                        "puc"
                    )
                ),

                "description": (
                    "Valid Pollution Under "
                    "Control certificate."
                ),
            },
        ]

        # =====================================================
        # COMPLIANCE SCORE
        # =====================================================

        passed_checks = sum(
            1
            for item
            in compliance_checks
            if item["passed"]
        )

        total_checks = len(
            compliance_checks
        )

        compliance_percentage = int(
            (
                passed_checks
                / total_checks
            )
            * 100
        )

        compliance_complete = (
            passed_checks
            == total_checks
            and has_vehicles
        )

        # =====================================================
        # DRIVER STATUS
        # =====================================================

        driver_status = str(
            driver.status
            or "off"
        ).upper()

        # =====================================================
        # SELECTED DRIVER DATA
        # =====================================================

        selected_data = {

            "id": driver.pk,

            # Actual User.full_name
            "name": name,

            # Actual User.phone_number
            "phone": phone,

            # KYC
            "status": doc_status,

            # Driver operational status
            "driver_status": driver_status,

            # Rating
            "rating": (
                driver.ratings
                if driver.ratings
                is not None
                else 0
            ),

            # Trips
            "total_trips": (
                driver.total_trips
                if driver.total_trips
                is not None
                else 0
            ),

            # Actual Driver.upi_id
            "upi": (
                driver.upi_id
                or "Not Set"
            ),

            # Actual Driver.doc_rejection_reason
            "rejection_reason": (
                rejection_reason
            ),

            "rejection_date": (
                format_date(
                    rejection_date
                )
            ),

            # -------------------------------------------------
            # LICENSE FRONT
            # -------------------------------------------------

            "license_front": {

                "present": (
                    has_document(
                        license_front
                    )
                ),

                "url": (
                    document_url(
                        license_front
                    )
                ),
            },

            # -------------------------------------------------
            # LICENSE BACK
            # -------------------------------------------------

            "license_back": {

                "present": (
                    has_document(
                        license_back
                    )
                ),

                "url": (
                    document_url(
                        license_back
                    )
                ),
            },

            # -------------------------------------------------
            # LICENSE EXPIRY
            # -------------------------------------------------

            "license_expiry": (
                format_date(
                    license_expiry
                )
            ),

            # -------------------------------------------------
            # VEHICLES
            # -------------------------------------------------

            "vehicles": (
                vehicle_details
            ),

            # -------------------------------------------------
            # COMPLIANCE
            # -------------------------------------------------

            "compliance_checks": (
                compliance_checks
            ),

            "passed_checks": (
                passed_checks
            ),

            "total_checks": (
                total_checks
            ),

            "compliance_percentage": (
                compliance_percentage
            ),

            "compliance_complete": (
                compliance_complete
            ),
        }

    # =========================================================
    # FINAL CONTEXT
    # =========================================================

    context = {

        # Driver queue
        "drivers": driver_cards,

        # Pagination
        "page_obj": page_obj,

        "paginator": paginator,

        # Current filters
        "status": status_filter,

        "search": search,

        # Selected driver
        "selected_driver": (
            selected_driver
        ),

        "selected": selected_data,

        # Counts
        "pending_count": (
            pending_count
        ),

        "approved_count": (
            approved_count
        ),

        "rejected_count": (
            rejected_count
        ),

        "total_drivers": (
            total_drivers
        ),
    }

    return render(
        request,
        "admin_pages/driver_onboarding.html",
        context,
    )

@admin_required
def payment_dashboard(request: HttpRequest) -> HttpResponse:
    """
    Financial Operations / Payment Gateway dashboard.

    Financial sources:
    - Payment: confirmed rider payments/revenue
    - WithdrawalRequest: driver payout requests
    - TransactionHistory: actual driver payout ledger
    - WebhookEvent: Cashfree webhook audit trail
    """

    # =========================================================
    # FINANCIAL ACTIONS
    # =========================================================
    if request.method == "POST":
        action = request.POST.get("action", "").strip().lower()
        withdrawal_id = request.POST.get("withdrawal_id", "").strip()

        # -----------------------------------------------------
        # APPROVE / REJECT SINGLE WITHDRAWAL
        # -----------------------------------------------------
        if action in {"approve", "reject"} and withdrawal_id:
            try:
                withdrawal_id_int = int(withdrawal_id)
            except (TypeError, ValueError):
                messages.error(request, "Invalid withdrawal ID.")
                return redirect("payment_dashboard")

            try:
                from django.db import transaction
                from django.utils import timezone
                from servers.rider.models import Wallet, WalletTransaction

                # =================================================
                # REJECT WITHDRAWAL
                # =================================================
                if action == "reject":
                    with transaction.atomic():
                        withdrawal = (
                            WithdrawalRequest.objects
                            .select_for_update()
                            .select_related("driver")
                            .get(id=withdrawal_id_int)
                        )

                        if withdrawal.status != "pending":
                            messages.error(
                                request,
                                (
                                    f"Withdrawal #{withdrawal.id} cannot be "
                                    f"rejected because its status is "
                                    f"'{withdrawal.status}'."
                                ),
                            )
                            return redirect("payment_dashboard")

                        wallet = (
                            Wallet.objects
                            .select_for_update()
                            .get(
                                user_id=withdrawal.driver.user_id_id
                            )
                        )

                        refund_reference = (
                            f"refund_rejected_withdrawal_{withdrawal.id}"
                        )

                        refund_idempotency_key = (
                            f"refund_rejected_withdrawal_{withdrawal.id}"
                        )

                        existing_refund = (
                            WalletTransaction.objects
                            .filter(
                                user_id=withdrawal.driver.user_id_id,
                                purpose="refund_rejected_withdrawal",
                                reference_id=refund_reference,
                                status="completed",
                            )
                            .first()
                        )

                        # Refund exactly once.
                        if not existing_refund:
                            wallet.balance += withdrawal.amount
                            wallet.save(
                                update_fields=["balance"]
                            )

                            WalletTransaction.objects.create(
                                user_id=withdrawal.driver.user_id,
                                amount=withdrawal.amount,
                                txn_type="credit",
                                status="completed",
                                purpose="refund_rejected_withdrawal",
                                reference_id=refund_reference,
                                idempotency_key=refund_idempotency_key,
                            )

                        withdrawal.status = "rejected"
                        withdrawal.processed_at = timezone.now()
                        withdrawal.admin_notes = (
                            "Rejected from Payment Dashboard."
                        )

                        withdrawal.save(
                            update_fields=[
                                "status",
                                "processed_at",
                                "admin_notes",
                            ]
                        )

                    messages.success(
                        request,
                        (
                            f"Withdrawal #{withdrawal_id_int} rejected "
                            "and wallet amount refunded."
                        ),
                    )

                    return redirect("payment_dashboard")

                # =================================================
                # APPROVE WITHDRAWAL
                # =================================================
                with transaction.atomic():
                    withdrawal = (
                        WithdrawalRequest.objects
                        .select_for_update()
                        .select_related("driver")
                        .get(id=withdrawal_id_int)
                    )

                    if withdrawal.status != "pending":
                        messages.error(
                            request,
                            (
                                f"Withdrawal #{withdrawal.id} cannot be "
                                f"approved because its status is "
                                f"'{withdrawal.status}'."
                            ),
                        )
                        return redirect("payment_dashboard")

                    withdrawal.status = "approved"
                    withdrawal.processed_at = timezone.now()
                    withdrawal.admin_notes = (
                        "Approved from Payment Dashboard."
                    )

                    withdrawal.save(
                        update_fields=[
                            "status",
                            "processed_at",
                            "admin_notes",
                        ]
                    )

                # -------------------------------------------------
                # Start payout AFTER database transaction commits.
                # -------------------------------------------------
                try:
                    from servers.driver.services import (
                        trigger_payout_creation,
                    )

                    trigger_payout_creation(withdrawal)

                    messages.success(
                        request,
                        (
                            f"Withdrawal #{withdrawal_id_int} approved "
                            "and payout processing started."
                        ),
                    )

                except Exception as payout_error:
                    logger.exception(
                        "Failed to start payout for withdrawal %s: %s",
                        withdrawal_id_int,
                        payout_error,
                    )

                    messages.error(
                        request,
                        (
                            f"Withdrawal #{withdrawal_id_int} was approved, "
                            "but payout processing could not be started."
                        ),
                    )

            except WithdrawalRequest.DoesNotExist:
                messages.error(
                    request,
                    f"Withdrawal #{withdrawal_id_int} was not found.",
                )

            except Wallet.DoesNotExist:
                logger.exception(
                    "Wallet missing for withdrawal %s",
                    withdrawal_id_int,
                )

                messages.error(
                    request,
                    (
                        "Driver wallet was not found. "
                        "Withdrawal was not rejected."
                    ),
                )

            except Exception as exc:
                logger.exception(
                    "Payment dashboard withdrawal action failed: %s",
                    exc,
                )

                messages.error(
                    request,
                    "Unable to process the withdrawal action.",
                )

            return redirect("payment_dashboard")

        # -----------------------------------------------------
        # EXPORT FINANCIAL STATEMENT
        # -----------------------------------------------------
        if action == "export_statement":
            response = HttpResponse(
                content_type="text/csv",
            )

            response["Content-Disposition"] = (
                'attachment; filename="saaradhigo_financial_statement.csv"'
            )

            writer = csv.writer(response)

            writer.writerow(
                [
                    "Type",
                    "ID",
                    "Date",
                    "Driver/User",
                    "Amount",
                    "Method",
                    "Status",
                    "Gateway",
                    "Gateway Reference",
                    "Gateway Status",
                ]
            )

            # ---------------------------------------------
            # Driver payouts
            # ---------------------------------------------
            withdrawals = (
                WithdrawalRequest.objects
                .select_related(
                    "driver",
                    "driver__user_id",
                )
                .order_by("-requested_at")
            )

            for withdrawal in withdrawals:
                driver_name = "Unknown Driver"

                try:
                    driver_name = (
                        withdrawal.driver.user_id.full_name
                        or "Unknown Driver"
                    )
                except Exception:
                    pass

                writer.writerow(
                    [
                        "Driver Payout",
                        withdrawal.id,
                        (
                            withdrawal.requested_at.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            if withdrawal.requested_at
                            else ""
                        ),
                        driver_name,
                        str(withdrawal.amount),
                        withdrawal.payout_method,
                        withdrawal.status,
                        "Cashfree",
                        withdrawal.payout_reference_id or "",
                        withdrawal.payout_status or "",
                    ]
                )

            # ---------------------------------------------
            # Completed rider payments
            # ---------------------------------------------
            completed_payments = (
                Payment.objects
                .filter(status="completed")
                .select_related("user_id")
                .order_by("-created_at")
            )

            for payment in completed_payments:
                user_name = "Unknown User"

                try:
                    user_name = (
                        payment.user_id.full_name
                        or "Unknown User"
                    )
                except Exception:
                    pass

                writer.writerow(
                    [
                        "Rider Payment",
                        payment.id,
                        (
                            payment.created_at.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            if payment.created_at
                            else ""
                        ),
                        user_name,
                        str(payment.amount),
                        payment.method,
                        payment.status,
                        payment.payment_gateway,
                        (
                            payment.gateway_payment_id
                            or payment.cashfree_payment_id
                            or payment.gateway_order_id
                            or payment.cashfree_order_id
                            or ""
                        ),
                        payment.status,
                    ]
                )

            return response

        # -----------------------------------------------------
        # BULK PAYOUT
        # -----------------------------------------------------
        if action == "bulk_approve":
            selected_ids = request.POST.getlist(
                "withdrawal_ids"
            )

            if not selected_ids:
                messages.error(
                    request,
                    "Select at least one pending withdrawal.",
                )
                return redirect("payment_dashboard")

            if len(selected_ids) > 50:
                messages.error(
                    request,
                    "You can process a maximum of 50 withdrawals at once.",
                )
                return redirect("payment_dashboard")

            from django.db import transaction
            from django.utils import timezone

            approved_withdrawals = []
            approved_count = 0
            skipped_count = 0

            for raw_id in selected_ids:
                try:
                    withdrawal_id_int = int(raw_id)
                except (TypeError, ValueError):
                    skipped_count += 1
                    continue

                try:
                    with transaction.atomic():
                        withdrawal = (
                            WithdrawalRequest.objects
                            .select_for_update()
                            .get(id=withdrawal_id_int)
                        )

                        if withdrawal.status != "pending":
                            skipped_count += 1
                            continue

                        withdrawal.status = "approved"
                        withdrawal.processed_at = timezone.now()
                        withdrawal.admin_notes = (
                            "Approved from Payment Dashboard bulk action."
                        )

                        withdrawal.save(
                            update_fields=[
                                "status",
                                "processed_at",
                                "admin_notes",
                            ]
                        )

                        approved_withdrawals.append(
                            withdrawal
                        )
                        approved_count += 1

                except WithdrawalRequest.DoesNotExist:
                    skipped_count += 1

                except Exception as exc:
                    skipped_count += 1

                    logger.exception(
                        "Bulk approval failed for withdrawal %s: %s",
                        withdrawal_id_int,
                        exc,
                    )

            # -------------------------------------------------
            # Start payouts after DB updates are committed.
            # -------------------------------------------------
            payout_started = 0
            payout_failed = 0

            if approved_withdrawals:
                try:
                    from servers.driver.services import (
                        trigger_payout_creation,
                    )

                    for withdrawal in approved_withdrawals:
                        try:
                            trigger_payout_creation(
                                withdrawal
                            )
                            payout_started += 1
                        except Exception as exc:
                            payout_failed += 1

                            logger.exception(
                                "Bulk payout start failed for "
                                "withdrawal %s: %s",
                                withdrawal.id,
                                exc,
                            )

                except Exception as exc:
                    payout_failed = len(
                        approved_withdrawals
                    )

                    logger.exception(
                        "Unable to load payout service: %s",
                        exc,
                    )

            if approved_count:
                message = (
                    f"{approved_count} withdrawal(s) approved "
                    f"and {payout_started} payout(s) started."
                )

                if payout_failed:
                    message += (
                        f" {payout_failed} payout(s) failed to start."
                    )

                if skipped_count:
                    message += (
                        f" {skipped_count} withdrawal(s) skipped."
                    )

                if payout_failed:
                    messages.warning(
                        request,
                        message,
                    )
                else:
                    messages.success(
                        request,
                        message,
                    )
            else:
                messages.error(
                    request,
                    "No pending withdrawals were approved.",
                )

            return redirect("payment_dashboard")

    # =========================================================
    # AUTHORITATIVE PAYMENT METRICS
    # =========================================================

    completed_payments = Payment.objects.filter(
        status="completed"
    )

    total_gross_revenue = (
        completed_payments.aggregate(
            total_revenue=models.Sum("amount")
        )["total_revenue"]
        or 0
    )

    completed_count = completed_payments.count()

    cancelled_count = Payment.objects.filter(
        status__in=[
            "failed",
            "refunded",
        ]
    ).count()

    # =========================================================
    # DRIVER PAYOUT REQUESTS
    # =========================================================

    recent_transactions = (
        WithdrawalRequest.objects
        .select_related(
            "driver",
            "driver__user_id",
            "driver__active_vehicle",
            "driver__active_vehicle__vehicle_type_id",
        )
        .order_by("-requested_at")
    )

    paginator = Paginator(
        recent_transactions,
        20,
    )

    page_number = request.GET.get(
        "page",
        1,
    )

    page_obj = paginator.get_page(
        page_number,
    )

    # =========================================================
    # CASHFREE GATEWAY LEDGER
    # =========================================================

    from servers.payments.models import (
        TransactionHistory,
        WebhookEvent,
    )

    # ---------------------------------------------------------
    # CASHFREE RIDER PAYMENTS
    # ---------------------------------------------------------

    gateway_payments = list(
        Payment.objects
        .filter(
            payment_gateway="cashfree",
        )
        .exclude(
            gateway_order_id__isnull=True,
            cashfree_order_id__isnull=True,
            gateway_payment_id__isnull=True,
            cashfree_payment_id__isnull=True,
        )
        .select_related("user_id")
        .order_by("-created_at")[:25]
    )

    # ---------------------------------------------------------
    # CASHFREE DRIVER PAYOUT TRANSACTIONS
    # ---------------------------------------------------------

    gateway_payouts = list(
        TransactionHistory.objects
        .filter(
            payment_gateway="cashfree",
        )
        .exclude(
            cashfree_transfer_id__isnull=True,
        )
        .exclude(
            cashfree_transfer_id="",
        )
        .select_related(
            "driver_id",
            "driver_id__user_id",
            "withdrawal_request",
        )
        .order_by("-created_at")[:25]
    )

    # ---------------------------------------------------------
    # FAILED UPI PAYOUTS
    # ---------------------------------------------------------

    failed_upi_payouts = list(
        WithdrawalRequest.objects
        .filter(
            payout_method="upi",
            status="failed",
        )
        .select_related(
            "driver",
            "driver__user_id",
        )
        .order_by(
            "-processed_at",
            "-requested_at",
        )[:25]
    )

    # ---------------------------------------------------------
    # REFUNDED RIDER PAYMENTS
    # ---------------------------------------------------------

    refunded_payments = list(
        Payment.objects
        .filter(
            status="refunded",
        )
        .select_related("user_id")
        .order_by("-updated_at")[:25]
    )

    # ---------------------------------------------------------
    # CASHFREE WEBHOOK LOG
    # ---------------------------------------------------------

    gateway_webhooks = list(
        WebhookEvent.objects
        .filter(
            gateway="cashfree",
        )
        .order_by("-received_at")[:30]
    )

    # =========================================================
    # BUILD ONE SMALL GATEWAY LEDGER
    # =========================================================

    gateway_ledger = []

    # ---------------------------------------------------------
    # Rider payments
    # ---------------------------------------------------------

    for payment in gateway_payments:
        gateway_ledger.append(
            {
                "type": "Rider Payment",
                "reference": (
                    payment.cashfree_payment_id
                    or payment.gateway_payment_id
                    or payment.cashfree_order_id
                    or payment.gateway_order_id
                    or f"PAY-{payment.id}"
                ),
                "order_id": (
                    payment.cashfree_order_id
                    or payment.gateway_order_id
                    or "-"
                ),
                "payment_id": (
                    payment.cashfree_payment_id
                    or payment.gateway_payment_id
                    or "-"
                ),
                "transfer_id": "-",
                "status": payment.status,
                "amount": payment.amount,
                "detail": payment.method,
                "created_at": payment.created_at,
                "source": "Payment",
            }
        )

    # ---------------------------------------------------------
    # Driver payouts
    # ---------------------------------------------------------

    for payout in gateway_payouts:
        driver_name = "Unknown Driver"

        try:
            driver_name = (
                payout.driver_id.user_id.full_name
                or "Unknown Driver"
            )
        except Exception:
            pass

        gateway_ledger.append(
            {
                "type": "Driver Payout",
                "reference": (
                    payout.cashfree_transfer_id
                    or f"TXN-{payout.id}"
                ),
                "order_id": "-",
                "payment_id": (
                    payout.cashfree_payment_id
                    or payout.gateway_payment_id
                    or "-"
                ),
                "transfer_id": (
                    payout.cashfree_transfer_id
                    or "-"
                ),
                "status": (
                    payout.status
                    or "unknown"
                ),
                "amount": payout.amount,
                "detail": driver_name,
                "created_at": payout.created_at,
                "source": "TransactionHistory",
            }
        )

    # ---------------------------------------------------------
    # Failed UPI payouts
    # ---------------------------------------------------------

    for withdrawal in failed_upi_payouts:
        driver_name = "Unknown Driver"

        try:
            driver_name = (
                withdrawal.driver.user_id.full_name
                or "Unknown Driver"
            )
        except Exception:
            pass

        gateway_ledger.append(
            {
                "type": "UPI Failure",
                "reference": (
                    withdrawal.payout_reference_id
                    or f"WD-{withdrawal.id}"
                ),
                "order_id": "-",
                "payment_id": "-",
                "transfer_id": "-",
                "status": (
                    withdrawal.payout_status
                    or "failed"
                ),
                "amount": withdrawal.amount,
                "detail": (
                    withdrawal.failure_reason
                    or driver_name
                ),
                "created_at": (
                    withdrawal.processed_at
                    or withdrawal.requested_at
                ),
                "source": "WithdrawalRequest",
            }
        )

    # ---------------------------------------------------------
    # Refunded rider payments
    # ---------------------------------------------------------

    for payment in refunded_payments:
        gateway_ledger.append(
            {
                "type": "Rider Refund",
                "reference": (
                    payment.cashfree_payment_id
                    or payment.gateway_payment_id
                    or f"PAY-{payment.id}"
                ),
                "order_id": (
                    payment.cashfree_order_id
                    or payment.gateway_order_id
                    or "-"
                ),
                "payment_id": (
                    payment.cashfree_payment_id
                    or payment.gateway_payment_id
                    or "-"
                ),
                "transfer_id": "-",
                "status": "refunded",
                "amount": payment.amount,
                "detail": "Payment refunded",
                "created_at": payment.updated_at,
                "source": "Payment",
            }
        )

    # ---------------------------------------------------------
    # Cashfree webhooks
    # ---------------------------------------------------------

    for webhook in gateway_webhooks:
        gateway_ledger.append(
            {
                "type": "Webhook",
                "reference": webhook.dedupe_key,
                "order_id": "-",
                "payment_id": "-",
                "transfer_id": "-",
                "status": (
                    webhook.result
                    or "received"
                ),
                "amount": 0,
                "detail": (
                    webhook.event_type
                    or "Cashfree Event"
                ),
                "created_at": webhook.received_at,
                "source": "WebhookEvent",
            }
        )

    # Newest gateway activity first.
    gateway_ledger.sort(
        key=lambda item: (
            item["created_at"]
            or ""
        ),
        reverse=True,
    )

    # Keep dashboard rendering small.
    gateway_ledger = gateway_ledger[:50]

    # =========================================================
    # GATEWAY SUMMARY
    # =========================================================

    cashfree_payment_count = Payment.objects.filter(
        payment_gateway="cashfree",
    ).count()

    cashfree_refund_count = Payment.objects.filter(
        payment_gateway="cashfree",
        status="refunded",
    ).count()

    failed_upi_count = WithdrawalRequest.objects.filter(
        payout_method="upi",
        status="failed",
    ).count()

    webhook_count = WebhookEvent.objects.filter(
        gateway="cashfree",
    ).count()

    # =========================================================
    # RENDER DASHBOARD
    # =========================================================

    return render(
        request,
        "admin_pages/payment_dashboard.html",
        {
            # Existing metrics
            "total_payments": total_gross_revenue,
            "completed_count": completed_count,
            "cancelled_count": cancelled_count,
            "page_obj": page_obj,

            # Gateway ledger
            "gateway_ledger": gateway_ledger,
            "gateway_payments": gateway_payments,
            "gateway_payouts": gateway_payouts,
            "failed_upi_payouts": failed_upi_payouts,
            "refunded_payments": refunded_payments,
            "gateway_webhooks": gateway_webhooks,

            # Gateway summary
            "cashfree_payment_count": cashfree_payment_count,
            "cashfree_refund_count": cashfree_refund_count,
            "failed_upi_count": failed_upi_count,
            "webhook_count": webhook_count,
        },
    )

@admin_required
def executive_revenue(request: HttpRequest) -> HttpResponse:
    tz = timezone.get_current_timezone()
    start_date = (request.GET.get("start_date") or "").strip()
    end_date = (request.GET.get("end_date") or "").strip()
    # ---------------------------------------------------------
    # DATE PARSER
    # ---------------------------------------------------------
    def parse_date(value: str):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    start_day = parse_date(start_date)
    end_day = parse_date(end_date)
    # ---------------------------------------------------------
    # 1. BASE QUERY - COMPLETED TRIPS
    # ---------------------------------------------------------
    trip_qs = Trip.objects.filter(
        status_id__status_code="completed"
    )
    if start_day:
        start_dt = timezone.make_aware(
            datetime.combine(start_day, time.min),
            tz
        )
        trip_qs = trip_qs.filter(
            completed_at__gte=start_dt
        )
    if end_day:
        end_dt = timezone.make_aware(
            datetime.combine(
                end_day + timedelta(days=1),
                time.min
            ),
            tz
        )
        trip_qs = trip_qs.filter(
            completed_at__lt=end_dt
        )
    completed_trips = trip_qs.select_related(
        "requested_vehicle_type",
        "vehicle_id__vehicle_type_id",
    )
    # ---------------------------------------------------------
    # 2. TOTAL RIDES
    # ---------------------------------------------------------
    total_rides = completed_trips.count()
    # ---------------------------------------------------------
    # 3. CALCULATE REVENUE PER TRIP
    #
    # Priority:
    #     1. final_fare
    #     2. estimated_fare
    #     3. 0
    # ---------------------------------------------------------
    trip_revenues = []
    for trip in completed_trips:
        # Use final fare when available
        fare = trip.final_fare
        # Fall back to estimated fare
        if fare is None:
            fare = trip.estimated_fare
        # If both are empty, use zero
        fare = Decimal(str(fare or "0.00"))
        # -----------------------------------------------------
        # VEHICLE CLASS
        #
        # Prefer requested vehicle type.
        # Fall back to actual vehicle type.
        # -----------------------------------------------------
        vehicle_type = trip.requested_vehicle_type
        if not vehicle_type and trip.vehicle_id:
            vehicle_type = trip.vehicle_id.vehicle_type_id
        if vehicle_type:
            vehicle_name = (
                getattr(vehicle_type, "type", None)
                or "Unknown"
            )
        else:
            vehicle_name = "Unknown"
        trip_revenues.append({
            "trip": trip,
            "trip_id": trip.pk,
            "vehicle": vehicle_name,
            "fare": fare,
            "completed_at": trip.completed_at,
        })
    # ---------------------------------------------------------
    # 4. GROSS BOOKING VALUE
    # ---------------------------------------------------------
    gbv = sum(
        item["fare"]
        for item in trip_revenues
    )
    gbv = Decimal(str(gbv or "0.00"))
    # ---------------------------------------------------------
    # 5. PLATFORM REVENUE
    # ---------------------------------------------------------
    platform_revenue = Decimal("0.00")
    total_commission_rate = Decimal("0.00")
    for item in trip_revenues:
        trip = item["trip"]
        fare = item["fare"]
    # Get commission rate from the trip's RateCard
        commission_rate = commission_percent_for_trip(trip)
        commission_rate = Decimal(
            str(commission_rate or "0.00"))
    # Calculate platform commission for this trip
        commission = (fare * commission_rate / Decimal("100")).quantize(Decimal("0.01"))
        platform_revenue += commission
        total_commission_rate += (commission_rate * fare )
# Weighted average take rate across completed trips
    if gbv > 0:
        take_rate = (
            total_commission_rate / gbv
        ).quantize(
            Decimal("0.1")
        )
    else:
        take_rate = Decimal("0.0")
    
    # ---------------------------------------------------------
    # 6. TOTAL / UTILIZED DRIVERS
    # ---------------------------------------------------------
    total_drivers = Driver.objects.count()
    if total_rides:
        utilized_drivers = (
            completed_trips
            .exclude(driver_id__isnull=True)
            .values_list("driver_id", flat=True)
            .distinct()
            .count()
        )
    else:
        utilized_drivers = 0
    fleet_utilization = round(
        (
            utilized_drivers / total_drivers * 100
        )
        if total_drivers
        else 0,
        1,
    )
    # ---------------------------------------------------------
    # 7. REVENUE BY VEHICLE CLASS
    # ---------------------------------------------------------
    bucketed = {}
    for item in trip_revenues:
        vehicle_name = item["vehicle"]
        fare = item["fare"]
        bucketed[vehicle_name] = (
            bucketed.get(
                vehicle_name,
                Decimal("0.00")
            )
            + fare
        )
    # ---------------------------------------------------------
    # 8. FORMAT VEHICLE CLASS BREAKDOWN
    # ---------------------------------------------------------
    aggregated_breakdown = []
    for name, revenue in sorted(
        bucketed.items(),
        key=lambda item: item[1],
        reverse=True,
    ):
        if gbv > 0:
            pct = round(
                (revenue / gbv * 100),
                1,
            )
        else:
            pct = 0
        aggregated_breakdown.append({
            "name": name,
            "revenue": revenue,
            "pct": pct,
        })
    # ---------------------------------------------------------
    # 9. MONTHLY REVENUE
    #
    # IMPORTANT:
    # Uses the SAME final_fare -> estimated_fare fallback
    # as GBV and vehicle-class revenue.
    # ---------------------------------------------------------
    monthly_data = {}
    for item in trip_revenues:
        completed_at = item["completed_at"]
        if not completed_at:
            continue
        month_key = completed_at.strftime("%Y-%m")
        month_name = completed_at.strftime("%b %Y")
        fare = item["fare"]
        if month_key not in monthly_data:
            monthly_data[month_key] = {
                "month": month_name,
                "revenue": Decimal("0.00"),
                "rides": 0,
            }
        monthly_data[month_key]["revenue"] += fare
        monthly_data[month_key]["rides"] += 1
    monthly_revenue = sorted(
        monthly_data.values(),
        key=lambda x: x["month"],
    )
    # ---------------------------------------------------------
    # 10. MONTH-OVER-MONTH GROWTH
    # ---------------------------------------------------------
    growth_trajectory = []
    previous_revenue = None
    for item in monthly_revenue:
        current_revenue = item["revenue"]
        if (
            previous_revenue is not None
            and previous_revenue > 0
        ):
            growth = (
                (
                    current_revenue
                    - previous_revenue
                )
                / previous_revenue
            ) * 100
            growth = round(growth, 1)
        else:
            growth = 0
        growth_trajectory.append({
            "month": item["month"],
            "revenue": current_revenue,
            "rides": item["rides"],
            "growth": growth,
        })
        previous_revenue = current_revenue
    # ---------------------------------------------------------
    # 11. CURRENT MONTH GROWTH
    # ---------------------------------------------------------
    revenue_growth = 0
    if len(monthly_revenue) >= 2:
        current = monthly_revenue[-1]["revenue"]
        previous = monthly_revenue[-2]["revenue"]
        if previous > 0:
            revenue_growth = round(
                (
                    (
                        current
                        - previous
                    )
                    / previous
                ) * 100,
                1,
            )
    # ---------------------------------------------------------
    # 12. BAR CHART DATA
    # ---------------------------------------------------------
    max_monthly_revenue = max(
        (
            item["revenue"]
            for item in monthly_revenue
        ),
        default=Decimal("0.00"),
    )
    for item in monthly_revenue:
        if max_monthly_revenue > 0:
            item["height"] = round(
                (
                    item["revenue"]
                    / max_monthly_revenue
                ) * 100,
                1,
            )
        else:
            item["height"] = 0
    # ---------------------------------------------------------
    # 13. CONTEXT
    # ---------------------------------------------------------
    context = {
        "start_date": start_date,
        "end_date": end_date,
        "gbv": gbv,
        "platform_revenue": platform_revenue,
        "take_rate": take_rate,
        "total_rides": total_rides,
        "total_drivers": total_drivers,
        "utilized_drivers": utilized_drivers,
        "fleet_utilization": fleet_utilization,
        "class_breakdown": aggregated_breakdown,
        "monthly_revenue": monthly_revenue,
        "growth_trajectory": growth_trajectory,
        "revenue_growth": revenue_growth,
    }
    return render(request,"admin_pages/executive_revenue.html",context,)

@admin_required
def dispute_support(request: HttpRequest) -> HttpResponse:

    # =========================================================
    # ADMIN ACTIONS
    # =========================================================

    if request.method == "POST":

        ticket_id = request.POST.get("ticket_id")
        action = request.POST.get("action")

        ticket = get_object_or_404(
            SupportTicket,
            id=ticket_id
        )

        # Keep current filters after POST
        search = request.POST.get("search", "")
        page = request.POST.get("page", "1")

        # -----------------------------------------------------
        # REPLY
        # -----------------------------------------------------

        if action == "reply":

            reply = request.POST.get("reply", "").strip()

            if not reply:

                messages.error(
                    request,
                    "Please enter a reply."
                )

            elif ticket.status == "CLOSED":

                messages.error(
                    request,
                    "This ticket is already closed."
                )

            else:

                SupportMessage.objects.create(
                    ticket=ticket,
                    author=request.user,
                    author_role="support",
                    body=reply,
                )

                # OPEN -> IN_PROGRESS
                if ticket.status == "OPEN":

                    ticket.status = "IN_PROGRESS"

                    ticket.save(
                        update_fields=["status"]
                    )

                messages.success(
                    request,
                    "Reply sent successfully."
                )

            return redirect(
                f"{request.path}"
                f"?selected_ticket={ticket.id}"
                f"&page={page}"
                f"&search={search}"
            )

        # -----------------------------------------------------
        # CLOSE
        # -----------------------------------------------------

        elif action == "close":

            if ticket.status != "CLOSED":

                ticket.status = "CLOSED"
                ticket.resolved_at = timezone.now()

                ticket.save(
                    update_fields=[
                        "status",
                        "resolved_at",
                        "updated_at",
                    ]
                )

                messages.success(
                    request,
                    "Ticket closed successfully."
                )

            return redirect(
                f"{request.path}"
                f"?selected_ticket={ticket.id}"
                f"&page={page}"
                f"&search={search}"
            )

        # -----------------------------------------------------
        # REOPEN
        # -----------------------------------------------------

        elif action == "reopen":

            if ticket.status == "CLOSED":

                ticket.status = "OPEN"
                ticket.resolved_at = None

                ticket.save(
                    update_fields=[
                        "status",
                        "resolved_at",
                        "updated_at",
                    ]
                )

                messages.success(
                    request,
                    "Ticket reopened successfully."
                )

            return redirect(
                f"{request.path}"
                f"?selected_ticket={ticket.id}"
                f"&page={page}"
                f"&search={search}"
            )

    # =========================================================
    # TICKET QUERYSET
    # =========================================================

    tickets_qs = (
        SupportTicket.objects
        .all()
        .order_by("-created_at")
    )

    # =========================================================
    # SEARCH
    # =========================================================

    search = request.GET.get(
        "search",
        ""
    ).strip()

    if search:

        search_filter = (
            Q(description__icontains=search)
            |
            Q(issue_type__icontains=search)
        )

        # Search by numeric ticket ID
        if search.isdigit():

            search_filter |= Q(
                id=int(search)
            )

        # Search rider information if available
        try:

            search_filter |= Q(
                user_id__full_name__icontains=search
            )

        except Exception:

            pass

        try:

            search_filter |= Q(
                user_id__phone_number__icontains=search
            )

        except Exception:

            pass

        tickets_qs = tickets_qs.filter(
            search_filter
        )

    # =========================================================
    # STATUS COUNTS
    # =========================================================

    all_tickets = SupportTicket.objects.all()

    open_count = all_tickets.filter(
        status="OPEN"
    ).count()

    in_progress_count = all_tickets.filter(
        status="IN_PROGRESS"
    ).count()

    waiting_count = all_tickets.filter(
        status="WAITING_USER"
    ).count()

    closed_count = all_tickets.filter(
        status="CLOSED"
    ).count()

    total_count = all_tickets.count()

    # =========================================================
    # URGENT TICKETS
    # =========================================================

    urgent_issue_types = {
        "safety",
        "account",
        "app_bug",
    }

    urgent_count = all_tickets.filter(
        status__in=[
            "OPEN",
            "IN_PROGRESS",
            "WAITING_USER",
        ],
        issue_type__in=urgent_issue_types,
    ).count()

    # =========================================================
    # PREPARE TICKETS
    # =========================================================

    for ticket in tickets_qs:

        # Priority
        ticket.priority = (
            "URGENT"
            if ticket.issue_type in urgent_issue_types
            else "NORMAL"
        )

        # Rider name
        ticket.rider_name = (
            getattr(
                ticket.user_id,
                "full_name",
                None
            )
            or
            getattr(
                ticket.user_id,
                "phone_number",
                None
            )
            or
            "Unknown rider"
        )

        # Driver
        ticket.driver = None

        # Status display
        try:

            ticket.status_display = (
                ticket.get_status_display()
            )

        except Exception:

            ticket.status_display = ticket.status

        # Issue display
        try:

            ticket.issue_type_display = (
                ticket.get_issue_type_display()
            )

        except Exception:

            ticket.issue_type_display = (
                ticket.issue_type
            )

    # =========================================================
    # PAGINATION
    # =========================================================

    paginator = Paginator(
        tickets_qs,
        5
    )

    page_number = request.GET.get(
        "page"
    )

    page_obj = paginator.get_page(
        page_number
    )

    tickets = page_obj.object_list

    # =========================================================
    # SELECTED TICKET
    # =========================================================

    selected_ticket_id = request.GET.get(
        "selected_ticket"
    )

    selected_ticket = None

    if selected_ticket_id:

        selected_ticket = (
            SupportTicket.objects
            .filter(
                id=selected_ticket_id
            )
            .first()
        )

        if selected_ticket:

            selected_ticket.priority = (
                "URGENT"
                if selected_ticket.issue_type
                in urgent_issue_types
                else "NORMAL"
            )

            selected_ticket.rider_name = (
                getattr(
                    selected_ticket.user_id,
                    "full_name",
                    None
                )
                or
                getattr(
                    selected_ticket.user_id,
                    "phone_number",
                    None
                )
                or
                "Unknown rider"
            )

            selected_ticket.driver = None

            try:

                selected_ticket.status_display = (
                    selected_ticket.get_status_display()
                )

            except Exception:

                selected_ticket.status_display = (
                    selected_ticket.status
                )

            try:

                selected_ticket.issue_type_display = (
                    selected_ticket.get_issue_type_display()
                )

            except Exception:

                selected_ticket.issue_type_display = (
                    selected_ticket.issue_type
                )

    # =========================================================
    # DEFAULT SELECTED TICKET
    # =========================================================

    if not selected_ticket and tickets:

        selected_ticket = tickets[0]

    # =========================================================
    # CONTEXT
    # =========================================================

    context = {

        "page_obj": page_obj,

        "tickets": tickets,

        "selected_ticket": selected_ticket,

        "search": search,

        # Counts
        "open_count": open_count,
        "in_progress_count": in_progress_count,
        "waiting_count": waiting_count,
        "closed_count": closed_count,
        "urgent_count": urgent_count,
        "total_count": total_count,
    }

    return render(
        request,
        "admin_pages/dispute_support.html",
        context
    )
@admin_required
def driver_loyalty(request: HttpRequest) -> HttpResponse:
    # ==========================================================
    # COMPLETED TRIPS
    # ==========================================================
    completed_trips = Trip.objects.filter(
        status_id__status_code="completed",
        driver_id__isnull=False,
    )
    # ==========================================================
    # FLEET STATISTICS
    # ==========================================================
    total_miles = completed_trips.aggregate(
        total=Sum(
            Coalesce(
                "actual_distance_km",
                "estimated_distance_km",
                output_field=DecimalField(
                    max_digits=12,
                    decimal_places=2,
                ),
            )
        )
    )["total"] or Decimal("0")
    total_revenue = completed_trips.aggregate(
        total=Sum(
            Coalesce(
                "final_fare",
                "estimated_fare",
                output_field=DecimalField(
                    max_digits=12,
                    decimal_places=2,
                ),
            )
        )
    )["total"] or Decimal("0")
    # ==========================================================
    # ACTIVE LOYALTY DRIVERS
    # ==========================================================
    active_loyalty_drivers = Driver.objects.filter(
        approved=True,
        status__in=[
            "online",
            "active",
            "on ride",
            "off ride",
        ],
    ).count()
    # ==========================================================
    # AVERAGE DRIVER RATING
    # ==========================================================
    avg_rating = Driver.objects.aggregate(
        average=Avg("ratings")
    )["average"] or Decimal("0")
    # ==========================================================
    # FLEET COMPLETION RATE
    # ==========================================================
    total_assigned = Trip.objects.filter(
        driver_id__isnull=False
    ).count()
    completed_count = completed_trips.count()
    if total_assigned > 0:
        fleet_completion_rate = (
            completed_count / total_assigned
        ) * 100
    else:
        fleet_completion_rate = 0
    # ==========================================================
    # GOLDEN MILES / LOYALTY TARGET
    # ==========================================================
    # Monthly fleet target.
    # Change this value based on your actual business target.
    monthly_target_miles = Decimal("100000")
    goal_percentage = (
        total_miles / monthly_target_miles
    ) * 100 if monthly_target_miles else Decimal("0")
    goal_percentage = min(
        goal_percentage,
        Decimal("100")
    )
    remaining_target = max(
        monthly_target_miles - total_miles,
        Decimal("0"),
    )
    # ==========================================================
    # FLEET LOYALTY TIER
    # ==========================================================
    if goal_percentage >= 100:
        loyalty_tier = "DIAMOND ELITE"
        unlocked_incentive = "Fuel Rebate 12%"
        next_milestone = "Premium Fleet Benefits"
    elif goal_percentage >= 75:
        loyalty_tier = "PLATINUM"
        unlocked_incentive = "Fuel Rebate 8%"
        next_milestone = "Fuel Rebate 12%"
    elif goal_percentage >= 50:
        loyalty_tier = "GOLD"
        unlocked_incentive = "Fuel Rebate 5%"
        next_milestone = "Fuel Rebate 8%"
    elif goal_percentage >= 25:
        loyalty_tier = "SILVER"
        unlocked_incentive = "Priority Support"
        next_milestone = "Fuel Rebate 5%"
    else:
        loyalty_tier = "BRONZE"
        unlocked_incentive = "Basic Loyalty Benefits"
        next_milestone = "Priority Support"
    # ==========================================================
    # DRIVER QUERYSET
    # ==========================================================
    drivers_qs = (
        Driver.objects
        .select_related("user_id")
        .annotate(
            # ----------------------------------------------
            # COMPLETED TRIPS
            # ----------------------------------------------
            completed_trip_count=Count(
                "trips",
                filter=Q(
                    trips__status_id__status_code="completed"
                ),
                distinct=True,
            ),
            # ----------------------------------------------
            # REVENUE
            # ----------------------------------------------
            revenue=Sum(
                Coalesce(
                    "trips__final_fare",
                    "trips__estimated_fare",
                    output_field=DecimalField(
                        max_digits=12,
                        decimal_places=2,
                    ),
                ),
                filter=Q(
                    trips__status_id__status_code="completed"
                ),
            ),
            # ----------------------------------------------
            # DISTANCE
            # ----------------------------------------------
            distance_km=Sum(
                Coalesce(
                    "trips__actual_distance_km",
                    "trips__estimated_distance_km",
                    output_field=DecimalField(
                        max_digits=12,
                        decimal_places=2,
                    ),
                ),
                filter=Q(
                    trips__status_id__status_code="completed"
                ),
            ),
        )
        .order_by(
            "-completed_trip_count",
            "-ratings",
        )
    )
    # ==========================================================
    # PAGINATION
    # ==========================================================
    paginator = Paginator(
        drivers_qs,
        12
    )
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(
        page_number
    )
    # ==========================================================
    # DRIVER CARD DATA
    # ==========================================================
    drivers = []
    for driver in page_obj:
        trips = driver.completed_trip_count or 0
        rating = driver.ratings or Decimal("0")
        revenue = driver.revenue or Decimal("0")
        distance = driver.distance_km or Decimal("0")
        # ----------------------------------------------
        # DRIVER LOYALTY TIER
        # ----------------------------------------------
        if trips >= 500:
            driver_tier = "PLATINUM"
        elif trips >= 250:
            driver_tier = "GOLD"
        elif trips >= 100:
            driver_tier = "SILVER"
        else:
            driver_tier = "BRONZE"
        # ----------------------------------------------
        # DRIVER NAME
        # ----------------------------------------------
        if driver.user_id.full_name:
            driver_name = driver.user_id.full_name
        else:
            driver_name = driver.user_id.phone_number
        # ----------------------------------------------
        # DRIVER DATA
        # ----------------------------------------------
        drivers.append({
            "id": driver.id,
            "name": driver_name,
            "loyalty_tier": driver_tier,
            "rating": round(
                float(rating),
                2
            ),
            "total_trips": trips,
            "total_distance": round(
                float(distance),
                2
            ),
            "total_revenue": round(
                float(revenue),
                2
            ),
            "status": driver.status,
            "approved": driver.approved,
        })
    # ==========================================================
    # CONTEXT
    # ==========================================================
    context = {
        # ----------------------------------------------
        # DRIVERS
        # ----------------------------------------------
        "drivers": drivers,
        "page_obj": page_obj,
        # ----------------------------------------------
        # TOP KPI
        # ----------------------------------------------
        "total_miles": round(
            float(total_miles),
            2
        ),
        "active_loyalty_drivers": active_loyalty_drivers,
        # ----------------------------------------------
        # REVENUE
        # ----------------------------------------------
        "total_revenue": round(
            float(total_revenue),
            2
        ),
        # ----------------------------------------------
        # RATING
        # ----------------------------------------------
        "average_rating": round(
            float(avg_rating),
            2
        ),
        # ----------------------------------------------
        # COMPLETION
        # ----------------------------------------------
        "fleet_completion_rate": round(
            float(fleet_completion_rate),
            1
        ),
        # ----------------------------------------------
        # GOLDEN MILES
        # ----------------------------------------------
        "goal_percentage": round(
            float(goal_percentage),
            1
        ),
        "monthly_target_miles": round(
            float(monthly_target_miles),
            2
        ),
        "remaining_target": round(
            float(remaining_target),
            2
        ),
        # ----------------------------------------------
        # LOYALTY
        # ----------------------------------------------
        "loyalty_tier": loyalty_tier,
        "unlocked_incentive": unlocked_incentive,
        "next_milestone": next_milestone,
    }
    # ==========================================================
    # RENDER
    # ==========================================================
    return render(
        request,
        "admin_pages/driver_loyalty.html",
        context,
    )
@admin_required
def fare_surge(request: HttpRequest) -> HttpResponse:
    now = timezone.now()
    one_hour_ago = now - timedelta(hours=1)
    # ---------------------------------------------------------
    # VEHICLE TYPES
    # ---------------------------------------------------------
    vehicle_types = (
        VehicleType.objects
        .all()
        .order_by('type')
    )
    # ---------------------------------------------------------
    # ALL ACTIVE ZONES
    # ---------------------------------------------------------
    zones = (
        ServiceZone.objects
        .filter(is_active=True)
        .order_by('name')
    )
    # ---------------------------------------------------------
    # SELECTED ZONE
    #
    # If zone_id is provided:
    #     use that zone
    #
    # Otherwise:
    #     use first active zone
    # ---------------------------------------------------------
    selected_zone_id = request.GET.get('zone_id')
    selected_zone = None
    if selected_zone_id:
        try:
            selected_zone = (
                ServiceZone.objects
                .get(
                     id=selected_zone_id,
                    is_active=True,
                )
            )
        except ServiceZone.DoesNotExist:
            selected_zone = zones.first()
    else:
        selected_zone = zones.first()
    # ---------------------------------------------------------
    # VEHICLE CONFIGURATIONS FOR SELECTED ZONE
    # ---------------------------------------------------------
    vehicle_configs = []
    if selected_zone:
        for vehicle_type in vehicle_types:
            config = (
                RateCard.objects
                .filter(
                    zone=selected_zone,
                    vehicle_type=vehicle_type,
                    is_active=True,
                    effective_from__lte=now,
                )
                .filter(
                    Q(effective_to__isnull=True) |
                    Q(effective_to__gt=now)
                )
                .order_by(
                    '-effective_from',
                    '-version',
                )
                .first()
            )
            vehicle_configs.append(
                {
                    'vehicle_type': vehicle_type,
                    'config': config,
                }
            )
    # ---------------------------------------------------------
    # CURRENT FARE CONFIGURATION
    #
    # Only for the selected zone
    # ---------------------------------------------------------
    config = None
    if selected_zone:
        config = (
            RateCard.objects
            .filter(
                zone=selected_zone,
                is_active=True,
                effective_from__lte=now,
            )
            .filter(
                Q(effective_to__isnull=True) |
                Q(effective_to__gt=now)
            )
            .order_by(
                '-effective_from',
                '-version',
            )
            .first()
        )
    # ---------------------------------------------------------
    # RECENT TRIPS - LAST ONE HOUR
    # ---------------------------------------------------------
    recent_trips = Trip.objects.filter(
        requested_at__gte=one_hour_ago
    )
    # ---------------------------------------------------------
    # ACTIVE SURGE ZONES
    # ---------------------------------------------------------
    surge_zones = (
        ServiceZone.objects
        .filter(
            is_active=True,
            trips__requested_at__gte=one_hour_ago,
            trips__surge_multiplier__gt=1,
        )
        .annotate(
            requests_per_hour=Count(
                'trips',
                distinct=True,
            ),
            active_drivers=Count(
                'trips__driver_id',
                filter=Q(
                    trips__driver_id__isnull=False,
                ),
                distinct=True,
            ),
            multiplier=Max(
                'trips__surge_multiplier',
            ),
        )
        .order_by(
            '-multiplier',
            '-requests_per_hour',
        )
    )
    # ---------------------------------------------------------
    # ACTIVE SURGE ZONES COUNT
    # ---------------------------------------------------------
    active_zones_count = surge_zones.count()
    # ---------------------------------------------------------
    # PEAK SURGE ZONE
    # ---------------------------------------------------------
    peak_zone = surge_zones.first()
    # ---------------------------------------------------------
    # AVERAGE SURGE MULTIPLIER
    # ---------------------------------------------------------
    avg_multiplier = (
        recent_trips
        .filter(
            surge_multiplier__gt=1
        )
        .aggregate(
            avg=Avg('surge_multiplier')
        )
        .get('avg')
    )
    if avg_multiplier is None:
        avg_multiplier = 1.0
    # ---------------------------------------------------------
    # CONTEXT
    # ---------------------------------------------------------
    context = {
        'config': config,
        'surge_zones': surge_zones,
        'peak_zone': peak_zone,
        'avg_multiplier': round(
            float(avg_multiplier),
            2
        ),
        'active_zones_count':
            active_zones_count,
        'recent_trips':
            recent_trips,
        'vehicle_configs':
            vehicle_configs,
        'zones':
            zones,
        'selected_zone':
            selected_zone,
    }
    # ---------------------------------------------------------
    # RENDER PAGE
    # ---------------------------------------------------------
    return render(
        request,
        "admin_pages/fare_surge.html",
        context
    )
@staff_member_required
@require_http_methods(["GET", "POST"])
def update_global_config(request):
    # ============================================================
    # GET
    # ============================================================
    if request.method == "GET":
        zone_id = request.GET.get("zone_id")
        if not zone_id:
            return JsonResponse({
                "success": False,
                "message": "Zone ID is required.",
                "configurations": {}
            }, status=400)
        try:
            zone = ServiceZone.objects.get(
                id=zone_id,
                is_active=True
            )
        except ServiceZone.DoesNotExist:
            return JsonResponse({
                "success": False,
                "message": "Selected zone does not exist or is inactive.",
                "configurations": {}
            }, status=404)
        try:
            rate_cards = (
                RateCard.objects
                .filter(
                    zone=zone,
                    is_active=True
                )
                .select_related("vehicle_type")
                .order_by("vehicle_type__type")
            )
            configurations = {}
            for rate_card in rate_cards:
                if not rate_card.vehicle_type:
                    continue
                vehicle_type = rate_card.vehicle_type
                vehicle_id = str(vehicle_type.id)
                configurations[vehicle_id] = {
                    "id": rate_card.id,
                    "vehicleTypeId": vehicle_type.id,
                    "vehicleName": str(vehicle_type.type),
                    "baseFare": str(
                        rate_card.base_fare
                        if rate_card.base_fare is not None
                        else ""
                    ),
                    "perKmFare": str(
                        rate_card.per_km_fare
                        if rate_card.per_km_fare is not None
                        else ""
                    ),
                    "perMinFare": str(
                        rate_card.per_min_fare
                        if rate_card.per_min_fare is not None
                        else ""
                    ),
                    "surgeCap": str(
                        rate_card.surge_cap_multiplier
                        if rate_card.surge_cap_multiplier is not None
                        else "1"
                    ),
                    "nightSurge": str(
                        rate_card.night_surge_multiplier
                        if rate_card.night_surge_multiplier is not None
                        else "1"
                    ),
                }
            return JsonResponse({
                "success": True,
                "zone_id": zone.id,
                "zone_name": zone.name,
                "configurations": configurations,
                "vehicle_count": len(configurations)
            })
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.exception(
                "Error loading fare configuration for zone %s",
                zone_id
            )
            return JsonResponse({
                "success": False,
                "message": "Unable to load fare configuration.",
                "error": str(e),
                "configurations": {}
            }, status=500)
    # ============================================================
    # POST
    # ============================================================
    zone_id = request.POST.get("zone_id")
    vehicle_type_id = request.POST.get("vehicle_type_id")
    base_fare = request.POST.get("base_fare")
    per_km_fare = request.POST.get("per_km_fare")
    per_min_fare = request.POST.get("per_min_fare")
    surge_cap_multiplier = request.POST.get("surge_cap_multiplier")
    night_surge_multiplier = request.POST.get("night_surge_multiplier")
    # ------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------
    if not zone_id:
        return JsonResponse({
            "success": False,
            "message": "Zone is required."
        }, status=400)
    if not vehicle_type_id:
        return JsonResponse({
            "success": False,
            "message": "Vehicle type is required."
        }, status=400)
    required_fields = {
        "Base fare": base_fare,
        "Per KM fare": per_km_fare,
        "Per minute fare": per_min_fare,
        "Surge cap": surge_cap_multiplier,
        "Night surge": night_surge_multiplier,
    }
    for field_name, value in required_fields.items():
        if value in [None, ""]:
            return JsonResponse({
                "success": False,
                "message": f"{field_name} is required."
            }, status=400)
    # ------------------------------------------------------------
    # Convert values
    # ------------------------------------------------------------
    try:
        base_fare = Decimal(base_fare)
        per_km_fare = Decimal(per_km_fare)
        per_min_fare = Decimal(per_min_fare)
        surge_cap_multiplier = Decimal(surge_cap_multiplier)
        night_surge_multiplier = Decimal(night_surge_multiplier)
    except (InvalidOperation, TypeError, ValueError):
        return JsonResponse({
            "success": False,
            "message": "Invalid fare value."
        }, status=400)
    # ------------------------------------------------------------
    # Validate values
    # ------------------------------------------------------------
    if base_fare < 0:
        return JsonResponse({
            "success": False,
            "message": "Base fare cannot be negative."
        }, status=400)
    if per_km_fare < 0:
        return JsonResponse({
            "success": False,
            "message": "Per KM fare cannot be negative."
        }, status=400)
    if per_min_fare < 0:
        return JsonResponse({
            "success": False,
            "message": "Per minute fare cannot be negative."
        }, status=400)
    if surge_cap_multiplier < 1:
        return JsonResponse({
            "success": False,
            "message": "Surge cap must be at least 1."
        }, status=400)
    if night_surge_multiplier < 1:
        return JsonResponse({
            "success": False,
            "message": "Night surge must be at least 1."
        }, status=400)
    # ------------------------------------------------------------
    # Get ServiceZone
    # ------------------------------------------------------------
    try:
        zone = ServiceZone.objects.get(
            id=zone_id,
            is_active=True
        )
    except ServiceZone.DoesNotExist:
        return JsonResponse({
            "success": False,
            "message": "Selected zone does not exist or is inactive."
        }, status=404)
    # ------------------------------------------------------------
    # Get VehicleType
    # ------------------------------------------------------------
    try:
        vehicle_type = VehicleType.objects.get(
            id=vehicle_type_id,
        )
    except VehicleType.DoesNotExist:
        return JsonResponse({
            "success": False,
            "message": "Selected vehicle type does not exist or is inactive."
        }, status=404)
    # ------------------------------------------------------------
# Create / Update RateCard
# ------------------------------------------------------------
    try:
        with db_transaction.atomic():
            rate_card = (
            RateCard.objects
            .filter(
                zone=zone,
                vehicle_type=vehicle_type,
                is_active=True
            )
            .order_by("-id")
            .first()
        )
        if rate_card is None:
            rate_card = RateCard.objects.create(
                zone=zone,
                vehicle_type=vehicle_type,
                base_fare=base_fare,
                per_km_fare=per_km_fare,
                per_min_fare=per_min_fare,
                # Required model field
                min_fare=base_fare,
                surge_cap_multiplier=surge_cap_multiplier,
                night_surge_multiplier=night_surge_multiplier,
                is_active=True
            )
            action = "created"
        else:
            # A price change SUPERSEDES the current card rather than editing it.
            #
            # This used to assign the new rates onto the live row and save it --
            # which silently rewrote the schedule that historical trips had been
            # priced under, because a fare snapshot points at a card by version.
            # RateCard.save() now refuses that, and new_version() is the supported
            # path: the successor inherits everything not overridden, takes
            # version + 1, and the old card's effective window is closed so the
            # resolver never sees two live cards for one zone + vehicle type.
            rate_card = rate_card.new_version(
                base_fare=base_fare,
                per_km_fare=per_km_fare,
                per_min_fare=per_min_fare,
                min_fare=(rate_card.min_fare
                          if rate_card.min_fare is not None else base_fare),
                surge_cap_multiplier=surge_cap_multiplier,
                night_surge_multiplier=night_surge_multiplier,
            )
            action = "superseded (new version %s)" % rate_card.version
        return JsonResponse({
            "success": True,
            "message": (
                f"{vehicle_type.type} fare "
                f"{action} successfully for "
                f"{zone.name}."
            ),
            "configuration": {
                "id": rate_card.id,
                "vehicleTypeId": vehicle_type.id,
                "vehicleName": str(vehicle_type.type),
                "baseFare": str(rate_card.base_fare),
                "perKmFare": str(rate_card.per_km_fare),
                "perMinFare": str(rate_card.per_min_fare),
                "surgeCap": str(
                    rate_card.surge_cap_multiplier
                ),
                "nightSurge": str(
                    rate_card.night_surge_multiplier
                )
            }
        })
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.exception(
            "GLOBAL CONFIG UPDATE FAILED | zone=%s | vehicle=%s",
            zone_id,
            vehicle_type_id
        )
        return JsonResponse({
            "success": False,
            "message": str(e),
            "error": str(e),
        }, status=500)
@admin_required
def ride(request: HttpRequest) -> HttpResponse:
    """
    Ride Management page.

    Supports:
    - All rides
    - Requested
    - Accepted
    - In Progress
    - Completed
    - Cancelled
    - Search by ride/rider/driver/vehicle
    - Status filtering
    - Pagination
    """

    # ---------------------------------------------------------
    # SEARCH + STATUS FILTER
    # ---------------------------------------------------------

    search_query = request.GET.get("search", "").strip()
    selected_status = request.GET.get("status", "").strip().lower()

    valid_statuses = {
        "requested",
        "accepted",
        "in_progress",
        "completed",
        "cancelled",
    }

    # ---------------------------------------------------------
    # BASE QUERYSET
    # ---------------------------------------------------------

    trips = (
        Trip.objects
        .select_related(
            "user_id",
            "driver_id",
            "driver_id__user_id",
            "vehicle_id",
            "requested_vehicle_type",
            "status_id",
            "zone",
        )
        .order_by("-requested_at")
    )

    # ---------------------------------------------------------
    # SEARCH
    # ---------------------------------------------------------

    if search_query:
        search_filter = Q(id__icontains=search_query)

        # Rider
        search_filter |= Q(
            user_id__full_name__icontains=search_query
        )
        search_filter |= Q(
            user_id__phone_number__icontains=search_query
        )

        # Driver
        search_filter |= Q(
            driver_id__user_id__full_name__icontains=search_query
        )
        search_filter |= Q(
            driver_id__user_id__phone_number__icontains=search_query
        )

        # Vehicle
        search_filter |= Q(
            vehicle_id__vehicle_number__icontains=search_query
        )

        trips = trips.filter(search_filter).distinct()

    # ---------------------------------------------------------
    # STATUS FILTER
    # ---------------------------------------------------------

    if selected_status in valid_statuses:
        trips = trips.filter(
            status_id__status_code=selected_status
        )

    # ---------------------------------------------------------
    # PAGINATION
    # ---------------------------------------------------------

    paginator = Paginator(trips, 10)

    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # ---------------------------------------------------------
    # TOTAL RIDES
    # ---------------------------------------------------------

    total_rides = Trip.objects.count()

    # ---------------------------------------------------------
    # STATUS COUNTS
    # ---------------------------------------------------------

    requested_count = Trip.objects.filter(
        status_id__status_code="requested"
    ).count()

    accepted_count = Trip.objects.filter(
        status_id__status_code="accepted"
    ).count()

    reached_count = Trip.objects.filter(
        status_id__status_code="reached"
    ).count()

    in_progress_count = Trip.objects.filter(
        status_id__status_code="in_progress"
    ).count()

    completed_count = Trip.objects.filter(
        status_id__status_code="completed"
    ).count()

    cancelled_count = Trip.objects.filter(
        status_id__status_code="cancelled"
    ).count()

    # ---------------------------------------------------------
    # ACTIVE RIDES
    # ---------------------------------------------------------

    active_rides = (
        requested_count
        + accepted_count
        + reached_count
        + in_progress_count
    )

    # ---------------------------------------------------------
    # CONTEXT
    # ---------------------------------------------------------

    context = {
        "trips": page_obj.object_list,
        "page_obj": page_obj,

        "total_rides": total_rides,
        "active_rides": active_rides,
        "completed_rides": completed_count,
        "cancelled_rides": cancelled_count,

        "requested_count": requested_count,
        "accepted_count": accepted_count,
        "reached_count": reached_count,
        "in_progress_count": in_progress_count,
        "completed_count": completed_count,
        "cancelled_count": cancelled_count,

        "selected_status": selected_status,
        "search_query": search_query,
    }

    return render(
        request,
        "admin_pages/ride.html",
        context,
    )


@admin_required
def ride_detail(request: HttpRequest, trip_id: int) -> HttpResponse:
    """Render the complete operations view for one trip."""
    trip = get_object_or_404(
        Trip.objects.select_related(
            "user_id",
            "driver_id",
            "driver_id__user_id",
            "vehicle_id",
            "requested_vehicle_type",
            "status_id",
            "zone",
        ),
        id=trip_id,
    )

    fare = trip.fare_pricing.order_by("-id").first()
    promo = (
        PromoRedemption.objects
        .filter(trip=trip)
        .select_related("promo")
        .first()
    )
    receipts = Receipt.objects.filter(trip_id=trip).order_by("-version")
    latest_receipt = receipts.first()
    commission_rate = commission_percent_for_trip(trip)
    fare_total = fare.total_fare if fare else (trip.final_fare or trip.estimated_fare or Decimal("0"))
    commission_amount = (
        fare_total * Decimal(str(commission_rate or "0")) / Decimal("100")
    ).quantize(Decimal("0.01"))

    timeline = [
        ("Requested", trip.requested_at),
        ("Accepted", trip.accepted_at),
        ("Driver arrived", trip.reached_at),
        ("OTP verified", getattr(trip, "otp_verified_at", None)),
        ("Started", trip.started_at),
        ("Completed", trip.completed_at),
        ("Cancelled", trip.cancelled_at),
    ]

    context = {
        "trip": trip,
        "fare": fare,
        "promo": promo,
        "receipts": receipts,
        "latest_receipt": latest_receipt,
        "chat_messages": ChatMessage.objects.filter(trip=trip).select_related("sender"),
        "timeline": timeline,
        "commission_rate": commission_rate,
        "commission_amount": commission_amount,
        "driver_net": (fare_total - commission_amount).quantize(Decimal("0.01")),
        "gst_amount": latest_receipt.gst_amount if latest_receipt else None,
        "has_route_trail": False,
    }
    return render(request, "admin_pages/ride_detail.html", context)


@admin_required
def riders(request: HttpRequest) -> HttpResponse:
    """Rider operations page with account, ride, payment and safety history."""

    User = get_user_model()

    query = (request.GET.get("q") or "").strip()
    selected_id = request.GET.get("rider")

    action_message = None
    action_error = None

    # ---------------------------------------------------------
    # RIDER LIST
    # ---------------------------------------------------------

    rider_users = (
        User.objects
        .filter(role="rider")
        .select_related("rider")
    )

    # Search by name / phone / email
    if query:
        rider_users = rider_users.filter(
            Q(full_name__icontains=query)
            | Q(phone_number__icontains=query)
            | Q(email__icontains=query)
        )

    rider_users = rider_users.order_by("-created_at")

    # ---------------------------------------------------------
    # PAGINATION
    # ---------------------------------------------------------

    paginator = Paginator(rider_users, 10)

    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # ---------------------------------------------------------
    # DASHBOARD COUNTS
    # ---------------------------------------------------------

    total_riders = User.objects.filter(role="rider").count()

    flagged_riders = Rider.objects.filter(
        flagged_for_review=True
    ).count()

    active_riders = User.objects.filter(
        role="rider",
        trips__status_id__status_code__in=[
            "requested",
            "accepted",
            "reached",
            "in_progress",
        ],
    ).distinct().count()

    today = timezone.localdate()

    rides_today = Trip.objects.filter(
        user_id__role="rider",
        requested_at__date=today,
    ).count()

    # ---------------------------------------------------------
    # SELECTED RIDER
    # ---------------------------------------------------------

    selected_user = None

    if selected_id:
        selected_user = (
            User.objects
            .filter(
                id=selected_id,
                role="rider",
            )
            .select_related("rider")
            .first()
        )

    # ---------------------------------------------------------
    # ACTIONS
    # ---------------------------------------------------------

    if request.method == "POST" and selected_user:

        action = request.POST.get("action")

        # Example: block rider
        if action == "block_rider":

            try:
                rider = selected_user.rider

                # Change this field if your actual Rider model
                # uses another status field.
                rider.status = "blocked"
                rider.save(update_fields=["status"])

                action_message = (
                    f"Rider #{selected_user.id} was blocked successfully."
                )

            except Rider.DoesNotExist:
                action_error = "Rider profile could not be found."

        # Example: unblock rider
        elif action == "unblock_rider":

            try:
                rider = selected_user.rider

                rider.status = "active"
                rider.save(update_fields=["status"])

                action_message = (
                    f"Rider #{selected_user.id} was unblocked successfully."
                )

            except Rider.DoesNotExist:
                action_error = "Rider profile could not be found."

    # ---------------------------------------------------------
    # DEFAULT VALUES
    # ---------------------------------------------------------

    trips = []
    payments = []
    receipts = []
    wallet = None
    wallet_transactions = []
    sos_events = []
    notifications = []
    favorite_places = []
    support_tickets = []
    notification_preferences = None

    active_trip = None
    open_sos = 0

    rider_trip_count = 0
    completed_trip_count = 0
    cancelled_trip_count = 0

    latest_trip = None

    # ---------------------------------------------------------
    # SELECTED RIDER DETAILS
    # ---------------------------------------------------------

    if selected_user:

        trip_queryset = (
            Trip.objects
            .filter(user_id=selected_user)
            .select_related(
                "driver_id__user_id",
                "status_id",
                "vehicle_id",
            )
            .order_by("-requested_at")
        )

        # Active ride
        active_trip = trip_queryset.filter(
            status_id__status_code__in=[
                "requested",
                "accepted",
                "reached",
                "in_progress",
            ]
        ).first()

        rider_trip_count = trip_queryset.count()

        completed_trip_count = trip_queryset.filter(
            status_id__status_code="completed"
        ).count()

        cancelled_trip_count = trip_queryset.filter(
            status_id__status_code="cancelled"
        ).count()

        latest_trip = trip_queryset.first()

        trips = trip_queryset[:25]

        # Payments
        payments = (
            Payment.objects
            .filter(user_id=selected_user)
            .select_related("trip_id")
            .order_by("-created_at")[:15]
        )

        # Receipts
        receipts = (
            Receipt.objects
            .filter(user_id=selected_user)
            .select_related("trip_id")
            .order_by("-issued_at")[:15]
        )

        # Wallet
        wallet = (
            Wallet.objects
            .filter(
                user_id=selected_user,
                scope=Wallet.SCOPE_RIDER,
            )
            .first()
        )

        # Wallet transactions
        wallet_transactions = (
            WalletTransaction.objects
            .filter(user_id=selected_user)
            .order_by("-created_at")[:15]
        )

        # SOS
        sos_events = (
            SOSEvent.objects
            .filter(user=selected_user)
            .select_related("trip")
            .order_by("-created_at")[:15]
        )

        open_sos = SOSEvent.objects.filter(
            user=selected_user,
            status="open",
        ).count()

        # Notifications
        notifications = (
            Notification.objects
            .filter(user_id=selected_user)
            .order_by("-created_at")[:10]
        )

        # Favorite places
        favorite_places = (
            FavoritePlace.objects
            .filter(user_id=selected_user)
            .order_by("id")
        )

        # Support tickets
        support_tickets = (
            SupportTicket.objects
            .filter(user_id=selected_user)
            .select_related("trip_id")
            .order_by("-created_at")[:10]
        )

        # Notification preferences
        preferences = (
            NotificationPreference.objects
            .filter(user_id=selected_user)
            .first()
        )

        if preferences:
            notification_preferences = {
                field: getattr(preferences, field)
                for field in (
                    "transactional",
                    "ride_event",
                    "payment",
                    "payout",
                    "sos",
                    "kyc",
                    "system",
                    "marketing",
                    "promo",
                    "push_enabled",
                    "email_enabled",
                    "sms_enabled",
                )
            }

    # ---------------------------------------------------------
    # CONTEXT
    # ---------------------------------------------------------

    return render(
        request,
        "admin_pages/riders.html",
        {
            # Pagination
            "page_obj": page_obj,
            "paginator": paginator,

            # Rider list
            "riders": page_obj.object_list,
            "selected_rider": selected_user,

            # Rider details
            "trips": trips,
            "payments": payments,
            "receipts": receipts,
            "wallet": wallet,
            "wallet_transactions": wallet_transactions,
            "sos_events": sos_events,
            "notifications": notifications,
            "favorite_places": favorite_places,
            "support_tickets": support_tickets,
            "notification_preferences": notification_preferences,

            # Statistics
            "total_riders": total_riders,
            "flagged_riders": flagged_riders,
            "active_riders": active_riders,
            "rides_today": rides_today,

            # Search
            "search": query,

            # Ride information
            "active_trip": active_trip,
            "open_sos": open_sos,
            "rider_trip_count": rider_trip_count,
            "completed_trip_count": completed_trip_count,
            "cancelled_trip_count": cancelled_trip_count,
            "latest_trip": latest_trip,

            # Messages
            "action_message": action_message,
            "action_error": action_error,
        },
    )
@admin_required
@require_http_methods(["GET", "POST"])
def promo_codes(request: HttpRequest) -> HttpResponse:
    """Admin list and creation flow for rider promo campaigns."""
    query = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "all").lower()

    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        description = (request.POST.get("description") or "").strip()
        discount_type = (request.POST.get("discount_type") or "").strip()
        discount_value = request.POST.get("discount_value")
        max_discount_amount = request.POST.get("max_discount_amount")
        min_fare = request.POST.get("min_fare")
        zone_id = request.POST.get("zone_id")
        valid_from_raw = request.POST.get("valid_from")
        valid_to_raw = request.POST.get("valid_to")
        max_total_redemptions = request.POST.get("max_total_redemptions")
        max_per_user_redemptions = request.POST.get("max_per_user_redemptions")
        is_active = request.POST.get("is_active") in {"on", "true", "True", "1"}

        if not code:
            return HttpResponse("Promo code is required.", status=400)
        if discount_type not in {"percent", "flat"}:
            return HttpResponse("Discount type is invalid.", status=400)

        try:
            discount_value_dec = Decimal(str(discount_value or "0"))
            min_fare_dec = Decimal(str(min_fare or "0"))
            max_discount_amount_dec = (
                Decimal(str(max_discount_amount)) if max_discount_amount not in (None, "") else None
            )
        except InvalidOperation:
            return HttpResponse("Discount or fare values must be numeric.", status=400)

        valid_from = parse_datetime(valid_from_raw) if valid_from_raw else None
        valid_to = parse_datetime(valid_to_raw) if valid_to_raw else None
        if valid_from is None or valid_to is None:
            return HttpResponse("Valid from and valid to are required.", status=400)
        if valid_to <= valid_from:
            return HttpResponse("Valid to must be after valid from.", status=400)

        try:
            max_total_redemptions_int = (
                int(max_total_redemptions) if max_total_redemptions not in (None, "") else None
            )
            max_per_user_redemptions_int = (
                int(max_per_user_redemptions) if max_per_user_redemptions not in (None, "") else 1
            )
        except (TypeError, ValueError):
            return HttpResponse("Usage limit values must be integers.", status=400)

        zone = None
        if zone_id not in (None, "", "0"):
            zone = ServiceZone.objects.filter(id=zone_id).first()
            if zone is None:
                return HttpResponse("Selected zone could not be found.", status=400)

        if PromoCode.objects.filter(code__iexact=code).exists():
            return HttpResponse("A promo code with that code already exists.", status=400)

        PromoCode.objects.create(
            code=code,
            description=description,
            discount_type=discount_type,
            discount_value=discount_value_dec,
            max_discount_amount=max_discount_amount_dec,
            min_fare=min_fare_dec,
            zone=zone,
            valid_from=valid_from,
            valid_to=valid_to,
            max_total_redemptions=max_total_redemptions_int,
            max_per_user_redemptions=max_per_user_redemptions_int,
            is_active=is_active,
        )
        return redirect("promo_codes")

    promos = PromoCode.objects.annotate(
        used_count=Count("redemptions", distinct=True)
    ).select_related("zone").order_by("-valid_from")
    if query:
        promos = promos.filter(Q(code__icontains=query) | Q(description__icontains=query))
    now = timezone.now()
    if status_filter == "active":
        promos = promos.filter(is_active=True, valid_from__lte=now, valid_to__gt=now)
    elif status_filter == "expired":
        promos = promos.filter(valid_to__lte=now)
    elif status_filter == "inactive":
        promos = promos.filter(is_active=False)
    return render(request, "admin_pages/promo_codes.html", {
        "promos": promos[:100],
        "search": query,
        "status_filter": status_filter,
        "now": now,
        "total_promos": PromoCode.objects.count(),
        "active_promos": PromoCode.objects.filter(
            is_active=True, valid_from__lte=now, valid_to__gt=now
        ).count(),
        # Redemption is not wired into booking: `promos.redeem_promo` has no
        # callers, so this count is structurally 0 and always will be until that
        # changes. Reported as an explicit state rather than a number, because
        # "0 redemptions" beside "N active promos" reads as a campaign nobody
        # used -- when the truth is that redeeming is not implemented yet.
        "redemptions_enabled": False,
        "total_redemptions": PromoRedemption.objects.count(),
        "zones": ServiceZone.objects.filter(is_active=True).order_by("name"),
    })

@admin_required
@require_http_methods(["GET", "POST"])
def emergency_dashboard(request: HttpRequest) -> HttpResponse:
    """Emergency queue with audited admin status transitions."""
    status_filter = (request.GET.get("status") or "all").lower()
    valid_statuses = {choice[0] for choice in SOSEvent.STATUS_CHOICES}
    create_error = ""
    if request.method == "POST":
        if request.POST.get("action") == "create_sos":
            initiated_by = request.POST.get("initiated_by")
            event_type = request.POST.get("event_type") or "panic"
            user_label = (request.POST.get("user_label") or "Admin reported user").strip()[:255]
            note = (request.POST.get("note") or "").strip()[:10000]
            trip_id = (request.POST.get("trip_id") or "").strip()
            latitude = (request.POST.get("latitude") or "").strip() or None
            longitude = (request.POST.get("longitude") or "").strip() or None
            if initiated_by not in {"rider", "driver"}:
                create_error = "Select whether the SOS was raised for a rider or driver."
            elif event_type not in dict(SOSEvent.EVENT_TYPE_CHOICES):
                create_error = "Select a valid emergency type."
            else:
                trip = None
                try:
                    if trip_id:
                        trip = Trip.objects.get(id=trip_id)
                    SOSEvent.objects.create(
                        user=request.user,
                        user_label=user_label,
                        initiated_by=initiated_by,
                        trip=trip,
                        event_type=event_type,
                        latitude=latitude,
                        longitude=longitude,
                        note=note,
                        ip_address=(request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                                    or request.META.get("REMOTE_ADDR")),
                        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:512],
                    )
                    return redirect("emergency_dashboard")
                except (Trip.DoesNotExist, ValueError, TypeError):
                    create_error = "Enter a valid trip ID, latitude, or longitude."
        event_id = request.POST.get("event_id")
        target = request.POST.get("status")
        if not create_error and target in {"acknowledged", "resolved", "false_alarm"}:
            event = SOSEvent.objects.filter(id=event_id).first()
            if event and event.status != target:
                event.status = target
                event.save(update_fields=["status"])
                SOSEventUpdate.objects.create(
                    event=event,
                    actor=request.user,
                    actor_label=str(request.user),
                    new_status=target,
                    note=(request.POST.get("note") or "")[:10000],
                )
        status_filter = request.POST.get("filter_status") or "all"
    events = SOSEvent.objects.select_related(
        "user", "trip", "trip__driver_id", "trip__driver_id__user_id"
    ).order_by("-created_at")
    if status_filter in valid_statuses:
        events = events.filter(status=status_filter)
    events = list(events[:100])
    for event in events:
        if event.user_id is None:
            event.user = request.user
    return render(request, "admin_pages/emergency.html", {
        "events": events,
        "status_filter": status_filter,
        "open_count": SOSEvent.objects.filter(status="open").count(),
        "acknowledged_count": SOSEvent.objects.filter(status="acknowledged").count(),
        "resolved_count": SOSEvent.objects.filter(status="resolved").count(),
        "total_count": SOSEvent.objects.count(),
        "create_error": create_error,
        "event_type_choices": SOSEvent.EVENT_TYPE_CHOICES,
    })
@admin_required
def transaction_dashboard(request):

    q = request.GET.get("q", "").strip().lower()
    selected_type = request.GET.get("type", "").strip().lower()
    selected_status = request.GET.get("status", "").strip().lower()

    def money(value):
        try:
            return Decimal(str(value or 0)).quantize(
                Decimal("0.01")
            )
        except Exception:
            return Decimal("0.00")

    def user_name(user, fallback):
        if not user:
            return fallback

        return (
            getattr(user, "full_name", None)
            or getattr(user, "phone_number", None)
            or getattr(user, "username", None)
            or fallback
        )

    def matches_search(row):
        if not q:
            return True

        searchable = " ".join(
            [
                str(row.get("transaction_id", "")),
                str(row.get("trip_id", "")),
                str(row.get("rider", "")),
                str(row.get("driver", "")),
                str(row.get("type", "")),
                str(row.get("payment_method", "")),
                str(row.get("gateway_reference", "")),
            ]
        ).lower()

        return q in searchable

    transactions = []

    # =========================================================
    # 1. AUTHORITATIVE RIDER PAYMENTS
    # =========================================================

    completed_payments = (
        Payment.objects
        .filter(status="completed")
        .select_related("user_id")
        .order_by("-created_at")
    )

    rider_total = money(
        completed_payments.aggregate(
            total=Sum("amount")
        )["total"]
    )

    for payment in completed_payments.iterator(chunk_size=500):
        rider = payment.user_id

        row = {
            "transaction_id": f"TXN-{payment.id}",
            "type": "rider_payment",
            "trip_id": getattr(payment.trip_id, "id", None),
            "rider": user_name(rider, "Unknown Rider"),
            "driver": getattr(payment, "driver_name", None) or "Not Assigned",
            "amount": money(payment.amount),
            "status": "success",
            "payment_method": payment.method or "—",
            "created_at": payment.created_at,
            "gateway_reference": (
                payment.gateway_payment_id
                or payment.cashfree_payment_id
                or payment.gateway_order_id
                or payment.cashfree_order_id
                or ""
            ),
        }

        if matches_search(row):
            transactions.append(row)

    # =========================================================
    # 2. REFUNDED PAYMENTS
    # =========================================================

    refunded_payments = (
        Payment.objects
        .filter(status="refunded")
        .select_related("user_id")
        .order_by("-updated_at")
    )

    refund_total = money(
        refunded_payments.aggregate(
            total=Sum("amount")
        )["total"]
    )

    for payment in refunded_payments.iterator(chunk_size=500):
        rider = payment.user_id

        row = {
            "transaction_id": f"REFUND-{payment.id}",
            "type": "refund",
            "trip_id": getattr(payment.trip_id, "id", None),
            "rider": user_name(rider, "Unknown Rider"),
            "driver": getattr(payment, "driver_name", None) or "Not Assigned",
            "amount": money(payment.amount),
            "status": "refunded",
            "payment_method": payment.method or "—",
            "created_at": payment.updated_at or payment.created_at,
            "gateway_reference": (
                payment.gateway_payment_id
                or payment.cashfree_payment_id
                or payment.gateway_order_id
                or payment.cashfree_order_id
                or ""
            ),
        }

        if matches_search(row):
            transactions.append(row)

    # =========================================================
    # 3. DRIVER EARNINGS FROM FINANCIAL LEDGER
    # =========================================================

    earning_history = (
        TransactionHistory.objects
        .filter(
            txn_type="credit",
            status__in=["success", "completed", "processed", "successful"],
        )
        .select_related(
            "user_id",
            "driver_id",
            "driver_id__user_id",
        )
        .order_by("-created_at")
    )

    driver_earning_total = money(
        earning_history.aggregate(
            total=Sum("amount")
        )["total"]
    )

    for txn in earning_history.iterator(chunk_size=500):
        driver = txn.driver_id
        driver_user = getattr(driver, "user_id", None)

        row = {
            "transaction_id": (
                f"EARNING-{txn.id}"
            ),
            "type": "driver_earnings",
            "trip_id": getattr(txn.trip_id, "id", None),
            "rider": user_name(
                txn.user_id,
                "Unknown Rider",
            ),
            "driver": user_name(
                driver_user,
                f"Driver #{driver.pk}" if driver else "Unknown Driver",
            ),
            "amount": money(txn.amount),
            "status": "success",
            "payment_method": txn.method or "—",
            "created_at": txn.created_at,
            "gateway_reference": (
                txn.cashfree_transfer_id
                or txn.gateway_transaction_id
                or txn.gateway_payment_id
                or ""
            ),
        }

        if matches_search(row):
            transactions.append(row)

    # =========================================================
    # 4. ACTUAL DRIVER PAYOUTS
    # =========================================================

    completed_withdrawals = (
        WithdrawalRequest.objects
        .filter(status="completed")
        .select_related(
            "driver",
            "driver__user_id",
        )
        .order_by("-processed_at", "-requested_at")
    )

    driver_payout_total = money(
        completed_withdrawals.aggregate(
            total=Sum("amount")
        )["total"]
    )

    for withdrawal in completed_withdrawals.iterator(chunk_size=500):
        driver = withdrawal.driver
        driver_user = getattr(driver, "user_id", None)

        row = {
            "transaction_id": f"PAYOUT-{withdrawal.id}",
            "type": "driver_payout",
            "trip_id": None,
            "rider": "—",
            "driver": user_name(
                driver_user,
                f"Driver #{driver.pk}" if driver else "Unknown Driver",
            ),
            "amount": money(withdrawal.amount),
            "status": "success",
            "payment_method": withdrawal.payout_method or "—",
            "created_at": (
                withdrawal.processed_at
                or withdrawal.requested_at
            ),
            "gateway_reference": (
                withdrawal.payout_reference_id
                or ""
            ),
        }

        if matches_search(row):
            transactions.append(row)

    # =========================================================
    # 5. CANCELLATION FEES -- NOT IMPLEMENTED, deliberately absent
    #
    # There is no cancellation charging in this platform. `Trip.cancellation_fee`
    # is a column no code writes, and the query that used to live here filtered
    # TransactionHistory on `method__icontains="cancel"` -- while every method
    # ever written is wallet / online / cash / deferred. It was empty by
    # construction, so it contributed a permanent zero to a revenue dashboard,
    # which reads as "we charge no cancellation fees": a business statement
    # nobody has made.
    #
    # Removed rather than labelled, because a greyed-out zero still occupies a
    # revenue row. The database column is intentionally kept -- it is where the
    # value goes once a cancellation policy exists.
    # =========================================================

    # =========================================================
    # 6. PLATFORM FEES
    #
    # Platform fees are not stored as a separate authoritative
    # financial ledger entry in the current models.
    # Do not manufacture a fee from FarePricing or estimated fare.
    # =========================================================

    platform_total = Decimal("0.00")

    # =========================================================
    # FILTER BY TRANSACTION TYPE
    # =========================================================

    if selected_type:
        transactions = [
            row
            for row in transactions
            if row["type"] == selected_type
        ]

    # =========================================================
    # FILTER BY STATUS
    # =========================================================

    if selected_status:
        transactions = [
            row
            for row in transactions
            if row["status"] == selected_status
        ]

    # =========================================================
    # SORT
    # =========================================================

    transactions.sort(
        key=lambda row: row.get("created_at") or 0,
        reverse=True,
    )

    # =========================================================
    # PAGINATION
    # =========================================================

    paginator = Paginator(transactions, 10)

    page_obj = paginator.get_page(
        request.GET.get("page", 1)
    )

    # =========================================================
    # TOTALS
    # =========================================================

    total_transactions = len(transactions)

    refund_total = money(refund_total)

    # =========================================================
    # CONTEXT
    # =========================================================

    context = {
        "transactions": page_obj.object_list,
        "page_obj": page_obj,

        "total_transactions": total_transactions,

        "rider_payments": rider_total,

        # Driver earnings are actual ledger credits.
        "driver_payments": driver_earning_total,
        "driver_earnings": driver_earning_total,

        # Actual completed withdrawal payouts.
        "driver_payouts": driver_payout_total,

        # No authoritative platform-fee ledger currently exists.
        "platform_fees": platform_total,

        "refunds": refund_total,

        "search": request.GET.get("q", ""),

        "transaction_type": selected_type,

        "transaction_status": selected_status,

        "transaction_types": [
            ("rider_payment", "Rider Payment"),
            ("driver_earnings", "Driver Earnings"),
            ("driver_payout", "Driver Payout"),
            ("platform_fee", "Platform Fee"),
            ("refund", "Refund"),
            ("wallet_credit", "Wallet Credit"),
            ("wallet_debit", "Wallet Debit"),
            ("payment_reversal", "Payment Reversal"),
        ],
    }

    return render(
        request,
        "admin_pages/transaction_dashboard.html",
        context,
    )
@admin_required
def predictive_heatmaps(request: HttpRequest) -> HttpResponse:
    """
    Predictive Heatmaps / Demand Intelligence dashboard.
    Builds live dashboard data from drivers and trips.
    """

    now = timezone.now()

    # ---------------------------------------------------------
    # 1. ACTIVE DRIVERS
    # ---------------------------------------------------------
    active_drivers = Driver.objects.filter(
        status="online"
    ).count()

    # ---------------------------------------------------------
    # 2. PENDING / UNMET RIDES
    # ---------------------------------------------------------
    pending_rides = Trip.objects.filter(
        status_id__status_code="requested"
    ).count()

    # ---------------------------------------------------------
    # 3. NETWORK STATUS
    # ---------------------------------------------------------
    if active_drivers == 0:
        network_status = "OFFLINE"
    elif pending_rides > active_drivers * 2:
        network_status = "CRITICAL"
    elif pending_rides > active_drivers:
        network_status = "HIGH DEMAND"
    else:
        network_status = "OPTIMAL"

    # ---------------------------------------------------------
    # 4. SUPPLY / DEMAND RATIO
    # ---------------------------------------------------------
    total_demand = active_drivers + pending_rides

    if total_demand > 0:
        supply_ratio = round(
            (active_drivers / total_demand) * 100
        )
    else:
        supply_ratio = 100

    supply_ratio = min(max(supply_ratio, 0), 100)
    deficit_pct = 100 - supply_ratio

    # ---------------------------------------------------------
    # 5. PREDICTED DEMAND ZONES
    # ---------------------------------------------------------
    demand_zones = []

    # Build zones from currently requested rides.
    zones = list(
        Trip.objects
        .filter(status_id__status_code__in=["requested"])
        .filter(zone__isnull=False)
        .values("zone__name")
        .annotate(request_count=Count("id"))
        .order_by("-request_count")[:10]
    )
    max_zone_requests = max(
        (zone["request_count"] for zone in zones),
        default=0,
    )

    for index, zone in enumerate(zones):
        request_count = zone["request_count"]

        # Simple prediction logic.
        if active_drivers == 0:
            predicted_surge = 3.0
        else:
            demand_supply = request_count / max(active_drivers, 1)

            if demand_supply >= 3:
                predicted_surge = 3.0
            elif demand_supply >= 2:
                predicted_surge = 2.5
            elif demand_supply >= 1:
                predicted_surge = 2.0
            else:
                predicted_surge = 1.2

        demand_zones.append({
            "id": index + 1,
            "zone_name": zone["zone__name"],
            "request_count": request_count,
            "demand_width": round(
                (request_count / max_zone_requests) * 100
            ) if max_zone_requests else 0,
            "reason": f"{request_count} active ride requests",
            "predicted_surge": predicted_surge,
            "predicted_at_time": (
                now + timedelta(minutes=45)
            ).strftime("%H:%M"),
        })

    # ---------------------------------------------------------
    # 6. DISPATCH LOGS
    # ---------------------------------------------------------
    # If you already have a dispatch log model, query it here.
    dispatch_logs = []

    # ---------------------------------------------------------
    # 7. RENDER DASHBOARD
    # ---------------------------------------------------------
    context = {
        "network_status": network_status,
        "active_drivers": active_drivers,
        "pending_rides": pending_rides,
        "supply_ratio": supply_ratio,
        "deficit_pct": deficit_pct,
        "demand_zones": demand_zones,
        "dispatch_logs": dispatch_logs,
    }

    return render(
        request,
        "admin_pages/predictive_heatmaps.html",
        context,
    )
def admin_logout(request: HttpRequest) -> HttpResponse:
    auth_logout(request)
    return redirect("login")
@admin_required
def global_search(request: HttpRequest) -> HttpResponse:
    """
    Global admin search.
    Smart search:
    - Numeric query -> exact driver ID / trip ID / phone number
    - Text query -> driver/user names
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    query = (request.GET.get("q") or "").strip()
    drivers = []
    riders = []
    trips = []
    if query:
        # =====================================================
        # NUMERIC SEARCH
        # =====================================================
        if query.isdigit():
            # -------------------------------------------------
            # DRIVER
            # Exact Driver ID
            # OR exact phone number
            # -------------------------------------------------
            drivers = (
                Driver.objects
                .select_related(
                    "user_id",
                    "active_vehicle",
                )
                .filter(
                    Q(id=int(query))
                    |
                    Q(user_id__phone_number=query)
                )
                .order_by("id")[:10]
            )
            # -------------------------------------------------
            # RIDER / USER
            # Exact phone number
            # -------------------------------------------------
            riders = (
                User.objects
                .filter(
                    Q(phone_number=query)
                    |
                    Q(id=int(query))
                )
                .order_by("id")[:10]
            )
            # -------------------------------------------------
            # TRIP
            # Exact Trip ID
            # -------------------------------------------------
            trips = (
                Trip.objects
                .select_related(
                    "user_id",
                    "driver_id",
                    "driver_id__user_id",
                    "status_id",
                    "vehicle_id",
                )
                .filter(
                    Q(id=int(query))
                )
                .order_by("-requested_at")[:10]
            )
        # =====================================================
        # TEXT SEARCH
        # =====================================================
        else:
            # -------------------------------------------------
            # DRIVER NAME
            # -------------------------------------------------
            drivers = (
                Driver.objects
                .select_related(
                    "user_id",
                    "active_vehicle",
                )
                .filter(
                    Q(user_id__full_name__icontains=query)
                )
                .order_by("id")[:10]
            )
            # -------------------------------------------------
            # RIDER / USER NAME
            # -------------------------------------------------
            riders = (
                User.objects
                .filter(
                    Q(full_name__icontains=query)
                )
                .order_by("id")[:10]
            )
            # -------------------------------------------------
            # TRIPS
            # -------------------------------------------------
            trips = (
                Trip.objects
                .select_related(
                    "user_id",
                    "driver_id",
                    "driver_id__user_id",
                    "status_id",
                    "vehicle_id",
                )
                .filter(
                    Q(user_id__full_name__icontains=query)
                    |
                    Q(driver_id__user_id__full_name__icontains=query)
                )
                .order_by("-requested_at")[:10]
            )
    context = {
        "query": query,
        "drivers": drivers,
        "riders": riders,
        "trips": trips,
        "driver_count": len(drivers),
        "rider_count": len(riders),
        "trip_count": len(trips),
    }
    return render(
        request,
        "admin_pages/global_search.html",
        context,
    )

@admin_required
def global_search_api(request: HttpRequest) -> JsonResponse:
    """Return compact results for the admin header search dropdown."""
    User = get_user_model()
    query = (request.GET.get("q") or "").strip()
    results = []
    if not query:
        return JsonResponse({"status": "ok", "results": []})

    users = User.objects.filter(role="rider").filter(
        Q(full_name__icontains=query)
        | Q(phone_number__icontains=query)
        | Q(email__icontains=query)
    ).order_by("id")[:10]
    for user in users:
        results.append({
            "type": "rider",
            "title": user.full_name or user.phone_number,
            "subtitle": f"Rider · {user.phone_number}",
            "url": f"/riders/?rider={user.id}",
        })

    drivers = Driver.objects.select_related("user_id").filter(
        Q(user_id__full_name__icontains=query)
        | Q(user_id__phone_number__icontains=query)
    ).order_by("id")[:10]
    for driver in drivers:
        results.append({
            "type": "driver",
            "title": driver.user_id.full_name or driver.user_id.phone_number,
            "subtitle": f"Driver · ID {driver.id}",
            "url": f"/driver/{driver.id}/",
        })

    return JsonResponse({"status": "ok", "results": results[:20]})

@admin_required
@require_http_methods(["GET", "POST"])
def driver_profile(request, driver_id):
    """
    Admin Driver Profile.

    Shows:
    - Driver information
    - Vehicle information
    - Trip performance
    - Revenue and distance
    - Cancellation discipline
    - Dispatch/acceptance timing
    - Fatigue and shift duty
    - Bank account
    - UPI
    - Recent driver cancellations
    - Trip history
    - Verification status

    POST:
    - block
    - unblock
    """

    # =========================================================
    # DRIVER
    # =========================================================
    driver = get_object_or_404(
        Driver.objects.select_related(
            "user_id",
            "active_vehicle",
            "active_vehicle__vehicle_type_id",
        ),
        id=driver_id,
    )

    # =========================================================
    # BLOCK / UNBLOCK DRIVER
    # =========================================================
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "block":
            driver.status = "blocked"
            driver.save(update_fields=["status"])

            return redirect("driver_profile", driver_id=driver.id)

        elif action == "unblock":
            driver.status = "active"
            driver.save(update_fields=["status"])

            return redirect("driver_profile", driver_id=driver.id)

    # =========================================================
    # DRIVER / USER DETAILS
    # =========================================================
    user = getattr(driver, "user_id", None)

    # ---------------------------------------------------------
    # DRIVER NAME
    # ---------------------------------------------------------
    driver_name = ""

    if user:
        try:
            full_name_from_user = user.get_full_name()
        except Exception:
            full_name_from_user = ""

        driver_name = (
            full_name_from_user
            or getattr(user, "username", "")
            or getattr(user, "first_name", "")
            or ""
        ).strip()

    # Fallback to Driver model fields
    if not driver_name:
        driver_name = (
            getattr(driver, "name", "")
            or getattr(driver, "full_name", "")
            or getattr(driver, "driver_name", "")
            or ""
        ).strip()

    # Final fallback
    if not driver_name:
        driver_name = "Unknown Driver"


    # ---------------------------------------------------------
    # PHONE
    # ---------------------------------------------------------
    phone_number = (
        getattr(user, "phone_number", None)
        or getattr(user, "phone", None)
        or getattr(driver, "phone_number", None)
        or getattr(driver, "phone", None)
        or "—"
    )

    # ---------------------------------------------------------
    # EMAIL
    # ---------------------------------------------------------
    email = (
        getattr(user, "email", None)
        or getattr(driver, "email", None)
        or "—"
    )

    # =========================================================
    # TRIPS
    # =========================================================
    trips_qs = (
        Trip.objects
        .filter(driver_id=driver)
        .select_related(
            "user_id",
            "status_id",
            "vehicle_id",
            "vehicle_id__vehicle_type_id",
        )
        .order_by("-requested_at")
    )

    total_trips = trips_qs.count()

    completed_trips = trips_qs.filter(
        status_id__status_code="completed"
    ).count()

    cancelled_trips = trips_qs.filter(
        status_id__status_code="cancelled"
    ).count()

    active_trips = trips_qs.filter(
        status_id__status_code__in=[
            "requested",
            "accepted",
            "reached",
            "in_progress",
        ]
    ).count()

    # =========================================================
    # DRIVER RATING
    # =========================================================
    rating = getattr(driver, "ratings", None)

    if rating is None:
        rating = 0

    # =========================================================
    # REVENUE
    # =========================================================
    revenue_result = (
        trips_qs
        .filter(status_id__status_code="completed")
        .aggregate(
            total=Sum("final_fare")
        )
    )

    total_revenue = revenue_result.get("total") or 0

    # =========================================================
    # DISTANCE
    # =========================================================
    distance_result = (
        trips_qs
        .aggregate(
            total=Sum("actual_distance_km")
        )
    )

    total_distance = distance_result.get("total") or 0

    # =========================================================
    # VEHICLE INFORMATION
    # =========================================================
    vehicle = None

    try:
        vehicle = driver.active_vehicle
    except Exception:
        vehicle = None

    # Driver #3 currently has active Vehicle #2,
    # but active_vehicle can be None.
    # Therefore use direct Vehicle lookup as fallback.
    if vehicle is None:
        vehicle = (
            Vehicle.objects
            .filter(
                driver_id=driver,
                status="active",
            )
            .select_related("vehicle_type_id")
            .order_by("-id")
            .first()
        )

    # =========================================================
    # VEHICLE DETAILS
    # =========================================================
    vehicle_type = None
    vehicle_number = None
    vehicle_brand = None
    vehicle_model = None
    vehicle_color = None
    vehicle_rc_doc = None
    vehicle_pic = None

    if vehicle:

        vehicle_type_obj = getattr(
            vehicle,
            "vehicle_type_id",
            None,
        )

        if vehicle_type_obj:
            vehicle_type = (
                getattr(
                    vehicle_type_obj,
                    "name",
                    None,
                )
                or getattr(
                    vehicle_type_obj,
                    "type_name",
                    None,
                )
                or str(vehicle_type_obj)
            )

        vehicle_number = getattr(
            vehicle,
            "vehicle_number",
            None,
        )

        vehicle_brand = getattr(
            vehicle,
            "brand",
            None,
        )

        vehicle_model = getattr(
            vehicle,
            "model",
            None,
        )

        vehicle_color = getattr(
            vehicle,
            "color",
            None,
        )

        vehicle_rc_doc = getattr(
            vehicle,
            "rc_doc",
            None,
        )

        vehicle_pic = getattr(
            vehicle,
            "vehicle_pic",
            None,
        )

    # =========================================================
    # DRIVER CANCELLATIONS
    # =========================================================
    cancellation_qs = (
        DriverCancellation.objects
        .filter(driver=driver)
        .select_related("trip")
        .order_by("-created_at")
    )

    driver_cancellation_count = cancellation_qs.count()

    # =========================================================
    # CANCELLATION RATE
    # =========================================================
    if total_trips > 0:
        cancellation_rate = (
            driver_cancellation_count / total_trips
        ) * 100
    else:
        cancellation_rate = 0

    # =========================================================
    # CANCELLATIONS IN LAST 24 HOURS
    # =========================================================
    now = timezone.now()

    last_24_hours = now - timedelta(hours=24)

    cancellations_24h = (
        cancellation_qs
        .filter(
            created_at__gte=last_24_hours
        )
        .count()
    )

    # =========================================================
    # RECENT CANCELLATIONS
    # =========================================================
    recent_cancellations = cancellation_qs[:10]

    # =========================================================
    # DISPATCH / ACCEPTANCE TIMING
    # =========================================================
    accepted_trips = trips_qs.filter(
        accepted_at__isnull=False,
        requested_at__isnull=False,
    )

    accepted_count = accepted_trips.count()

    if total_trips > 0:
        acceptance_rate = (
            accepted_count / total_trips
        ) * 100
    else:
        acceptance_rate = 0

    acceptance_times = []

    for trip in accepted_trips:

        if trip.requested_at and trip.accepted_at:

            response_seconds = (
                trip.accepted_at
                - trip.requested_at
            ).total_seconds()

            if response_seconds >= 0:
                acceptance_times.append(
                    response_seconds
                )

    if acceptance_times:

        average_response_seconds = (
            sum(acceptance_times)
            / len(acceptance_times)
        )

        average_response_minutes = (
            average_response_seconds / 60
        )

        acceptance_timing_available = True

    else:

        average_response_seconds = None
        average_response_minutes = None
        acceptance_timing_available = False

    # =========================================================
    # DRIVER SESSION / FATIGUE
    # =========================================================
    current_session = (
        DriverSession.objects
        .filter(
            driver=driver,
            ended_at__isnull=True,
        )
        .order_by("-started_at")
        .first()
    )

    current_shift_hours = 0

    if current_session and current_session.started_at:

        current_shift_seconds = (
            now
            - current_session.started_at
        ).total_seconds()

        if current_shift_seconds < 0:
            current_shift_seconds = 0

        current_shift_hours = (
            current_shift_seconds / 3600
        )

    # =========================================================
    # ROLLING 24 HOUR DUTY
    # =========================================================
    duty_window_start = (
        now - timedelta(hours=24)
    )

    sessions_24h = (
        DriverSession.objects
        .filter(
            driver=driver,
            started_at__lt=now,
        )
        .filter(
            models.Q(
                ended_at__isnull=True
            )
            |
            models.Q(
                ended_at__gte=duty_window_start
            )
        )
        .order_by("started_at")
    )

    rolling_duty_seconds = 0

    for session in sessions_24h:

        if not session.started_at:
            continue

        session_start = session.started_at

        if session_start < duty_window_start:
            session_start = duty_window_start

        session_end = (
            session.ended_at
            or now
        )

        if session_end > now:
            session_end = now

        if session_end > session_start:

            rolling_duty_seconds += (
                session_end
                - session_start
            ).total_seconds()

    rolling_24h_duty_hours = (
        rolling_duty_seconds / 3600
    )

    # =========================================================
    # FATIGUE STATUS
    # =========================================================
    if rolling_24h_duty_hours >= 12:

        fatigue_status = "Lockout Required"

    elif rolling_24h_duty_hours >= 10:

        fatigue_status = "High"

    elif rolling_24h_duty_hours >= 8:

        fatigue_status = "Warning"

    else:

        fatigue_status = "Normal"

    # =========================================================
    # FATIGUE LOCKOUT
    # =========================================================
    fatigue_lockout_until = getattr(
        driver,
        "fatigue_lockout_until",
        None,
    )

    if fatigue_lockout_until:

        if fatigue_lockout_until > now:

            fatigue_status = "Locked"

        else:

            fatigue_lockout_until = None

    # =========================================================
    # BANK ACCOUNT
    # =========================================================
    bank_account = None

    try:

        bank_account = driver.bank_account

    except Exception:

        bank_account = (
            DriverBankAccount.objects
            .filter(driver=driver)
            .first()
        )

    bank_configured = False
    bank_name = None
    bank_account_number = None
    bank_ifsc = None
    masked_bank_account = None

    if bank_account:

        bank_name = getattr(
            bank_account,
            "bank_name",
            None,
        )

        bank_account_number = getattr(
            bank_account,
            "account_number",
            None,
        )

        bank_ifsc = getattr(
            bank_account,
            "ifsc_code",
            None,
        )

        if bank_account_number:

            bank_configured = True

            account_string = str(
                bank_account_number
            )

            if len(account_string) > 4:

                masked_bank_account = (
                    "*"
                    * (len(account_string) - 4)
                    + account_string[-4:]
                )

            else:

                masked_bank_account = account_string

    # =========================================================
    # UPI
    # =========================================================
    upi_contact = None

    try:

        upi_contact = driver.upi_contact

    except Exception:

        upi_contact = (
            DriverUPIContact.objects
            .filter(
                driver=driver,
                is_active=True,
            )
            .first()
        )

    upi_configured = False
    upi_id = None

    if upi_contact and getattr(
        upi_contact,
        "is_active",
        True,
    ):

        upi_id = getattr(
            upi_contact,
            "upi_id",
            None,
        )

        if upi_id:
            upi_configured = True

    # =========================================================
    # DRIVER STATUS
    # =========================================================
    driver_status = getattr(
        driver,
        "status",
        None,
    )

    driver_doc_status = getattr(
        driver,
        "doc_status",
        None,
    )

    # =========================================================
    # VERIFICATION
    # =========================================================
    approval_status = driver_status

    # =========================================================
    # TRIP HISTORY
    # =========================================================
    recent_trips = trips_qs[:10]

    # =========================================================
    # TEMPLATE ALIASES
    # =========================================================
    is_blocked = (
        driver_status == "blocked"
    )

    approved = (
        driver_doc_status == "approved"
        or driver_status == "approved"
    )

    document_status = driver_doc_status

    # Fatigue aliases used by template
    fatigue_locked = bool(
        fatigue_lockout_until
        and fatigue_lockout_until > now
    )

    # =========================================================
    # CONTEXT
    # =========================================================
    context = {

        # -----------------------------------------------------
        # DRIVER
        # -----------------------------------------------------
        "driver": driver,
        "user": user,

        # IMPORTANT:
        # All three point to the SAME actual driver name.
        "driver_name": driver_name,
        "full_name": driver_name,
        "name": driver_name,

        "phone_number": phone_number,
        "email": email,

        # -----------------------------------------------------
        # DRIVER STATUS
        # -----------------------------------------------------
        "driver_status": driver_status,

        "driver_doc_status": driver_doc_status,

        "doc_status": driver_doc_status,

        "document_status": document_status,

        "approval_status": approval_status,

        "approved": approved,

        "is_blocked": is_blocked,

        # -----------------------------------------------------
        # VEHICLE
        # -----------------------------------------------------
        "vehicle": vehicle,

        "vehicle_type": vehicle_type,

        "vehicle_number": vehicle_number,

        "vehicle_brand": vehicle_brand,

        "vehicle_model": vehicle_model,

        "vehicle_color": vehicle_color,

        "vehicle_rc_doc": vehicle_rc_doc,

        "vehicle_pic": vehicle_pic,

        # -----------------------------------------------------
        # PERFORMANCE
        # -----------------------------------------------------
        "total_trips": total_trips,

        "completed_trips": completed_trips,

        "cancelled_trips": cancelled_trips,

        "active_trips": active_trips,

        "rating": rating,

        # -----------------------------------------------------
        # REVENUE / DISTANCE
        # -----------------------------------------------------
        "total_revenue": total_revenue,

        "total_distance": total_distance,

        # -----------------------------------------------------
        # CANCELLATION
        # -----------------------------------------------------
        "driver_cancellation_count":
            driver_cancellation_count,

        "total_driver_cancellations":
            driver_cancellation_count,

        "cancellation_rate":
            cancellation_rate,

        "cancellations_24h":
            cancellations_24h,

        "cancellation_count_24h":
            cancellations_24h,

        "recent_cancellations":
            recent_cancellations,

        # -----------------------------------------------------
        # DISPATCH / ACCEPTANCE
        # -----------------------------------------------------
        "accepted_count":
            accepted_count,

        "acceptance_rate":
            acceptance_rate,

        "average_response_seconds":
            average_response_seconds,

        "average_response_minutes":
            average_response_minutes,

        "average_dispatch_response_minutes":
            average_response_minutes,

        "acceptance_timing_available":
            acceptance_timing_available,

        "dispatch_response_available":
            acceptance_timing_available,

        # -----------------------------------------------------
        # SESSION / FATIGUE
        # -----------------------------------------------------
        "current_session":
            current_session,

        "current_shift_hours":
            current_shift_hours,

        "rolling_24h_duty_hours":
            rolling_24h_duty_hours,

        "duty_hours_24h":
            rolling_24h_duty_hours,

        "fatigue_status":
            fatigue_status,

        "fatigue_lockout_until":
            fatigue_lockout_until,

        "fatigue_locked":
            fatigue_locked,

        # -----------------------------------------------------
        # BANK
        # -----------------------------------------------------
        "bank_account":
            bank_account,

        "bank_configured":
            bank_configured,

        "bank_name":
            bank_name,

        "bank_account_number":
            bank_account_number,

        "masked_bank_account":
            masked_bank_account,

        "bank_ifsc":
            bank_ifsc,

        # -----------------------------------------------------
        # UPI
        # -----------------------------------------------------
        "upi_contact":
            upi_contact,

        "upi_configured":
            upi_configured,

        "upi_id":
            upi_id,

        # -----------------------------------------------------
        # TRIPS
        # -----------------------------------------------------
        "recent_trips":
            recent_trips,

        "trips":
            recent_trips,
    }

    return render(
        request,
        "admin_pages/driver_profile.html",
        context,
    )
from servers.auth_user.services import send_push_notification
@admin_required
def notifications(request: HttpRequest) -> HttpResponse:
    """
    Admin notification page.
    Handles user search and notification sending.
    """

    User = get_user_model()

    # Dynamic rider/driver search
    if request.method == "GET" and request.GET.get("search"):

        query = request.GET.get("search", "").strip()

        if len(query) < 2:
            return JsonResponse({"users": []})

        users = (
            User.objects
            .filter(
                role__in=["rider", "driver"],
                is_active=True,
            )
            .filter(
                Q(full_name__icontains=query)
                | Q(phone_number__icontains=query)
            )
            .only(
                "id",
                "full_name",
                "phone_number",
                "role",
            )
            .order_by("full_name")[:20]
        )

        return JsonResponse({
            "users": [
                {
                    "id": user.id,
                    "name": user.full_name or "Unnamed User",
                    "phone": user.phone_number,
                    "role": user.role,
                }
                for user in users
            ]
        })

    # Send notification
    if request.method == "POST":

        audience = (request.POST.get("audience") or "all").strip()
        notif_type = (request.POST.get("notif_type") or "system").strip()
        title = (request.POST.get("title") or "").strip()
        message = (request.POST.get("message") or "").strip()

        if not title:
            messages.error(request, "Notification title is required.")
            return redirect("notifications")

        if not message:
            messages.error(request, "Notification message is required.")
            return redirect("notifications")

        if notif_type not in {"system", "marketing"}:
            messages.error(request, "Invalid notification type.")
            return redirect("notifications")

        recipients = User.objects.filter(
            role__in=["rider", "driver"],
            is_active=True,
        )

        if audience == "riders":
            recipients = recipients.filter(role="rider")

        elif audience == "drivers":
            recipients = recipients.filter(role="driver")

        elif audience == "specific":

            raw_ids = request.POST.get("user_ids", "")

            user_ids = [
                value.strip()
                for value in raw_ids.split(",")
                if value.strip().isdigit()
            ]

            if not user_ids:
                messages.error(
                    request,
                    "Please select at least one rider or driver."
                )
                return redirect("notifications")

            recipients = recipients.filter(id__in=user_ids)

        elif audience != "all":
            messages.error(request, "Invalid audience.")
            return redirect("notifications")

        sent_count = 0
        push_count = 0
        skipped_count = 0

        for user in recipients.iterator():

            preferences = getattr(
                user,
                "notification_preferences",
                None,
            )

            if preferences is not None:
                if not preferences.is_enabled_for(notif_type):
                    skipped_count += 1
                    continue

            Notification.objects.create(
                user_id=user,
                title=title,
                message=message,
                notif_type=notif_type,
                data={
                    "source": "admin_broadcast",
                    "notification_type": notif_type,
                },
            )

            sent_count += 1

            if (
                preferences is None
                or preferences.push_enabled
            ):
                if getattr(user, "fcm_token", None):
                    if send_push_notification(
                        user,
                        title,
                        message,
                        {
                            "source": "admin_broadcast",
                            "notification_type": notif_type,
                        },
                    ):
                        push_count += 1

        messages.success(
            request,
            f"Notification created for {sent_count} users. "
            f"Push notifications queued: {push_count}. "
            f"Skipped: {skipped_count}."
        )

        return redirect("notifications")

    return render(
        request,
        "admin_pages/notifications.html",
    )


@admin_required
@require_http_methods(["GET", "POST"])
def stale_rides(request: HttpRequest) -> HttpResponse:
    """Operator queue for active rides that have stopped looking alive.

    Exists because QA trip 42 stranded a driver's supply and there was no way for
    anyone without a shell to see it, let alone act on it.

    WHAT AN OPERATOR CAN DO HERE, AND WHAT THEY DELIBERATELY CANNOT
    --------------------------------------------------------------
    Available:

      mark_reviewed   Records that a human looked and judged the ride legitimate.
                      Clears the flag. Changes nothing about the trip.
      release_driver  Clears the ephemeral driver active-trip marker for a driver
                      whose trip PostgreSQL already considers finished. A cache
                      repair, not a business decision -- reconcile consults the
                      database and only clears when the two disagree, so it cannot
                      free a driver who is genuinely mid-ride.

    NOT available, on purpose:

      admin_complete  Completing a ride creates settlement, a wallet movement, a
                      commission and a receipt. Whether an abandoned ride may be
                      completed, and at what fare, is a business policy decision
                      nobody has made. A button for it would invent that policy.
      admin_cancel    Terminating a started ride has the same problem from the
                      other side: what the rider owes, if anything, is undecided.

    So this page is DETECTION plus the two safe actions. Termination stays blocked
    until the money policy is decided, and the page says so rather than hiding it.
    """
    from servers.admin_audit.services import record_admin_action
    from servers.ride.liveness import (
        attention_after_seconds, classify, reconcile_driver_active_trip,
        stale_after_seconds, stale_candidates,
    )
    from servers.ride.models import Trip

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        trip_id = (request.POST.get("trip_id") or "").strip()
        reason = (request.POST.get("reason") or "").strip()

        if not reason:
            messages.error(request, "A reason is required for any action here.")
            return redirect("stale_rides")

        trip = Trip.objects.filter(id=trip_id).select_related(
            "status_id", "driver_id").first()
        if trip is None:
            messages.error(request, "Trip not found.")
            return redirect("stale_rides")

        if action == "mark_reviewed":
            before = {
                "stale_flagged_at": str(trip.stale_flagged_at),
                "stale_reason": trip.stale_reason,
            }
            Trip.objects.filter(id=trip.id).update(
                stale_flagged_at=None, stale_reason="")
            record_admin_action(
                request, action="stale_ride_marked_reviewed",
                target_type="Trip", target_id=trip.id,
                before=before, after={"stale_flagged_at": None},
                reason=reason,
            )
            messages.success(
                request,
                f"Ride #{trip.id} marked reviewed. Trip status unchanged.")

        elif action == "release_driver":
            if trip.driver_id is None:
                messages.error(request, "That ride has no assigned driver.")
                return redirect("stale_rides")
            outcome = reconcile_driver_active_trip(trip.driver_id_id)
            record_admin_action(
                request, action="driver_availability_reconciled",
                target_type="Driver", target_id=trip.driver_id_id,
                before={"trip_id": trip.id,
                        "trip_status": trip.status_id.status_code},
                after={"outcome": outcome},
                reason=reason,
            )
            if outcome == "repaired_cleared":
                messages.success(
                    request,
                    f"Driver #{trip.driver_id_id} released: the stale active-trip "
                    "marker was cleared and they can be dispatched again.")
            elif outcome == "ok":
                messages.info(
                    request,
                    f"Nothing to repair for driver #{trip.driver_id_id}. The "
                    "database and the live state already agree, which means this "
                    "ride is still genuinely active.")
            else:
                messages.warning(
                    request,
                    f"Reconciliation for driver #{trip.driver_id_id} reported "
                    f"'{outcome}'.")
        else:
            messages.error(request, "Unsupported action.")
        return redirect("stale_rides")

    # ---------------------------------------------------------------- the queue
    now = timezone.now()
    rows = []
    for trip in stale_candidates(now=now, limit=200):
        classification, silent, reason = classify(trip, now=now)
        driver = trip.driver_id
        anchor = trip.started_at or trip.accepted_at or trip.requested_at
        rows.append({
            "trip_id": trip.id,
            "status": trip.status_id.status_code,
            "classification": classification,
            "silent_minutes": round(silent / 60) if silent else None,
            "reason": trip.stale_reason or reason,
            "started_at": trip.started_at or trip.accepted_at,
            "elapsed_minutes": round((now - anchor).total_seconds() / 60),
            "last_driver_activity_at": trip.last_driver_activity_at,
            "last_rider_activity_at": trip.last_rider_activity_at,
            "driver_id": driver.id if driver else None,
            # A stable, non-identifying label. An operator who needs to phone the
            # rider opens the trip; a queue does not need a phone number in it.
            "rider_ref": f"R{trip.user_id_id}",
            "flagged_at": trip.stale_flagged_at,
        })

    context = {
        "rows": rows,
        "stale_after_minutes": round(stale_after_seconds() / 60),
        "attention_after_minutes": round(attention_after_seconds() / 60),
        "termination_blocked": True,
    }
    return render(request, "admin_pages/stale_rides.html", context)
