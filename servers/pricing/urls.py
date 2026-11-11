from django.urls import include, path
from rest_framework.routers import DefaultRouter

from servers.pricing.views import (
    RateCardViewSet,
    ServiceZoneViewSet,
    list_active_zones,
    quote,
)


from . import fee_plan_views
from .commission_views import (
    commission_matrix, config_list, config_update, fare_cards_overview, fare_cards_preview,
    regulation_list, regulation_update, state_cap_update,
    rule_detail, rule_preview, rule_submit, rules,
)
from .market_views import list_countries
from .vehicle_admin import vehicle_class_detail, vehicle_classes
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
    path('admin/commission/config/<str:country>/states/<str:state>/', state_cap_update,
         name='commission-state-cap-update'),
    path('admin/regulation/', regulation_list, name='regulation'),
    path('admin/fee-plans/', fee_plan_views.fee_plans_list, name='fee-plans'),
    path('admin/fee-plans/simulate/', fee_plan_views.fee_plan_simulate, name='fee-plans-simulate'),
    path('admin/fee-plans/<int:plan_id>/', fee_plan_views.fee_plan_detail, name='fee-plan'),
    path('admin/fee-plans/<int:plan_id>/submit/', fee_plan_views.fee_plan_submit,
         name='fee-plan-submit'),
    path('admin/cost-per-trip/', fee_plan_views.cost_per_trip, name='cost-per-trip'),
    path('admin/fixed-costs/<str:country>/<str:month>/', fee_plan_views.fixed_cost_update,
         name='fixed-cost'),
    path('admin/regulation/<str:country>/', regulation_update,
         name='regulation-update'),
    path('admin/vehicle-classes/', vehicle_classes, name='vehicle-classes'),
    path('admin/vehicle-classes/<int:pk>/', vehicle_class_detail, name='vehicle-class'),
    path('admin/fare-cards/overview/', fare_cards_overview, name='fare-cards-overview'),
    path('admin/fare-cards/preview/', fare_cards_preview, name='fare-cards-preview'),
    path('', include(router.urls)),
]
