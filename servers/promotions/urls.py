from django.urls import path

from . import views

urlpatterns = [
    # --- the rider / driver apps ----------------------------------------------------------
    path('referrals/attach/', views.referral_attach, name='promotions_referral_attach'),
    path('referrals/mine/', views.referral_mine, name='promotions_referral_mine'),
    path('driver/incentives/', views.driver_incentives, name='promotions_driver_incentives'),

    # --- the operator console (everything under admin/ is gated and swept) ----------------
    path('admin/campaigns/', views.CampaignCollection.as_view(), name='promo_campaigns'),
    path('admin/campaigns/<int:promotion_id>/', views.CampaignItem.as_view(),
         name='promo_campaign'),
    path('admin/campaigns/<int:promotion_id>/submit/', views.campaign_submit,
         name='promo_campaign_submit'),
    path('admin/campaigns/<int:promotion_id>/pause/', views.campaign_pause,
         name='promo_campaign_pause'),
    path('admin/campaigns/<int:promotion_id>/resume/', views.campaign_resume,
         name='promo_campaign_resume'),
    path('admin/campaigns/<int:promotion_id>/cancel/', views.campaign_cancel,
         name='promo_campaign_cancel'),
    path('admin/campaigns/<int:promotion_id>/budget-increase/', views.campaign_budget_increase,
         name='promo_campaign_budget_increase'),
    path('admin/campaigns/<int:promotion_id>/activity/', views.campaign_activity,
         name='promo_campaign_activity'),
    path('admin/options/', views.options, name='promo_options'),
]
