from django.urls import path
from .views import login, dashboard, driver_onboarding, dispute_support, payment_dashboard, executive_revenue, driver_loyalty,ride, fare_surge, predictive_heatmaps, admin_logout, update_global_config

urlpatterns=[
    path('login/',login,name='login'),
    path('',dashboard,name='fleet_monitor'),
    path('driver_onboarding/',driver_onboarding,name='driver_onboarding'),
    path('dispute_support/',dispute_support,name='dispute_support'),
    path('payment_dashboard/',payment_dashboard,name='payment_dashboard'),
    path('executive_revenue/',executive_revenue,name='executive_revenue'),
    path('driver_loyalty/',driver_loyalty,name='driver_loyalty'),
    path('ride/', ride, name='ride'),
    path('fare_surge/',fare_surge,name='fare_surge'),
    path('api/global-config/',update_global_config,name='update_global_config'),
    path('predictive_heatmaps/',predictive_heatmaps,name='predictive_heatmaps'),
    path('logout/',admin_logout,name='logout')
]