from django.urls import path
from .views import (
    create_order, verify_payment, payment_webhook, payout_webhook, payment_history, refund_payment,
    switch_payment_method, retry_payment, pending_payments
)
from .admin_views import admin_list_transactions
from .admin_actions import (
    driver_adjust, payout_approve, payout_bulk, payout_config_propose, payout_investigation,
    payout_reject, payout_retry, rider_wallet_credit,
)
from .admin_finance import (
    finance_driver_ledger, finance_export, finance_overview, finance_payment_detail,
    finance_payments, finance_payout_config, finance_payout_detail, finance_payouts,
    finance_reconciliation, finance_reconciliation_items, finance_reconciliation_resolve,
    finance_settlements,
)

urlpatterns = [
    path('create-order/', create_order),
    path('verify/', verify_payment),
    path('webhook/', payment_webhook),
    path('payout-webhook/', payout_webhook),
    path('history/', payment_history),
    path('refund/', refund_payment),
    path('switch/', switch_payment_method),
    path('retry/', retry_payment),
    path('pending/', pending_payments),
    
    # Admin Panel APIs
    path('admin/payments/', finance_payments),
    path('admin/payments/<int:payment_id>/', finance_payment_detail),
    path('admin/transactions/', admin_list_transactions),
    # Finance read side (module 7, part A). Scope: servers/payments/scope.py.
    path('admin/settlements/', finance_settlements),
    path('admin/payouts/', finance_payouts),
    path('admin/payouts/<int:payout_id>/', finance_payout_detail),
    # Finance write side (module 7, part B). Every money write is a maker-checker request.
    path('admin/payouts/bulk-action/', payout_bulk),
    path('admin/payouts/<int:payout_id>/approve/', payout_approve),
    path('admin/payouts/<int:payout_id>/reject/', payout_reject),
    path('admin/payouts/<int:payout_id>/retry/', payout_retry),
    path('admin/payouts/<int:payout_id>/investigation/', payout_investigation),
    path('admin/drivers/<int:driver_id>/adjust/', driver_adjust),
    path('admin/riders/<int:rider_id>/wallet-credit/', rider_wallet_credit),
    path('admin/payout-config/<str:country>/', payout_config_propose),
    path('admin/drivers/<int:driver_id>/ledger/', finance_driver_ledger),
    path('admin/overview/', finance_overview),
    path('admin/reconciliation/', finance_reconciliation),
    path('admin/reconciliation/items/', finance_reconciliation_items),
    path('admin/reconciliation/items/<int:item_id>/resolve/', finance_reconciliation_resolve),
    path('admin/payout-config/', finance_payout_config),
    path('admin/export/<str:kind>/', finance_export),
]
