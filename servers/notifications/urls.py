from django.urls import path

from . import app_views, views, webhooks

urlpatterns = [
    # --- the rider / driver app: their own preferences ---------------------------------
    path('preferences/', app_views.my_preferences, name='notifications_preferences'),

    # --- provider webhooks: authenticated by signature or shared secret, not by a user ---
    path('webhooks/msg91/<str:token>/', webhooks.webhook, {'provider': 'msg91'},
         name='notifications_webhook_msg91_token'),
    path('webhooks/<str:provider>/', webhooks.webhook, name='notifications_webhook'),

    # --- the operator console (everything under admin/ is gated and swept) -----------------
    path('admin/templates/', views.TemplateCollection.as_view(), name='notify_templates'),
    path('admin/templates/preview/', views.template_preview_adhoc, name='notify_template_preview_adhoc'),
    path('admin/templates/<int:template_id>/', views.TemplateItem.as_view(), name='notify_template'),
    path('admin/templates/<int:template_id>/registrations/', views.template_registrations,
         name='notify_template_registrations'),
    path('admin/templates/<int:template_id>/preview/', views.template_preview,
         name='notify_template_preview'),
    path('admin/templates/<int:template_id>/submit/', views.template_submit,
         name='notify_template_submit'),
    path('admin/templates/<int:template_id>/archive/', views.template_archive,
         name='notify_template_archive'),
    path('admin/campaigns/', views.CampaignCollection.as_view(), name='notify_campaigns'),
    path('admin/campaigns/audience-preview/', views.campaign_audience_preview,
         name='notify_campaign_audience'),
    path('admin/campaigns/<int:campaign_id>/', views.CampaignItem.as_view(), name='notify_campaign'),
    path('admin/campaigns/<int:campaign_id>/submit/', views.campaign_submit,
         name='notify_campaign_submit'),
    path('admin/campaigns/<int:campaign_id>/cancel/', views.campaign_cancel,
         name='notify_campaign_cancel'),
    path('admin/cities/', views.campaign_cities, name='notify_cities'),
    path('admin/messages/', views.message_list, name='notify_messages'),
    path('admin/messages/<int:message_id>/', views.message_detail, name='notify_message'),
    path('admin/providers/', views.provider_state, name='notify_providers'),
    path('admin/users/<int:user_id>/preferences/', views.UserPreferences.as_view(),
         name='notify_user_preferences'),
]
