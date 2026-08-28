from django.urls import path

from .views import (
    admin_logout,
    dashboard,
    dispute_support,
    driver_loyalty,
    driver_onboarding,
    executive_revenue,
    fare_surge,
    login,
    payment_dashboard,
    predictive_heatmaps,
    ride,
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
    path("transactions/", transaction_dashboard, name="transaction_dashboard"),
    path("fare_surge/", fare_surge, name="fare_surge"),
    path("api/global-config/", update_global_config, name="update_global_config"),
    path("predictive_heatmaps/", predictive_heatmaps, name="predictive_heatmaps"),
    path("logout/", admin_logout, name="logout"),
]
