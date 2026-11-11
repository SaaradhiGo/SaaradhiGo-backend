from django.urls import path

from . import views

urlpatterns = [
    path('admin/flags/', views.flag_list, name='fraud_flags'),
    path('admin/flags/<int:flag_id>/', views.flag_detail, name='fraud_flag_detail'),
    path('admin/flags/<int:flag_id>/assign/', views.flag_assign),
    path('admin/flags/<int:flag_id>/unassign/', views.flag_unassign),
    path('admin/flags/<int:flag_id>/note/', views.flag_note),
    path('admin/flags/<int:flag_id>/confirm/', views.flag_confirm),
    path('admin/flags/<int:flag_id>/dismiss/', views.flag_dismiss),
    path('admin/flags/<int:flag_id>/reopen/', views.flag_reopen),
    path('admin/flags/<int:flag_id>/handoff/', views.flag_handoff),
    path('admin/flags/<int:flag_id>/reviewers/', views.flag_reviewers),
    path('admin/flags/<int:flag_id>/recover/', views.flag_recover),
    path('admin/flags/<int:flag_id>/payout-hold/', views.flag_payout_hold),
    path('admin/flags/<int:flag_id>/restrict-cash/', views.flag_restrict_cash),
    path('admin/payout-holds/<int:hold_id>/release/', views.payout_hold_release),
    path('admin/rules/', views.rules_list, name='fraud_rules'),
    path('admin/rules/<str:country>/<str:rule_key>/', views.rule_update),
    path('admin/options/', views.options, name='fraud_options'),
]
