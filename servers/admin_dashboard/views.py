from functools import wraps
from django.utils import timezone
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
# from servers.driver.admin_utils import list_drivers_admin
from servers.driver.models import Driver

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
    status=request.GET.get('status',None)
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
    return render(request, "admin_pages/driver_onboarding.html",{"pending_reviews_count":pending.count(),"approved_count":approved.count(),"drivers":drivers,"selected_driver":selected_driver,"page_number":page_number})


@admin_required
def dispute_support(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_pages/dispute_support.html")


@admin_required
def payment_dashboard(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_pages/payment_dashboard.html")


@admin_required
def executive_revenue(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_pages/executive_revenue.html")


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
