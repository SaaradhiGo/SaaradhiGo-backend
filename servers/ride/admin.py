from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError

from .models import (
    DRIVER_ACTIVE_TRIP_STATUSES, TripStatus, Trip, FarePricing,
    VehicleFarePricing, Rating, Receipt, ChatMessage, PromoCode,
    PromoRedemption, driver_active_trip_ids,
)

admin.site.register(TripStatus)


class TripAdminForm(forms.ModelForm):
    """Blocks manual double-assignment of a driver from Django admin.

    `Trip` was registered bare (`admin.site.register(Trip)`), which gave
    superusers a fully editable form — including `driver_id` and `status_id` —
    with no lock and no validation. That is a second driver-assignment path
    alongside `_accept_trip`, and an application-level check inside
    `_accept_trip` cannot see it.

    Validation rather than read-only fields on purpose: ops occasionally needs
    to repair a trip by hand, and making the lifecycle fields read-only would
    remove that without replacing it. This refuses only the specific unsafe
    edit and leaves every legitimate one alone.

    Defence-in-depth only. The durable fix is the database constraint in
    Stage 2; this form runs no lock, so it cannot win a race against a
    concurrent acceptance — it exists to stop an accidental manual edit.
    """

    class Meta:
        model = Trip
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        driver = cleaned.get('driver_id')
        status = cleaned.get('status_id')
        if driver is None or status is None:
            return cleaned

        if status.status_code not in DRIVER_ACTIVE_TRIP_STATUSES:
            # Assigning a driver to a completed/cancelled/requested trip does
            # not occupy them, so there is nothing to conflict with.
            return cleaned

        conflicting = driver_active_trip_ids(driver, exclude_trip_id=self.instance.pk)
        if conflicting:
            raise ValidationError({
                'driver_id': (
                    f'Driver is already on active trip(s) {conflicting}. '
                    'A driver may hold at most one active trip — finish or '
                    'cancel the other trip first.'
                ),
            })
        return cleaned


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    form = TripAdminForm
    list_display = ('id', 'user_id', 'driver_id', 'status_id', 'requested_at', 'completed_at')
    list_filter = ('status_id',)
    search_fields = ('id', 'user_id__phone_number')
    # The OTP is the rider's secret, read aloud to the driver at pickup. It has
    # no business being typed into an admin form.
    readonly_fields = ('otp',)
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
