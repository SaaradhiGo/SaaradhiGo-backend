from django.contrib import admin
from .models import (
    TripStatus, Trip, FarePricing, VehicleFarePricing, Rating, Receipt,
    ChatMessage, PromoCode, PromoRedemption,
)

admin.site.register(TripStatus)
admin.site.register(Trip)
admin.site.register(FarePricing)
admin.site.register(VehicleFarePricing)
admin.site.register(Rating)


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ('id', 'receipt_number', 'trip_id', 'user_id', 'total_fare', 'version', 'issued_at', 'last_sent_at')
    search_fields = ('receipt_number', 'trip_id__id', 'user_id__phone_number')
    list_filter = ('version',)
    readonly_fields = ('issued_at', 'last_sent_at', 'html_body')


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'trip', 'sender_role', 'is_system', 'created_at')
    list_filter = ('sender_role', 'is_system')
    search_fields = ('body', 'trip__id')
    readonly_fields = ('created_at', 'read_at')


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = (
        'code', 'discount_type', 'discount_value', 'min_fare',
        'valid_from', 'valid_to', 'redemption_count', 'max_total_redemptions',
        'is_active', 'zone',
    )
    list_filter = ('is_active', 'discount_type', 'zone')
    search_fields = ('code', 'description')


@admin.register(PromoRedemption)
class PromoRedemptionAdmin(admin.ModelAdmin):
    list_display = ('id', 'promo', 'user', 'trip', 'discount_amount', 'created_at')
    search_fields = ('promo__code', 'user__phone_number', 'trip__id')
