from django.urls import path
from .views import (
    driver_earnings, driver_earnings_summary,
    list_vehicles, create_vehicle, update_vehicle, delete_vehicle, update_driver_profile, get_driver_profile,
    driver_withdrawal_balance, driver_withdrawal_request, driver_withdrawal_history, driver_withdrawal_block_status,
    driver_incentives, driver_elite_status
)
from .onboarding_views import driver_onboarding
from .admin_drivers import (
    admin_approve_driver, admin_bulk_drivers, admin_kyc_queue, admin_note_driver,
    admin_reinstate_driver, admin_review_document, admin_suspend_driver, admin_vehicle_status,
)
from .admin_views import (
    list_drivers_admin, retrieve_driver_admin, driver_full_detail_admin,
    update_kyc_status_admin, delete_driver_admin, get_vehicle_details,
    list_withdrawals_admin, approve_withdrawal_admin, reject_withdrawal_admin, bulk_action_withdrawal_admin
)

urlpatterns = [
    # path('add/', add_driver),
    # path('update_location/', update_location),
    # path('remove_driver/', remove_driver_view),
    # Driver
    path('driver/update/',update_driver_profile),
    path('driver/profile/',get_driver_profile),
    # What this driver still has to supply before an operator can approve
    # them, computed from the same compliance rules the console reviews
    # against. See onboarding_views for why it is not a second list.
    path('driver/onboarding/', driver_onboarding),
    path('driver/incentives/', driver_incentives),
    path('driver/elite-status/', driver_elite_status),
    # Earnings
    path('earnings/', driver_earnings),
    path('earnings/summary/', driver_earnings_summary),
    # Withdrawals
    path('withdrawals/balance/', driver_withdrawal_balance),
    path('withdrawals/request/', driver_withdrawal_request),
    path('withdrawals/history/', driver_withdrawal_history),
    path('withdrawals/block-status/', driver_withdrawal_block_status),
    # Vehicles
    path('vehicles/', list_vehicles),
    path('vehicles/add/', create_vehicle),
    path('vehicles/<int:vehicle_id>/', update_vehicle),
    path('vehicles/<int:vehicle_id>/delete/', delete_vehicle),

    # Admin URLs
    path('admin/', list_drivers_admin),
    path('admin/<int:driver_id>/', retrieve_driver_admin),
    path('admin/<int:driver_id>/full/', driver_full_detail_admin),
    path('admin/<int:driver_id>/update-kyc/', update_kyc_status_admin),
    path('admin/<int:driver_id>/delete/', delete_driver_admin),
    path('admin/<int:driver_id>/vehicles/', get_vehicle_details),
    # Drivers & KYC module (console module 3)
    path('admin/kyc-queue/', admin_kyc_queue),
    path('admin/bulk/', admin_bulk_drivers),
    path('admin/<int:driver_id>/documents/review/', admin_review_document),
    path('admin/<int:driver_id>/approve/', admin_approve_driver),
    path('admin/<int:driver_id>/suspend/', admin_suspend_driver),
    path('admin/<int:driver_id>/reinstate/', admin_reinstate_driver),
    path('admin/<int:driver_id>/note/', admin_note_driver),
    path('admin/<int:driver_id>/vehicles/<int:vehicle_id>/status/', admin_vehicle_status),
    
    # Admin Withdrawal Management
    path('admin/withdrawals/', list_withdrawals_admin),
    path('admin/withdrawals/<int:withdrawal_id>/approve/', approve_withdrawal_admin),
    path('admin/withdrawals/<int:withdrawal_id>/reject/', reject_withdrawal_admin),
    path('admin/withdrawals/bulk-action/', bulk_action_withdrawal_admin),
]