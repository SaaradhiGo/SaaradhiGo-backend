from rest_framework import serializers
from base.serializer_fields import MoneyField
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
    amount = MoneyField(max_digits=12)
    #: Additive: a plain-words label for the lines that need one (a recovery of a reward).
    label = serializers.SerializerMethodField()

    def get_label(self, obj):
        return 'Recovery of reward (reviewed)' if obj.purpose == 'reward_recovery' else None

    class Meta:
        model = WalletTransaction
        fields = ['id', 'user_id', 'amount', 'txn_type', 'status', 'gateway_order_id', 'gateway_payment_id', 'gateway_signature', 'payment_gateway', 'created_at', 'label']