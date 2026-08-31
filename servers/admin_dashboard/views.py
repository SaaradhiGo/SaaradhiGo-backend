from datetime import datetime, time, timedelta
from functools import wraps
from django.utils import timezone
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render,get_object_or_404
# from servers.driver.admin_utils import list_drivers_admin
from servers.support.models import SupportTicket
from django.core.paginator import Paginator
from django.db import models, transaction as db_transaction
from django.db.models.functions import Coalesce
from django.db.models import (Avg,Count, Q,Sum,F,Max,Value,DecimalField,)
from servers.driver.models import Driver, WithdrawalRequest,VehicleType,Vehicle
from servers.ride.models import FarePricing, Trip
from servers.pricing.services import commission_percent_for_trip
from servers.pricing.models import ServiceZone, RateCard
from decimal import Decimal, InvalidOperation
from django.http import JsonResponse
from django.views.decorators.http import require_POST,require_http_methods
from django.contrib.admin.views.decorators import staff_member_required
from servers.payments.models import Payment
import json
import logging
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
            rate_card.base_fare = base_fare
            rate_card.per_km_fare = per_km_fare
            rate_card.per_min_fare = per_min_fare
            # Keep existing minimum fare when updating
            if rate_card.min_fare is None:
                rate_card.min_fare = base_fare
            rate_card.surge_cap_multiplier = surge_cap_multiplier
            rate_card.night_surge_multiplier = night_surge_multiplier
            rate_card.save()
            action = "updated"
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
def ride(request):
    """
    Ride Management page.
    Uses the existing Trip model only.
    No database/model changes are required.
    Supports:
    - All rides
    - Requested
    - Accepted
    - In Progress
    - Completed
    - Cancelled
    - Ride statistics
    - Rider/Driver/Vehicle relationships
    """
    # ---------------------------------------------------------
    # STATUS FILTER
    # ---------------------------------------------------------
    selected_status = request.GET.get('status', '').strip().lower()
    valid_statuses = {
        'requested',
        'accepted',
        'in_progress',
        'completed',
        'cancelled',
    }
    # ---------------------------------------------------------
    # BASE QUERYSET
    # ---------------------------------------------------------
    trips = (
        Trip.objects
        .select_related(
            'user_id',
            'driver_id',
            'vehicle_id',
            'requested_vehicle_type',
            'status_id',
            'zone',
        )
        .order_by('-requested_at')
    )
    # ---------------------------------------------------------
    # APPLY STATUS FILTER
    # ---------------------------------------------------------
    if selected_status in valid_statuses:
        trips = trips.filter(
            status_id__status_code=selected_status
        )
    # ---------------------------------------------------------
    # TOTAL RIDES
    # ---------------------------------------------------------
    total_rides = Trip.objects.count()
    # ---------------------------------------------------------
    # STATUS COUNTS
    # ---------------------------------------------------------
    requested_count = Trip.objects.filter(
        status_id__status_code='requested'
    ).count()
    accepted_count = Trip.objects.filter(
        status_id__status_code='accepted'
    ).count()
    reached_count = Trip.objects.filter(
        status_id__status_code='reached'
    ).count()
    in_progress_count = Trip.objects.filter(
        status_id__status_code='in_progress'
    ).count()
    completed_count = Trip.objects.filter(
        status_id__status_code='completed'
    ).count()
    cancelled_count = Trip.objects.filter(
        status_id__status_code='cancelled'
    ).count()
    # ---------------------------------------------------------
    # ACTIVE RIDES
    #
    # A ride is considered active when it is:
    # requested, accepted, reached or in progress.
    # ---------------------------------------------------------
    active_rides = (
        requested_count
        + accepted_count
        + reached_count
        + in_progress_count
    )
    # ---------------------------------------------------------
    # TEMPLATE CONTEXT
    # ---------------------------------------------------------
    context = {
        # Ride records
        'trips': trips,
        # Main statistics
        'total_rides': total_rides,
        'active_rides': active_rides,
        'completed_rides': completed_count,
        'cancelled_rides': cancelled_count,
        # Individual status counts
        'requested_count': requested_count,
        'accepted_count': accepted_count,
        'reached_count': reached_count,
        'in_progress_count': in_progress_count,
        'completed_count': completed_count,
        'cancelled_count': cancelled_count,
        # Current filter
        'selected_status': selected_status,
    }
    return render(
        request,
        'admin_pages/ride.html',
        context
    )
