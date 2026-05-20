from django.urls import path
from .views import estimate_fare, ride_history, driver_history, trip_detail, rate_trip,trip_driver_details, get_active_trip, proxy_geocode, proxy_directions
from .admin_views import admin_list_trips, admin_live_locations

urlpatterns = [
    # path('ride-request/', ride_request),
    path('estimate-fare/', estimate_fare),
    path('ride-history/', ride_history),
    path('driver-history/', driver_history),
    path('active/', get_active_trip),
    path('trip/<int:trip_id>/', trip_detail),
    path('trip/<int:trip_id>/details/',trip_driver_details),
    path('rate-trip/', rate_trip),
    
    # Maps Proxy
    path('maps/geocode', proxy_geocode),
    path('maps/directions', proxy_directions),

    # Admin Panel APIs
    path('admin/trips/', admin_list_trips),
    path('admin/live-locations/', admin_live_locations),
]