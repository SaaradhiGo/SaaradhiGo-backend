from datetime import datetime, time, timedelta
from decimal import Decimal
from functools import wraps
from django.utils import timezone
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
# from servers.driver.admin_utils import list_drivers_admin
from servers.support.models import SupportTicket
from django.core.paginator import Paginator
from django.db import models
from django.db.models.functions import Coalesce
from django.db.models import (Avg,Count, Q,Sum,F,Max,Value,DecimalField,)
from servers.driver.models import Driver, WithdrawalRequest
from servers.ride.models import FarePricing, Trip
from servers.pricing.services import commission_percent_for_trip
from servers.pricing.models import ServiceZone, RateCard
from decimal import Decimal, InvalidOperation
from django.http import JsonResponse
from django.views.decorators.http import require_POST

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
        user = request.user
        is_admin = (
            user.is_authenticated
            and (getattr(user, "role", None) == "admin" or getattr(user, "is_superuser", False))
        )
        if not is_admin:
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
    if request.user.is_authenticated and (
        getattr(request.user, "role", None) == "admin"
        or getattr(request.user, "is_superuser", False)
    ):
        # Already logged in as admin -> go straight to the dashboard.
        return redirect("fleet_monitor")

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

            user = authenticate(request, username=phone_number, password=password)
            if user is None:
                error = "Invalid phone number or password."
            elif not (getattr(user, "role", None) == "admin" or getattr(user, "is_superuser", False)):
                error = "You do not have admin privileges."
            else:
                auth_login(request, user)
                return redirect("fleet_monitor")

    return render(request, 'admin_pages/login.html', {"error": error})


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


@admin_required
def driver_onboarding(request: HttpRequest) -> HttpResponse:
    page_number=request.GET.get("page",1)
    status=request.GET.get('status',"ALL")
    selected_driver_id=request.GET.get('selected_driver',None)
    selected_driver=None
    
    if selected_driver_id:
        selected_driver=Driver.objects.get(id=selected_driver_id)
    approved=Driver.objects.filter(doc_status='approved')
    pending=Driver.objects.filter(doc_status='pending')
    if status=="APPROVED":
        drivers=approved
    elif status=="REJECTED":
        drivers=Driver.objects.filter(doc_status='rejected')
    elif status=="PENDING":
        drivers=pending
    else:
        drivers=Driver.objects.all()
    if request.method=="POST":
        action=request.POST.get('action',None)
        if action=="approve":
            selected_driver.doc_status='approved'
            selected_driver.approved=True
            selected_driver.save()
        elif action=="reject":
            selected_driver.doc_status='rejected'
            selected_driver.save()
        selected_driver.doc_status_updated_at=timezone.now()
    return render(request, "admin_pages/driver_onboarding.html",{"pending_reviews_count":pending.count(),"approved_count":approved.count(),"drivers":drivers,"selected_driver":selected_driver,"page_number":page_number,"status_filter":status})

@admin_required
def dispute_support(request: HttpRequest) -> HttpResponse:
    tickets_qs = SupportTicket.objects.all().order_by('-created_at')
    open_count = tickets_qs.filter(status='OPEN').count()
    urgent_issue_types = {'safety', 'account', 'app_bug'}
    urgent_count = tickets_qs.filter(status='OPEN', issue_type__in=urgent_issue_types).count()
    total_count = tickets_qs.count()

    ticket_list = []
    for ticket in tickets_qs:
        ticket.priority = 'URGENT' if ticket.issue_type in urgent_issue_types else 'NORMAL'
        ticket.rider_name = (
            getattr(ticket.user_id, 'full_name', None)
            or getattr(ticket.user_id, 'phone_number', None)
            or 'Unknown rider'
        )
        ticket.driver = None
        # Provide human-friendly labels for templates
        try:
            ticket.status_display = ticket.get_status_display()
        except Exception:
            ticket.status_display = ticket.status
        try:
            ticket.issue_type_display = ticket.get_issue_type_display()
        except Exception:
            ticket.issue_type_display = ticket.issue_type
        ticket_list.append(ticket)

    # Pagination
    paginator = Paginator(ticket_list, 5)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Selected ticket details
    selected_ticket_id = request.GET.get('selected_ticket')
    selected_ticket = None
    if selected_ticket_id:
        selected_ticket = next(
            (ticket for ticket in ticket_list if str(ticket.id) == str(selected_ticket_id)),
            None,
        )
    if not selected_ticket and page_obj.object_list:
        selected_ticket = page_obj.object_list[0]

    context = {
        'page_obj': page_obj,
        'tickets': page_obj.object_list,
        'selected_ticket': selected_ticket,
        'open_count': open_count,
        'urgent_count': urgent_count,
        'total_count': total_count,
    }

    return render(request, "admin_pages/dispute_support.html", context)