@admin_required
def transaction_dashboard(request):
    from decimal import Decimal
    from django.core.paginator import Paginator
    from django.shortcuts import render
    from servers.ride.models import Trip
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
    trips = list(
        Trip.objects
        .select_related(
            "user_id",
            "driver_id",
            "driver_id__user_id",
            "status_id",
        )
        .order_by("-requested_at")
    )
    rider_rows = []
    earning_rows = []
    payout_rows = []
    fee_rows = []
    refund_rows = []
    cancellation_rows = []
    for trip in trips:
        rider = trip.user_id
        driver = trip.driver_id
        driver_user = getattr(driver, "user_id", None) if driver else None
        rider_name = user_name(rider, "Unknown Rider")
        driver_name = user_name(
            driver_user,
            f"Driver #{driver.pk}" if driver else "Not Assigned"
        )
        fare = money(
            trip.final_fare
            if trip.final_fare is not None
            else trip.estimated_fare
        )
        if fare <= 0:
            continue
        payment_method = (
            getattr(trip, "payment_method", None) or "—"
        )
        created_at = (
            getattr(trip, "completed_at", None)
            or getattr(trip, "cancelled_at", None)
            or getattr(trip, "requested_at", None)
        )
        # =====================================================
        # CANCELLATION HAS PRIORITY
        # =====================================================
        is_cancelled = bool(
            getattr(trip, "cancelled_at", None)
        )
        if not is_cancelled and getattr(trip, "status_id", None):
            status_code = str(
                getattr(
                    trip.status_id,
                    "status_code",
                    ""
                )
                or ""
            ).lower()
            is_cancelled = "cancel" in status_code
        if is_cancelled:
            cancellation_fee = money(
                getattr(
                    trip,
                    "cancellation_fee",
                    None
                )
            )
            cancellation_rows.append({
                "transaction_id": f"CANCEL-{trip.id}",
                "type": "cancellation_fee",
                "trip_id": trip.id,
                "rider": rider_name,
                "driver": driver_name,
                "amount": cancellation_fee,
                "status": "cancelled",
                "payment_method": payment_method,
                "created_at": created_at,
            })
            # Paid cancelled ride -> refund fare
            payment_status = str(
                getattr(
                    trip,
                    "payment_status",
                    ""
                )
                or ""
            ).lower()
            if payment_status in {
                "completed",
                "success",
                "successful",
                "paid",
                "refunded",
            }:
                refund_rows.append({
                    "transaction_id": f"REFUND-{trip.id}",
                    "type": "refund",
                    "trip_id": trip.id,
                    "rider": rider_name,
                    "driver": driver_name,
                    "amount": fare,
                    "status": "refunded",
                    "payment_method": payment_method,
                    "created_at": created_at,
                })
            continue
        # =====================================================
        # NORMAL COMPLETED RIDE
        # =====================================================
        status_code = ""
        if getattr(trip, "status_id", None):
            status_code = str(
                getattr(
                    trip.status_id,
                    "status_code",
                    ""
                )
                or ""
            ).lower()
        if "complete" not in status_code:
            continue
        # Rider Payment
        rider_rows.append({
            "transaction_id": f"TXN-{trip.id}",
            "type": "rider_payment",
            "trip_id": trip.id,
            "rider": rider_name,
            "driver": driver_name,
            "amount": fare,
            "status": "success",
            "payment_method": payment_method,
            "created_at": created_at,
        })
        # Commission
        try:
            from servers.pricing.services import (
                commission_percent_for_trip
            )
            commission_percent = Decimal(
                str(
                    commission_percent_for_trip(trip)
                )
            )
        except Exception:
            commission_percent = Decimal("18.00")
        platform_fee = money(
            fare
            * commission_percent
            / Decimal("100")
        )
        driver_earning = money(
            fare - platform_fee
        )
        # Driver Earnings
        earning_rows.append({
            "transaction_id": f"EARNING-{trip.id}",
            "type": "driver_earnings",
            "trip_id": trip.id,
            "rider": rider_name,
            "driver": driver_name,
            "amount": driver_earning,
            "status": "success",
            "payment_method": payment_method,
            "created_at": created_at,
        })
        # Driver Payout (mock = earnings)
        payout_rows.append({
            "transaction_id": f"PAYOUT-{trip.id}",
            "type": "driver_payout",
            "trip_id": trip.id,
            "rider": rider_name,
            "driver": driver_name,
            "amount": driver_earning,
            "status": "success",
            "payment_method": payment_method,
            "created_at": created_at,
        })
        # Platform Fee
        fee_rows.append({
            "transaction_id": f"FEE-{trip.id}",
            "type": "platform_fee",
            "trip_id": trip.id,
            "rider": rider_name,
            "driver": driver_name,
            "amount": platform_fee,
            "status": "success",
            "payment_method": payment_method,
            "created_at": created_at,
        })
    # =========================================================
    # FILTERED TRANSACTIONS
    # =========================================================
    if selected_type == "rider_payment":
        transactions = rider_rows
    elif selected_type == "driver_earnings":
        transactions = earning_rows
    elif selected_type == "driver_payout":
        transactions = payout_rows
    elif selected_type == "platform_fee":
        transactions = fee_rows
    elif selected_type == "refund":
        transactions = refund_rows
    elif selected_type == "cancellation_fee":
        transactions = cancellation_rows
    else:
        # All Types = completed rider payments + actual
        # cancellation/refund events
        transactions = (
            rider_rows
            + refund_rows
            + cancellation_rows
        )
    # =========================================================
    # SEARCH
    # =========================================================
    if q:
        transactions = [
            t for t in transactions
            if (
                q in str(t["transaction_id"]).lower()
                or q in str(t["trip_id"]).lower()
                or q in str(t["rider"]).lower()
                or q in str(t["driver"]).lower()
                or q in str(t["type"]).lower()
                or q in str(t["payment_method"]).lower()
            )
        ]
    # =========================================================
    # STATUS
    # =========================================================
    if selected_status:
        transactions = [
            t for t in transactions
            if t["status"] == selected_status
        ]
    transactions.sort(
        key=lambda x: x["created_at"] or 0,
        reverse=True
    )
    # =========================================================
    # TOTALS
    # =========================================================
    rider_total = money(sum(
        (t["amount"] for t in rider_rows),
        Decimal("0.00")
    ))
    platform_total = money(sum(
        (t["amount"] for t in fee_rows),
        Decimal("0.00")
    ))
    driver_earning_total = money(sum(
        (t["amount"] for t in earning_rows),
        Decimal("0.00")
    ))
    driver_payout_total = money(sum(
        (t["amount"] for t in payout_rows),
        Decimal("0.00")
    ))
    refund_total = money(sum(
        (t["amount"] for t in refund_rows),
        Decimal("0.00")
    ))
    cancellation_total = money(sum(
        (t["amount"] for t in cancellation_rows),
        Decimal("0.00")
    ))
    # =========================================================
    # PAGINATION
    # =========================================================
    paginator = Paginator(transactions, 10)
    page_obj = paginator.get_page(
        request.GET.get("page", 1)
    )
    # =========================================================
    # CONTEXT
    # =========================================================
    context = {
        "transactions": transactions,
        "page_obj": page_obj,
        # All financial events shown in All Types
        "total_transactions": len(transactions)
            if selected_type or q or selected_status
            else len(rider_rows) + len(refund_rows)
            + len(cancellation_rows),
        "rider_payments": rider_total,
        # IMPORTANT: don't add earnings + payout
        "driver_payments": driver_earning_total,
        "driver_earnings": driver_earning_total,
        "driver_payouts": driver_payout_total,
        "platform_fees": platform_total,
        "refunds": refund_total,
        "cancellation_fees": cancellation_total,
        "search": request.GET.get("q", ""),
        "transaction_type": selected_type,
        "transaction_status": selected_status,
        "transaction_types": [
            ("rider_payment", "Rider Payment"),
            ("driver_earnings", "Driver Earnings"),
            ("driver_payout", "Driver Payout"),
            ("platform_fee", "Platform Fee"),
            ("refund", "Refund"),
            ("cancellation_fee", "Cancellation Fee"),
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
    return render(request, "admin_pages/predictive_heatmaps.html")
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
@require_http_methods(["GET", "POST"])
def driver_profile(request: HttpRequest, driver_id: int) -> HttpResponse:
    # =========================================================
    # GET DRIVER
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
        is_json_request = (
            request.content_type
            and "application/json" in request.content_type
        )
        # -----------------------------------------------------
        # Get action
        # -----------------------------------------------------
        try:
            if is_json_request:
                data = json.loads(
                    request.body.decode("utf-8") or "{}"
                )
                action = data.get("action")
            else:
                action = request.POST.get("action")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse(
                {
                    "status": "error",
                    "error": {
                        "code": "INVALID_JSON",
                        "message": "Invalid request data.",
                        "details": {
                            "field": "general",
                            "issue": "Invalid JSON body",
                        },
                    },
                },
                status=400,
            )
        # -----------------------------------------------------
        # Validate action
        # -----------------------------------------------------
        if action not in ["block", "unblock"]:
            if is_json_request:
                return JsonResponse(
                    {
                        "status": "error",
                        "error": {
                            "code": "INVALID_ACTION",
                            "message": "Invalid driver action.",
                            "details": {
                                "field": "action",
                                "issue": "Action must be block or unblock",
                            },
                        },
                    },
                    status=400,
                )
            return redirect(
                "driver_profile",
                driver_id=driver_id,
            )
        # -----------------------------------------------------
        # Update driver status
        # -----------------------------------------------------
        try:
            if action == "block":
                driver.status = "blocked"
                driver.save(
                    update_fields=["status"]
                )
                message = "Driver blocked successfully."
            else:
                driver.status = "active"
                driver.save(
                    update_fields=["status"]
                )
                message = "Driver unblocked successfully."
            # -------------------------------------------------
            # JSON response
            # -------------------------------------------------
            if is_json_request:
                return JsonResponse(
                    {
                        "status": "success",
                        "message": message,
                        "data": {
                            "driver_id": driver.id,
                            "action": action,
                            "status": driver.status,
                        },
                    }
                )
            # -------------------------------------------------
            # Normal form request
            # -------------------------------------------------
            return redirect(
                "driver_profile",
                driver_id=driver_id,
            )
        except Exception as exc:
            logger.exception(
                "Failed to %s driver %s",
                action,
                driver.id,
            )
            if is_json_request:
                return JsonResponse(
                    {
                        "status": "error",
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "Unable to update driver status.",
                            "details": {
                                "field": "general",
                                "issue": str(exc),
                            },
                        },
                    },
                    status=500,
                )
            return redirect(
                "driver_profile",
                driver_id=driver_id,
            )
    # =========================================================
    # DRIVER STATUS
    # =========================================================
    driver_status = str(
        getattr(driver, "status", "") or ""
    ).lower()
    is_blocked = driver_status == "blocked"
    # =========================================================
    # DRIVER USER INFORMATION
    # =========================================================
    user = getattr(
        driver,
        "user_id",
        None,
    )
    driver_name = ""
    phone_number = ""
    email = ""
    if user:
        driver_name = (
            getattr(user, "full_name", "")
            or ""
        )
        phone_number = (
            getattr(user, "phone_number", "")
            or ""
        )
        email = (
            getattr(user, "email", "")
            or ""
        )
    # =========================================================
    # DRIVER TRIPS
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
    # ---------------------------------------------------------
    # All trips
    # ---------------------------------------------------------
    total_trips = trips_qs.count()
    # ---------------------------------------------------------
    # Completed trips
    # ---------------------------------------------------------
    completed_trips = trips_qs.filter(
        status_id__status_code="completed"
    ).count()
    # ---------------------------------------------------------
    # Cancelled trips
    # ---------------------------------------------------------
    cancelled_trips = trips_qs.filter(
        status_id__status_code="cancelled"
    ).count()
    # ---------------------------------------------------------
    # Active trips
    #
    # requested / accepted / reached / in_progress
    # ---------------------------------------------------------
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
    rating_value = (
        driver.ratings
        if getattr(driver, "ratings", None) is not None
        else Decimal("0.00")
    )
    try:
        rating_value = Decimal(str(rating_value))
    except (TypeError, ValueError):
        rating_value = Decimal("0.00")
    # =========================================================
    # REVENUE
    #
    # Use final_fare for completed trips.
    # If final_fare is NULL, use estimated_fare.
    # =========================================================
    revenue_data = trips_qs.filter(
        status_id__status_code="completed"
    ).aggregate(
        total_revenue=Sum("final_fare")
    )
    total_revenue = (
        revenue_data.get("total_revenue")
        or Decimal("0.00")
    )
    # =========================================================
    # TOTAL DISTANCE
    #
    # Prefer actual_distance_km.
    # =========================================================
    distance_data = trips_qs.aggregate(
        total_distance=Sum("actual_distance_km")
    )
    total_distance = (
        distance_data.get("total_distance")
        or Decimal("0.00")
    )
    # =========================================================
    # VEHICLE
    #
    # First use Driver.active_vehicle.
    #
    # If active_vehicle_id is NULL, look for an active vehicle
    # belonging to this driver.
    #
    # This handles your current Driver #3 data:
    #
    # Driver #3 -> active_vehicle_id = NULL
    # Vehicle #2 -> driver_id = 3, status = active
    # =========================================================
    vehicle = getattr(
        driver,
        "active_vehicle",
        None,
    )
    if vehicle is None:
        vehicle = (
            Vehicle.objects
            .filter(
                driver_id=driver,
                status="active",
            )
            .select_related("vehicle_type_id")
            .order_by("id")
            .first()
        )
    context = {
        # =====================================================
        # DRIVER
        # =====================================================
        "driver": driver,
        "driver_id": driver.id,
        "driver_status": driver_status,
        "is_blocked": is_blocked,
        "driver_name": driver_name,
        "phone_number": phone_number,
        "email": email,
        # =====================================================
        # APPROVAL / DOCUMENT
        # =====================================================
        "approved": bool(
            getattr(
                driver,
                "approved",
                False,
            )
        ),
        "doc_status": getattr(
            driver,
            "doc_status",
            "pending",
        ),
        "document_status": getattr(
            driver,
            "doc_status",
            "pending",
        ),
        # =====================================================
        # PERFORMANCE
        # =====================================================
        "total_trips": total_trips,
        "completed_trips": completed_trips,
        "cancelled_trips": cancelled_trips,
        "active_trips": active_trips,
        "rating": rating_value,
        "total_revenue": total_revenue,
        "total_distance": total_distance,
        "trips": trips_qs[:50],
    }
    # =========================================================
    # VEHICLE INFORMATION
    # =========================================================
    context["vehicle"] = vehicle
    if vehicle:
        context["vehicle_id"] = getattr(
            vehicle,
            "id",
            None,
        )
        context["vehicle_number"] = getattr(
            vehicle,
            "vehicle_number",
            "",
        )
        context["vehicle_brand"] = getattr(
            vehicle,
            "brand",
            "",
        )
        context["vehicle_model"] = getattr(
            vehicle,
            "model",
            "",
        )
        context["vehicle_color"] = getattr(
            vehicle,
            "color",
            "",
        )
        context["vehicle_year"] = getattr(
            vehicle,
            "year",
            "",
        )
        context["vehicle_capacity"] = getattr(
            vehicle,
            "capacity",
            "",
        )
        context["vehicle_status"] = getattr(
            vehicle,
            "status",
            "",
        )
        # -----------------------------------------------------
        # Vehicle Type
        #
        # Actual model:
        #
        # Vehicle.vehicle_type_id -> VehicleType
        # VehicleType.type
        # -----------------------------------------------------
        vehicle_type = getattr(
            vehicle,
            "vehicle_type_id",
            None,
        )
        if vehicle_type:
            context["vehicle_type"] = getattr(
                vehicle_type,
                "type",
                "",
            )
        else:
            context["vehicle_type"] = ""
        # -----------------------------------------------------
        # RC document
        #
        # Actual model field is rc_doc
        # -----------------------------------------------------
        rc_value = getattr(
            vehicle,
            "rc_doc",
            None,
        )
        context["rc_available"] = bool(
            rc_value
        )
    else:
        context.update(
            {
                "vehicle_id": None,
                "vehicle_number": "",
                "vehicle_type": "",
                "vehicle_brand": "",
                "vehicle_model": "",
                "vehicle_color": "",
                "vehicle_year": "",
                "vehicle_capacity": "",
                "vehicle_status": "",
                "rc_available": False,
            }
        )
    # =========================================================
    # OPTIONAL: DRIVER'S OWN total_trips FIELD
    #
    # Keep database value available to template if needed.
    # =========================================================
    context["driver_total_trips"] = getattr(
        driver,
        "total_trips",
        0,
    )
    # =========================================================
    # RENDER
    # =========================================================
    return render(
        request,
        "admin_pages/driver_profile.html",
        context,
    )
