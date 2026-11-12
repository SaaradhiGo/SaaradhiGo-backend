from rest_framework import serializers
from base.serializer_fields import MoneyField
from servers.ride.models import Trip, FarePricing, Rating
from servers.driver.models import Driver, Vehicle


class FarePricingSerializer(serializers.ModelSerializer):
    base_fare = MoneyField()
    distance_fare = MoneyField()
    time_fare = MoneyField()
    total_fare = MoneyField()

    class Meta:
        model = FarePricing
        fields = ['base_fare', 'distance_fare', 'time_fare', 'surge_multiplier', 'total_fare']


class RatingSerializer(serializers.ModelSerializer):
    rater_name = serializers.SerializerMethodField()

    class Meta:
        model = Rating
        fields = ['id', 'rater_id', 'rater_name', 'score', 'comments', 'created_at']

    def get_rater_name(self, obj):
        return obj.rater_id.full_name or obj.rater_id.phone_number


class TripListSerializer(serializers.ModelSerializer):
    """Compact serializer for trip lists (ride history)."""
    status = serializers.SerializerMethodField()
    driver_name = serializers.SerializerMethodField()
    vehicle_info = serializers.SerializerMethodField()
    requested_vehicle_type = serializers.SerializerMethodField()
    user=serializers.SerializerMethodField()
    #: The IANA time zone of the trip's market (its zone), so every client shows the trip's
    #: times as they were where it happened (2026-11-08). Read-only.
    timezone = serializers.SerializerMethodField()
    #: Why the SYSTEM cancelled (e.g. `no_driver_accepted`) and the sentence to
    #: show, so a client that missed the socket frame can still say "No driver
    #: accepted" rather than "Ride cancelled" (2026-11-08, trip 191). Additive;
    #: None for rider, driver and ops cancels, whose notes are not shared.
    cancellation_reason = serializers.SerializerMethodField()
    cancellation_message = serializers.SerializerMethodField()
    estimated_fare = MoneyField(read_only=True, allow_null=True)
    final_fare = MoneyField(read_only=True, allow_null=True)

    class Meta:
        model = Trip
        fields = [
            'id', 'pickup_address', 'destination_address',
            'pickup_lat', 'pickup_long', 'destination_lat', 'destination_long',
            # THE CURRENCY OF THESE FARES. It is stamped on the trip at
            # write time; without it every client renders a bare number and
            # picks a symbol for it, which is wrong the moment a second
            # market exists.
            'currency',
            'estimated_fare', 'final_fare', 'surge_multiplier',
            'estimated_distance_km', 'actual_distance_km',
            'payment_method', 'payment_status',
            'status', 'driver_name', 'vehicle_info', 'requested_vehicle_type',
            'requested_at', 'completed_at', 'cancelled_at','estimated_duration_min','actual_duration_min','user',
            # Who cancelled: rider, driver, system or admin ('' while not cancelled).
            'cancelled_by',
            'cancellation_reason', 'cancellation_message',
            'timezone',
        ]

    def get_cancellation_reason(self, obj):
        if obj.cancelled_by == 'system' and obj.cancellation_reason:
            return obj.cancellation_reason
        return None

    def get_cancellation_message(self, obj):
        if self.get_cancellation_reason(obj) == 'no_driver_accepted':
            from servers.ride.tasks import NO_DRIVER_MESSAGE
            return NO_DRIVER_MESSAGE
        return None

    def get_timezone(self, obj):
        from base.market_time import trip_timezone
        tz = trip_timezone(obj)
        return getattr(tz, 'key', None) or str(tz)

    def get_status(self, obj):
        return obj.status_id.status_code if obj.status_id else 'pending'

    def get_driver_name(self, obj):
        if obj.driver_id:
            return obj.driver_id.public_name
        return None

    def get_vehicle_info(self, obj):
        if obj.vehicle_id:
            v = obj.vehicle_id
            return {
                'vehicle_number': v.vehicle_number,
                'brand': v.brand,
                'model': v.model,
                'color': v.color,
                'type': str(v.vehicle_type_id) if v.vehicle_type_id else None,
            }
        return None
    def get_requested_vehicle_type(self, obj):
        """The category the RIDER chose, which exists from the moment they book.

        `vehicle_info` describes the car that turned up, so it is null until a
        driver accepts -- and null forever on a trip nobody accepted. A rider
        reopening their own ride needs to see what they asked for, which is
        this. The admin dashboard already reads this field; the rider's own
        history did not expose it.
        """
        vt = obj.requested_vehicle_type
        return str(vt) if vt else None

    def get_user(self,obj):
        return obj.user_id.full_name