@admin_required
def payment_dashboard(request: HttpRequest) -> HttpResponse:
    total_gross_revenue = FarePricing.objects.aggregate(total_revenue=models.Sum('total_fare'))['total_revenue'] or 0
    completed_count = WithdrawalRequest.objects.filter(status='completed').count()
    cancelled_count = WithdrawalRequest.objects.filter(status='failed').count()
    recent_transactions = WithdrawalRequest.objects.select_related('driver','driver__user_id','driver__active_vehicle','driver__active_vehicle__vehicle_type_id').order_by('-requested_at')
    paginator = Paginator(recent_transactions, 20)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    print(page_obj)
    return render(request, "admin_pages/payment_dashboard.html",{"total_payments": total_gross_revenue, "completed_count": completed_count, "cancelled_count": cancelled_count, "page_obj": page_obj})
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
    # 1. CURRENT FARE CONFIGURATION
    # ---------------------------------------------------------
    config = (
        RateCard.objects
        .filter(
            is_active=True,
            effective_from__lte=now
        )
        .filter(
            Q(effective_to__isnull=True) |
            Q(effective_to__gt=now)
        )
        .order_by(
            '-effective_from',
            '-version'
        )
        .first()
    )

    # ---------------------------------------------------------
    # 2. RECENT TRIPS - LAST ONE HOUR
    # ---------------------------------------------------------
    recent_trips = Trip.objects.filter(
        requested_at__gte=one_hour_ago
    )

    # ---------------------------------------------------------
    # 3. ACTIVE SURGE ZONES
    #
    # A zone is considered active when:
    #   - zone is active
    #   - trip happened/requested in last hour
    #   - trip has surge > 1
    #
    # Drivers are counted from drivers assigned to those trips.
    # ---------------------------------------------------------
    surge_zones = (
        ServiceZone.objects
        .filter(
            is_active=True,
            trips__requested_at__gte=one_hour_ago,
            trips__surge_multiplier__gt=1,
        )
        .annotate(

            # Number of surge requests during last hour
            requests_per_hour=Count(
                'trips',
                distinct=True,
            ),

            # Number of UNIQUE drivers assigned to those trips
            active_drivers=Count(
                'trips__driver_id',
                filter=Q(
                    trips__driver_id__isnull=False,
                ),
                distinct=True,
            ),

            # Highest surge multiplier in this zone
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
    # 4. NUMBER OF ACTIVE SURGE ZONES
    # ---------------------------------------------------------
    active_zones_count = surge_zones.count()

    # ---------------------------------------------------------
    # 5. PEAK SURGE ZONE
    # ---------------------------------------------------------
    peak_zone = surge_zones.first()

    # ---------------------------------------------------------
    # 6. AVERAGE SURGE MULTIPLIER
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
    # 7. CONTEXT FOR TEMPLATE
    # ---------------------------------------------------------
    context = {
        'config': config,

        'surge_zones': surge_zones,

        'peak_zone': peak_zone,

        'avg_multiplier': round(
            float(avg_multiplier),
            2
        ),

        'active_zones_count': active_zones_count,

        'recent_trips': recent_trips,
    }

    # ---------------------------------------------------------
    # 8. RENDER PAGE
    # ---------------------------------------------------------
    return render(
        request,
        "admin_pages/fare_surge.html",
        context
    )

@admin_required
@require_POST
def update_global_config(request: HttpRequest) -> JsonResponse:

    now = timezone.now()

    # Get currently active RateCard
    config = (
        RateCard.objects
        .filter(
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

    if not config:
        return JsonResponse(
            {
                'success': False,
                'message': 'No active fare configuration found.'
            },
            status=404,
        )

    # ---------------------------------------------------------
    # DEBUG: See exactly what the browser sent
    # ---------------------------------------------------------

    print("GLOBAL CONFIG POST DATA:")
    print(request.POST.dict())

    try:

        # Get values from POST
        base_fare_value = request.POST.get('base_fare')
        per_km_fare_value = request.POST.get('per_km_fare')
        per_min_fare_value = request.POST.get('per_min_fare')
        surge_cap_value = request.POST.get('surge_cap_multiplier')
        night_surge_value = request.POST.get('night_surge_multiplier')

        print("base_fare =", base_fare_value)
        print("per_km_fare =", per_km_fare_value)
        print("per_min_fare =", per_min_fare_value)
        print("surge_cap_multiplier =", surge_cap_value)
        print("night_surge_multiplier =", night_surge_value)

        # Check for missing values
        if not base_fare_value:
            raise ValueError("Base Fare is empty.")

        if not per_km_fare_value:
            raise ValueError("Per KM Fare is empty.")

        if not per_min_fare_value:
            raise ValueError("Per Minute Fare is empty.")

        if not surge_cap_value:
            raise ValueError("Surge Cap is empty.")

        if not night_surge_value:
            raise ValueError("Night Surge is empty.")

        # Convert to Decimal
        base_fare = Decimal(base_fare_value)
        per_km_fare = Decimal(per_km_fare_value)
        per_min_fare = Decimal(per_min_fare_value)
        surge_cap = Decimal(surge_cap_value)
        night_surge = Decimal(night_surge_value)

        # ---------------------------------------------------------
        # VALIDATION
        # ---------------------------------------------------------

        if base_fare < 0:
            raise ValueError("Base Fare cannot be negative.")

        if per_km_fare < 0:
            raise ValueError("Per KM Fare cannot be negative.")

        if per_min_fare < 0:
            raise ValueError("Per Minute Fare cannot be negative.")

        if surge_cap < 1:
            raise ValueError("Surge Cap must be at least 1.00.")

        if night_surge < 1:
            raise ValueError("Night Surge must be at least 1.00.")

    except (TypeError, ValueError, InvalidOperation) as e:

        print("FARE CONFIG VALIDATION ERROR:", str(e))

        return JsonResponse(
            {
                'success': False,
                'message': str(e),
            },
            status=400,
        )

    # ---------------------------------------------------------
    # UPDATE DATABASE
    # ---------------------------------------------------------

    config.base_fare = base_fare
    config.per_km_fare = per_km_fare
    config.per_min_fare = per_min_fare
    config.surge_cap_multiplier = surge_cap
    config.night_surge_multiplier = night_surge

    config.save()

    print("FARE CONFIG UPDATED SUCCESSFULLY")
    print("RateCard ID:", config.id)

    return JsonResponse(
        {
            'success': True,
            'message': 'Global fare configuration updated successfully.',
            'config': {
                'base_fare': str(config.base_fare),
                'per_km_fare': str(config.per_km_fare),
                'per_min_fare': str(config.per_min_fare),
                'surge_cap_multiplier': str(
                    config.surge_cap_multiplier
                ),
                'night_surge_multiplier': str(
                    config.night_surge_multiplier
                ),
            }
        }
    )

@admin_required
def predictive_heatmaps(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_pages/predictive_heatmaps.html")


def admin_logout(request: HttpRequest) -> HttpResponse:
    auth_logout(request)
    return redirect("login")
