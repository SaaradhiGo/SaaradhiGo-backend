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
    global_search_api,
    login,
    ops_mfa_challenge,
    payment_dashboard,
    predictive_heatmaps,
    ride,
    ride_detail,
    riders,
    promo_codes,
    emergency_dashboard,
    stale_rides,
    transaction_dashboard,
    update_global_config,
    notifications,
)

urlpatterns = [
    path("login/", login, name="login"),
    # The second-factor challenge. Reached only by a session whose password
    # already succeeded; @admin_required redirects here until it is cleared.
    path("mfa/", ops_mfa_challenge, name="ops_mfa_challenge"),
    path("", dashboard, name="fleet_monitor"),
    path("driver_onboarding/", driver_onboarding, name="driver_onboarding"),
    path("dispute_support/", dispute_support, name="dispute_support"),
    path("payment_dashboard/", payment_dashboard, name="payment_dashboard"),
    path("executive_revenue/", executive_revenue, name="executive_revenue"),
    path("driver_loyalty/", driver_loyalty, name="driver_loyalty"),
    path("ride/", ride, name="ride"),
    path("ride/<int:trip_id>/", ride_detail, name="ride_detail"),
    path("riders/", riders, name="riders"),
    path("promo-codes/", promo_codes, name="promo_codes"),
    path("emergency/", emergency_dashboard, name="emergency_dashboard"),
    # Operator queue for active rides that stopped looking alive. Detection plus
    # two safe actions only; see the view docstring on why termination is not
    # exposed.
    path("stale-rides/", stale_rides, name="stale_rides"),
    path("transactions/", transaction_dashboard, name="transaction_dashboard"),
    path("fare_surge/", fare_surge, name="fare_surge"),
    path("api/global-config/", update_global_config, name="update_global_config"),
    path("predictive_heatmaps/", predictive_heatmaps, name="predictive_heatmaps"),
    path("logout/", admin_logout, name="logout"),

    # Global Search
    path("search/", global_search, name="global_search"),
    path("api/search/", global_search_api, name="global_search_api"),

    # Driver Profile
    path("driver/<int:driver_id>/", driver_profile, name="driver_profile"),
    path("notifications/", notifications, name="notifications"),
]