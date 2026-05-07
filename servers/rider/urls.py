from django.urls import path
from .views import (
    save_favorite_locations, get_favorite_locations, get_nearby_drivers,
    list_notifications, mark_notification_read, mark_all_notifications_read,
    get_wallet_balance, delete_favorite_location, create_wallet_order, verify_wallet_payment, get_wallet_transactions, wallet_payment
)

urlpatterns=[
    path('locations/',save_favorite_locations),
    path('locations/all/',get_favorite_locations),
    path('locations/<int:location_id>/delete/',delete_favorite_location),
    path('nearby/',get_nearby_drivers),
    # Notifications
    path('notifications/', list_notifications),
    path('notifications/<int:notif_id>/read/', mark_notification_read),
    path('notifications/read-all/', mark_all_notifications_read),
    # Wallet
    path('wallet/balance/', get_wallet_balance),
    path('wallet/create-order/', create_wallet_order),
    path('wallet/verify/', verify_wallet_payment),
    path('wallet/transactions/', get_wallet_transactions),
    path('wallet/payment/', wallet_payment),
]
