from rest_framework import serializers
from base.serializer_fields import MoneyField
from .models import Payment, TransactionHistory

class PaymentSerializer(serializers.ModelSerializer):
    # `amount` as it has always been sent; the minor-unit column stays internal.
    amount = MoneyField()

    class Meta:
        model = Payment
        exclude = ['amount_minor']

class TransactionHistorySerializer(serializers.ModelSerializer):
    amount = MoneyField()

    class Meta:
        model = TransactionHistory
        exclude = ['amount_minor']
