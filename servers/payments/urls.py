from django.urls import path
from .views import (
    create_order, verify_payment, payment_webhook, payout_webhook, payment_history, refund_payment,
    switch_payment_method, retry_payment, pending_payments
)
from .admin_views import admin_list_payments, admin_list_transactions

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
    path('admin/payments/', admin_list_payments),
    path('admin/transactions/', admin_list_transactions),
]
