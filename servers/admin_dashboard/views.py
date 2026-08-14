from datetime import datetime, time, timedelta
from decimal import Decimal
from functools import wraps
from django.utils import timezone
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
# from servers.driver.admin_utils import list_drivers_admin
from servers.driver.models import Driver,WithdrawalRequest
from servers.support.models import SupportTicket
from django.core.paginator import Paginator
from servers.ride.models import FarePricing, Trip
from django.db import models

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

    trip_qs = Trip.objects.filter(status_id__status_code="completed")
    if start_day:
        start_dt = timezone.make_aware(datetime.combine(start_day, time.min), tz)
        trip_qs = trip_qs.filter(completed_at__gte=start_dt)
    if end_day:
        end_dt = timezone.make_aware(datetime.combine(end_day + timedelta(days=1), time.min), tz)
        trip_qs = trip_qs.filter(completed_at__lt=end_dt)

    completed_trips = trip_qs.select_related("requested_vehicle_type", "vehicle_id__vehicle_type_id")
    # ---------------------------------------------------------
    # 2. TOTAL RIDES
    # ---------------------------------------------------------

    total_rides = completed_trips.count()
    # ---------------------------------------------------------
    # 3. GROSS BOOKING VALUE
    # ---------------------------------------------------------

    gbv = completed_trips.aggregate(total=models.Sum("final_fare"))["total"] or Decimal("0.00")
    gbv = Decimal(str(gbv)) if not isinstance(gbv, Decimal) else gbv
    # ---------------------------------------------------------
    # 4. PLATFORM REVENUE
    # ---------------------------------------------------------
    platform_revenue = gbv * Decimal("0.15")
    take_rate = 15.0
    # ---------------------------------------------------------
    # 5. TOTAL / UTILIZED DRIVERS
    # ---------------------------------------------------------

    total_drivers = Driver.objects.count()
    utilized_drivers = (
        completed_trips.exclude(driver_id__isnull=True)
        .values_list("driver_id", flat=True)
        .distinct()
        .count() if total_rides else 0
    )
    fleet_utilization = round((utilized_drivers / total_drivers * 100) if total_drivers else 0, 1)
    # ---------------------------------------------------------
    # 6. REVENUE BY VEHICLE CLASS
    # ----------------------
    bucketed = {}
    for trip in completed_trips:
        vehicle_type = None
        if trip.vehicle_id and trip.vehicle_id.vehicle_type_id:
            vehicle_type = trip.vehicle_id.vehicle_type_id
        elif trip.requested_vehicle_type:
            vehicle_type = trip.requested_vehicle_type

        if not vehicle_type:
            continue

        name = getattr(vehicle_type, "type", None) or "Unknown"
        revenue = Decimal(str(trip.final_fare or Decimal("0.00")))
        bucketed[name] = bucketed.get(name, Decimal("0.00")) + revenue

    aggregated_breakdown = []
    for name, revenue in sorted(bucketed.items(), key=lambda item: item[1], reverse=True):
        pct = round((revenue / gbv * 100) if gbv else 0, 1)
        aggregated_breakdown.append({
            "name": name,
            "revenue": revenue,
            "pct": pct,
        })

      # ---------------------------------------------------------
    # 7. MONTHLY REVENUE
    # ---------------------------------------------------------

    monthly_data = {}

    for trip in completed_trips:

        if not trip.completed_at:
            continue

        month_key = trip.completed_at.strftime("%Y-%m")
        month_name = trip.completed_at.strftime("%b %Y")

        fare = Decimal(
            str(
                trip.final_fare
                or Decimal("0.00")
            )
        )

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
        key=lambda x: x["month"]
    )

    # ---------------------------------------------------------
    # 8. MONTH-OVER-MONTH GROWTH
    # ---------------------------------------------------------

    growth_trajectory = []

    previous_revenue = None

    for item in monthly_revenue:

        current_revenue = item["revenue"]

        if previous_revenue and previous_revenue > 0:

            growth = (
                (current_revenue - previous_revenue)
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
    # 9. CURRENT MONTH GROWTH
    # ---------------------------------------------------------

    revenue_growth = 0

    if len(monthly_revenue) >= 2:

        current = monthly_revenue[-1]["revenue"]
        previous = monthly_revenue[-2]["revenue"]

        if previous > 0:
            revenue_growth = round(
                (
                    (current - previous)
                    / previous
                ) * 100,
                1
            )

    # ---------------------------------------------------------
    # 10. BAR CHART DATA
    # ---------------------------------------------------------

    max_monthly_revenue = max(
        (
            item["revenue"]
            for item in monthly_revenue
        ),
        default=Decimal("0.00")
    )

    for item in monthly_revenue:

        if max_monthly_revenue > 0:
            item["height"] = round(
                (
                    item["revenue"]
                    / max_monthly_revenue
                ) * 100,
                1
            )
        else:
            item["height"] = 0

    # ---------------------------------------------------------
    # 11. CONTEXT
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

    return render(
        request,
        "admin_pages/executive_revenue.html",
        context
    )
@admin_required
def driver_loyalty(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_pages/driver_loyalty.html")


@admin_required
def fare_surge(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_pages/fare_surge.html")


@admin_required
def predictive_heatmaps(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_pages/predictive_heatmaps.html")


def admin_logout(request: HttpRequest) -> HttpResponse:
    auth_logout(request)
    return redirect("login")
