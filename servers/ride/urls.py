from django.urls import path
from .views import (
    estimate_fare, ride_history, driver_history, trip_detail, rate_trip,
    trip_driver_details, get_active_trip, proxy_geocode, proxy_directions,
    proxy_place_details, proxy_reverse_geocode, maps_client_meter,
    driver_cancel_trip, rider_cancel_trip, rider_cancel_preview, resend_receipt, receipt_pdf,
    apply_promo_endpoint, trip_chat_history, trip_eta, booking_options,
)
from .admin_views import admin_list_trips, admin_dashboard, admin_trip_detail
from .admin_stale import (
    admin_stale_mark_reviewed, admin_stale_release_driver, admin_stale_release_preview,
)
from .admin_trips import (
    admin_adjust_fare, admin_bulk_trips, admin_cancel_trip, admin_flag_trip,
    admin_note_trip, admin_refund_trip, admin_resend_pickup_code, admin_unflag_trip,
)

from . import zero_tolerance  # noqa: E402
from servers.payments import card_hold  # noqa: E402

urlpatterns = [
    # path('ride-request/', ride_request),
    path('estimate-fare/', estimate_fare),
    path('ride-history/', ride_history),
    path('driver-history/', driver_history),
    path('active/', get_active_trip),
    path('trip/<int:trip_id>/', trip_detail),
    path('trips/<int:trip_id>/report-impairment/', zero_tolerance.report_impairment),
    path('trips/<int:trip_id>/tip/', card_hold.tip_view),
    path('trip/<int:trip_id>/details/',trip_driver_details),
    path('trip/<int:trip_id>/eta/', trip_eta),
    path('trip/<int:trip_id>/driver-cancel/', driver_cancel_trip),
    path('trip/<int:trip_id>/cancel/', rider_cancel_trip),
    path('trip/<int:trip_id>/cancel-preview/', rider_cancel_preview),
    path('trip/<int:trip_id>/receipt/resend/', resend_receipt),
    path('trip/<int:trip_id>/receipt/pdf/', receipt_pdf),
    path('trip/<int:trip_id>/chat/', trip_chat_history),
    path('promo/apply/', apply_promo_endpoint),
    path('rate-trip/', rate_trip),

    # Maps Proxy
    path('maps/geocode', proxy_geocode),
    path('maps/place-details', proxy_place_details),
    path('maps/reverse-geocode', proxy_reverse_geocode),
    path('booking-options/', booking_options),
    path('maps/directions', proxy_directions),
    path('maps/meter/', maps_client_meter),

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
    path('admin/trips/<int:trip_id>/resend-pickup-code/', admin_resend_pickup_code),
    path('admin/trips/<int:trip_id>/stale/mark-reviewed/', admin_stale_mark_reviewed),
    path('admin/trips/<int:trip_id>/stale/release-driver/', admin_stale_release_driver),
    path('admin/trips/<int:trip_id>/stale/release-preview/', admin_stale_release_preview),
    path('admin/dashboard/', admin_dashboard),
]