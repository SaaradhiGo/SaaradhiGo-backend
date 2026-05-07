from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class PaymentGateway(models.TextChoices):
    """Supported payment gateways."""
    CASHFREE = 'cashfree', 'Cashfree'
    # Add more gateways as needed


class Payment(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]
    METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('online', 'Online'),
        ('wallet',"Wallet"),
        ('deferred', 'Deferred')
    ]

    trip_id = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='payments')
    user_id = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Payment gateway
    payment_gateway = models.CharField(
        max_length=20,
        choices=PaymentGateway.choices,
        default=PaymentGateway.CASHFREE,
        db_index=True
    )
    
    # Generic gateway fields
    gateway_order_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    gateway_payment_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    gateway_signature = models.CharField(max_length=512, blank=True, null=True)
    gateway_metadata = models.JSONField(default=dict, blank=True)  # Store gateway-specific data
    
    # Cashfree-specific fields (for backward compatibility with existing data)
    cashfree_order_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    cashfree_payment_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    cashfree_payment_session_id = models.CharField(max_length=256, blank=True, null=True)
    
    # Legacy fields kept for compatibility
    driver_txn_id = models.CharField(max_length=256, blank=True, null=True)
    driver_name = models.CharField(max_length=256, blank=True, null=True)

    # Payment method switching fields
    replaced_by = models.OneToOneField(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='replaces'
    )
    switch_reason = models.CharField(
        max_length=50, null=True, blank=True,
        choices=[('user_requested', 'User Requested'), ('system_error', 'System Error')]
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Payment {self.id} - Trip {self.trip_id.id} ({self.status})'

    class Meta:
        ordering = ['-created_at']


class TransactionHistory(models.Model):
    TXN_TYPE_CHOICES = [
        ('payment', 'Payment from Rider'),
        ('credit', 'Credit to Driver'),
        ('debit', 'Debit from Wallet'),
        ('refund', 'Refund to Rider'),
    ]

    trip_id = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='transactions', null=True, blank=True)
    user_id = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    driver_id = models.ForeignKey('driver.Driver', on_delete=models.CASCADE, related_name='transactions')
    withdrawal_request = models.ForeignKey('driver.WithdrawalRequest', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=50)
    
    # Payment gateway fields
    payment_gateway = models.CharField(
        max_length=20,
        choices=PaymentGateway.choices,
        default=PaymentGateway.CASHFREE,
        db_index=True
    )
    gateway_payment_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    gateway_transaction_id = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    
    # Cashfree-specific fields (for backward compatibility with existing data)
    cashfree_payment_id = models.CharField(max_length=256, blank=True, null=True)
    cashfree_transfer_id = models.CharField(max_length=256, blank=True, null=True)  # For payouts
    
    user_name = models.CharField(max_length=256, blank=True, null=True)
    user_txn_id = models.CharField(max_length=256, blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    txn_type = models.CharField(max_length=20, choices=TXN_TYPE_CHOICES, default='payment')
    gateway_metadata = models.JSONField(default=dict, blank=True)  # Store gateway-specific data
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Transaction {self.id} - Trip {self.trip_id.id}'

    class Meta:
        verbose_name_plural = 'Transaction histories'
        ordering = ['-created_at']
