from django.urls import path
from .views import (
    estimate_fare, ride_history, driver_history, trip_detail, rate_trip,
    trip_driver_details, get_active_trip, proxy_geocode, proxy_directions,
    proxy_place_details, proxy_reverse_geocode,
    driver_cancel_trip, rider_cancel_trip, resend_receipt, receipt_pdf,
    apply_promo_endpoint, trip_chat_history,
)
from .admin_views import admin_list_trips, admin_dashboard, admin_trip_detail
from .admin_stale import admin_stale_mark_reviewed, admin_stale_release_driver
from .admin_trips import (
    admin_adjust_fare, admin_bulk_trips, admin_cancel_trip, admin_flag_trip,
    admin_note_trip, admin_refund_trip, admin_unflag_trip,
)

urlpatterns = [
    # path('ride-request/', ride_request),
    path('estimate-fare/', estimate_fare),
    path('ride-history/', ride_history),
    path('driver-history/', driver_history),
    path('active/', get_active_trip),
    path('trip/<int:trip_id>/', trip_detail),
    path('trip/<int:trip_id>/details/',trip_driver_details),
    path('trip/<int:trip_id>/driver-cancel/', driver_cancel_trip),
    path('trip/<int:trip_id>/cancel/', rider_cancel_trip),
    path('trip/<int:trip_id>/receipt/resend/', resend_receipt),
    path('trip/<int:trip_id>/receipt/pdf/', receipt_pdf),
    path('trip/<int:trip_id>/chat/', trip_chat_history),
    path('promo/apply/', apply_promo_endpoint),
    path('rate-trip/', rate_trip),

    # Maps Proxy
    path('maps/geocode', proxy_geocode),
    path('maps/place-details', proxy_place_details),
    path('maps/reverse-geocode', proxy_reverse_geocode),
    path('maps/directions', proxy_directions),

    # Admin Panel APIs
    path('admin/trips/', admin_list_trips),
    path('admin/trips/<int:trip_id>/', admin_trip_detail),
    # Trips module writes (console module 2)
    path('admin/trips/bulk/', admin_bulk_trips),
    path('admin/trips/<int:trip_id>/cancel/', admin_cancel_trip),
    path('admin/trips/<int:trip_id>/refund/', admin_refund_trip),
    path('admin/trips/<int:trip_id>/adjust-fare/', admin_adjust_fare),
    path('admin/trips/<int:trip_id>/flag/', admin_flag_trip),
    path('admin/trips/<int:trip_id>/unflag/', admin_unflag_trip),
    path('admin/trips/<int:trip_id>/note/', admin_note_trip),
    path('admin/trips/<int:trip_id>/stale/mark-reviewed/', admin_stale_mark_reviewed),
    path('admin/trips/<int:trip_id>/stale/release-driver/', admin_stale_release_driver),
    path('admin/dashboard/', admin_dashboard),
]