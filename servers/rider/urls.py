from django.urls import path
from .views import (
    save_favorite_locations, get_favorite_locations, get_nearby_drivers,
    list_notifications, mark_notification_read, mark_all_notifications_read,
    get_wallet_balance, delete_favorite_location, get_wallet_transactions, wallet_payment,
    get_notification_preferences, update_notification_preferences,
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
    # Notification preferences (DPDP)
    path('notifications/preferences/', get_notification_preferences),
    path('notifications/preferences/update/', update_notification_preferences),
    # Wallet
    path('wallet/balance/', get_wallet_balance),
    path('wallet/transactions/', get_wallet_transactions),
    path('wallet/payment/', wallet_payment),
]
