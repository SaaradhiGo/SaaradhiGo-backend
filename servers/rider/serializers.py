from rest_framework import serializers
from .models import FavoritePlace, Rider, Notification, WalletTransaction


class RiderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rider
        fields = '__all__'


class FavoritePlaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = FavoritePlace
        fields = ['id', 'user_id', 'address_text', 'latitude', 'longitude']
        read_only_fields = ['user_id']


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'title', 'message', 'is_read', 'created_at']
class WalletTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WalletTransaction
        fields = ['id', 'user_id', 'amount', 'txn_type', 'status', 'gateway_order_id', 'gateway_payment_id', 'gateway_signature', 'payment_gateway', 'created_at']