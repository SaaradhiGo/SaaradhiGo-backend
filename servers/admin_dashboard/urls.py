from django.urls import path

from .views import (
    admin_logout,
    dashboard,
    dispute_support,
    driver_loyalty,
    driver_onboarding,
    driver_profile,
    executive_revenue,
    fare_surge,
    global_search,
    login,
    payment_dashboard,
    predictive_heatmaps,
    ride,
    riders,
    promo_codes,
    emergency_dashboard,
    transaction_dashboard,
    update_global_config,
)

urlpatterns = [
    path("login/", login, name="login"),
    path("", dashboard, name="fleet_monitor"),
    path("driver_onboarding/", driver_onboarding, name="driver_onboarding"),
    path("dispute_support/", dispute_support, name="dispute_support"),
    path("payment_dashboard/", payment_dashboard, name="payment_dashboard"),
    path("executive_revenue/", executive_revenue, name="executive_revenue"),
    path("driver_loyalty/", driver_loyalty, name="driver_loyalty"),
    path("ride/", ride, name="ride"),
    path("riders/", riders, name="riders"),
    path("promo-codes/", promo_codes, name="promo_codes"),
    path("emergency/", emergency_dashboard, name="emergency_dashboard"),
    path("transactions/", transaction_dashboard, name="transaction_dashboard"),
    path("fare_surge/", fare_surge, name="fare_surge"),
    path("api/global-config/", update_global_config, name="update_global_config"),
    path("predictive_heatmaps/", predictive_heatmaps, name="predictive_heatmaps"),
    path("logout/", admin_logout, name="logout"),

    # Global Search
    path("search/", global_search, name="global_search"),

    # Driver Profile
    path("driver/<int:driver_id>/", driver_profile, name="driver_profile"),
]