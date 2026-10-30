from django.urls import include, path
from rest_framework.routers import DefaultRouter

from servers.pricing.views import (
    RateCardViewSet,
    ServiceZoneViewSet,
    list_active_zones,
    quote,
)


from .commission_views import (
    commission_matrix, config_list, config_update, fare_cards_overview, fare_cards_preview,
    rule_detail, rule_preview, rule_submit, rules,
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
    # Module 8: commission matrix / rules / config, and the fare-card overview.
    path('admin/commission/matrix/', commission_matrix, name='commission-matrix'),
    path('admin/commission/rules/', rules, name='commission-rules'),
    path('admin/commission/rules/<int:rule_id>/', rule_detail, name='commission-rule'),
    path('admin/commission/rules/<int:rule_id>/preview/', rule_preview,
         name='commission-rule-preview'),
    path('admin/commission/rules/<int:rule_id>/submit/', rule_submit,
         name='commission-rule-submit'),
    path('admin/commission/config/', config_list, name='commission-config'),
    path('admin/commission/config/<str:country>/', config_update,
         name='commission-config-update'),
    path('admin/fare-cards/overview/', fare_cards_overview, name='fare-cards-overview'),
    path('admin/fare-cards/preview/', fare_cards_preview, name='fare-cards-preview'),
    path('', include(router.urls)),
]
