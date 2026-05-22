from django.urls import path
from .views import (
    estimate_fare, ride_history, driver_history, trip_detail, rate_trip,
    trip_driver_details, get_active_trip, proxy_geocode, proxy_directions,
    driver_cancel_trip, resend_receipt, receipt_pdf,
    apply_promo_endpoint, trip_chat_history,
)
from .admin_views import admin_list_trips, admin_live_locations, admin_dashboard, admin_trip_detail

urlpatterns = [
    # path('ride-request/', ride_request),
    path('estimate-fare/', estimate_fare),
    path('ride-history/', ride_history),
    path('driver-history/', driver_history),
    path('active/', get_active_trip),
    path('trip/<int:trip_id>/', trip_detail),
    path('trip/<int:trip_id>/details/',trip_driver_details),
    path('trip/<int:trip_id>/driver-cancel/', driver_cancel_trip),
    path('trip/<int:trip_id>/receipt/resend/', resend_receipt),
    path('trip/<int:trip_id>/receipt/pdf/', receipt_pdf),
    path('trip/<int:trip_id>/chat/', trip_chat_history),
    path('promo/apply/', apply_promo_endpoint),
    path('rate-trip/', rate_trip),

    # Maps Proxy
    path('maps/geocode', proxy_geocode),
    path('maps/directions', proxy_directions),

    # Admin Panel APIs
    path('admin/trips/', admin_list_trips),
    path('admin/trips/<int:trip_id>/', admin_trip_detail),
    path('admin/live-locations/', admin_live_locations),
    path('admin/dashboard/', admin_dashboard),
]