from django.urls import include, path
from rest_framework.routers import DefaultRouter

from servers.pricing.views import (
    RateCardViewSet,
    ServiceZoneViewSet,
    list_active_zones,
    quote,
)


from .market_views import list_countries
from .vehicle_views import list_vehicle_types

router = DefaultRouter()
router.register(r'admin/zones', ServiceZoneViewSet, basename='admin-zones')
router.register(r'admin/rate-cards', RateCardViewSet, basename='admin-rate-cards')

urlpatterns = [
    path('zones/', list_active_zones, name='public-zones'),
    path('countries/', list_countries, name='public-countries'),
    path('vehicle-types/', list_vehicle_types, name='public-vehicle-types'),
    path('quote/', quote, name='fare-quote'),
    path('', include(router.urls)),
]