class TripDetailSerializer(TripListSerializer):
    """Full serializer for single trip detail with fare breakdown and ratings."""
    fare_breakdown = serializers.SerializerMethodField()
    ratings = RatingSerializer(many=True, read_only=True)
    # OTP is the rider's secret to read aloud to the driver at pickup. The
    # driver enters whatever the rider says; they must NEVER see the OTP
    # via the API. SerializerMethodField + request-context check enforces
    # that: only the trip's own rider gets the OTP value back.
    otp = serializers.SerializerMethodField()
    # Promotions (module 12), additive. `estimated_fare` / `final_fare` stay the FULL fare (the
    # driver's earnings and commission are computed on it). A promo is platform-funded, so these two
    # say what the rider is charged: the cash a driver collects on a cash trip is `rider_payable`.
    promo_discount = serializers.SerializerMethodField()
    rider_payable = serializers.SerializerMethodField()
    # The driver card on the rider's "driver arriving" screen (2026-11-06). Only the trip's own
    # rider (or its driver) gets them; anyone else sees None.
    driver_photo_url = serializers.SerializerMethodField()
    driver_total_trips = serializers.SerializerMethodField()
    # What the trip's DRIVER keeps (2026-11-06), for their on-trip sheet: the settlement's own
    # arithmetic (`settlement_amounts`), never a second calculation. None for anyone else, and
    # None when no commission rule governs the trip.
    driver_earning = serializers.SerializerMethodField()
    # 11 Nov (rider QA fixes): the receipt screen is a tax invoice, the same as the PDF, and the
    # finding screen counts down to the city's search time.
    invoice = serializers.SerializerMethodField()
    search_expires_at = serializers.SerializerMethodField()
    vehicle_line = serializers.SerializerMethodField()

    def get_invoice(self, obj):
        request = self.context.get('request') if hasattr(self, 'context') else None
        user = getattr(request, 'user', None)
        if user is None or not (getattr(user, 'id', None) == obj.user_id_id or getattr(user, 'is_staff', False)):
            return None
        from servers.ride.models import Receipt
        from servers.ride.receipts import _rider_name, company_lines, receipt_invoice
        receipt = Receipt.objects.filter(trip_id=obj).order_by('-version', '-id').first()
        if receipt is None:
            return None
        from base.countries import get_country
        return {
            'number': receipt.receipt_number,
            'issued_at': receipt.issued_at.isoformat() if receipt.issued_at else None,
            'rider_name': _rider_name(obj.user_id),
            'company': company_lines(),
            'tax_label': get_country(getattr(obj.zone, 'country', None)).tax_label,
            'tax_amount': str(receipt.gst_amount),
            'total': str(receipt.total_fare),
            'currency': receipt.currency,
            # 12 Nov: the lines before tax, the taxable value, the tax and the total, adding up
            # to the paisa: the same lines as the PDF (servers/ride/invoice_lines.py).
            'breakdown': receipt_invoice(obj, total=receipt.total_fare, gst_amount=receipt.gst_amount,
                                         currency=receipt.currency),
        }

    def get_search_expires_at(self, obj):
        if obj.driver_id_id or not obj.requested_at:
            return None
        from datetime import timedelta
        from servers.ride.rider_booking import search_timeout_for_trip
        return (obj.requested_at + timedelta(seconds=search_timeout_for_trip(obj))).isoformat()

    def get_vehicle_line(self, obj):
        from servers.ride.receipts import vehicle_line
        return vehicle_line(obj.vehicle_id) or None

    class Meta(TripListSerializer.Meta):
        fields = TripListSerializer.Meta.fields + [
            'accepted_at', 'started_at',
            'fare_breakdown', 'ratings', 'otp', 'promo_discount', 'rider_payable',
            # The apps draw this instead of buying a route per location ping (Maps M1).
            'planned_route_polyline',
            'driver_photo_url', 'driver_total_trips', 'driver_earning',
            'invoice', 'search_expires_at', 'vehicle_line',
        ]

    def _party(self, obj):
        request = self.context.get('request') if hasattr(self, 'context') else None
        uid = getattr(getattr(request, 'user', None), 'id', None)
        if uid is None or not obj.driver_id:
            return False
        return uid in (obj.user_id_id, obj.driver_id.user_id_id)

    def get_driver_photo_url(self, obj):
        # A short-lived signed URL to the driver's own photo, or None (no photo, or the store
        # cannot sign). Never a permanent link.
        if not self._party(obj):
            return None
        from servers.driver.document_access import signed_url
        return signed_url(obj.driver_id.user_id.avatar)

    def get_driver_earning(self, obj):
        request = self.context.get('request') if hasattr(self, 'context') else None
        uid = getattr(getattr(request, 'user', None), 'id', None)
        if uid is None or not obj.driver_id or uid != obj.driver_id.user_id_id:
            return None
        from base.money import currency_for_trip, from_minor
        from servers.driver.utils import settlement_amounts
        from servers.pricing.commission import CommissionNotConfigured

        try:
            amounts = settlement_amounts(obj)
        except CommissionNotConfigured:
            return None
        return str(from_minor(amounts['net_minor'], currency_for_trip(obj)))

    def get_driver_total_trips(self, obj):
        # Completed trips, incremented when a trip completes (consumers.py).
        if not self._party(obj):
            return None
        return obj.driver_id.total_trips

    def get_promo_discount(self, obj):
        from servers.promotions import rider_promos

        minor = rider_promos.discount_minor(obj)
        return str(rider_promos.dec(minor, obj.currency)) if minor else None

    def get_rider_payable(self, obj):
        from servers.promotions import rider_promos

        if not rider_promos.discount_minor(obj):
            return None
        return str(rider_promos.payable(obj))

    def get_fare_breakdown(self, obj):
        # Uses prefetched items rather than hitting the DB
        pricing_list = obj.fare_pricing.all()
        if pricing_list:
            return FarePricingSerializer(pricing_list[0]).data
        return None

    def get_otp(self, obj):
        request = self.context.get('request') if hasattr(self, 'context') else None
        if not request or not getattr(request, 'user', None):
            return None
        # Only the rider on this trip gets to see the OTP.
        if getattr(request.user, 'id', None) == getattr(obj, 'user_id_id', None):
            return obj.otp
        return None
