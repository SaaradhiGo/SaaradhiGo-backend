from django.urls import path

from . import views

# Mounted at api/v1/platform/. Everything operator-facing is under `admin/` so the structural
# guard finds each route; the public app-config lives at api/v1/app-config/ (servers/urls.py).
urlpatterns = [
    path('admin/markets/', views.markets_list, name='platform_markets'),
    path('admin/markets/nodes/', views.node_add, name='platform_node_add'),
    path('admin/markets/<str:city_code>/readiness/', views.city_readiness,
         name='platform_city_readiness'),
    path('admin/markets/<str:city_code>/status/', views.city_status, name='platform_city_status'),
    path('admin/flags/', views.flags_collection, name='platform_flags'),
    path('admin/flags/<str:key>/', views.flag_item, name='platform_flag'),
    path('admin/app-versions/', views.versions_list, name='platform_app_versions'),
    path('admin/app-versions/<str:app>/<str:platform>/', views.version_update,
         name='platform_app_version'),
    path('admin/notices/', views.notices_collection, name='platform_notices'),
    path('admin/notices/<int:notice_id>/', views.notice_update, name='platform_notice'),
    path('admin/notices/<int:notice_id>/cancel/', views.notice_cancel,
         name='platform_notice_cancel'),
    path('admin/settings/', views.settings_collection, name='platform_settings'),
    path('admin/settings/<str:key>/', views.setting_update, name='platform_setting'),
    path('admin/integrations/', views.integrations_status, name='platform_integrations'),
    path('admin/options/', views.options, name='platform_options'),
]
