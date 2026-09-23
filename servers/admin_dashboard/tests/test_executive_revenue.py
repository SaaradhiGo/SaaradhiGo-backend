"""Executive revenue reporting must read commission from the rate card.

This suite was previously never executed: CI ran `pytest tests/`, so nothing
under `servers/**/tests/` was collected. Once discovery was fixed it failed
immediately, asserting `platform_revenue == fare * 0.15` against an
implementation that produced 18%.

The 15% was a stale literal, not a business rule. Tracing the source of
truth:

  * `pricing.services.commission_percent_for_trip` is canonical, and
    resolves in this order: the trip's zone+vehicle-type `RateCard`, then a
    database-backed `PlatformSettings` row, then
    `settings.PLATFORM_COMMISSION_PERCENT`, then a hardcoded 18%.
  * `settings.PLATFORM_COMMISSION_PERCENT` defaults to `Decimal("18")`.
  * ADR-0006 records the real per-zone business rates: 18% auto/hatchback,
    20% sedan/SUV, seeded in `pricing/migrations/0004_seed_vja_wgl_vtz.py`.
  * `admin_dashboard.views.executive_revenue` already calls
    `commission_percent_for_trip` per trip. It hardcodes nothing.
  * 15% appears in no ADR, migration, model, setting or rate card.

So the implementation was right and the test was wrong. The fix is NOT to
swap 15 for 18 — that would replace one magic number with another, and
would break again the moment an operator changes the fallback or adds a
`PlatformSettings` row. Instead:

  * `test_platform_revenue_matches_canonical_commission_source` asserts the
    report agrees with `commission_percent_for_trip`, whatever that resolves
    to. It pins the invariant that actually matters: reporting must not
    invent its own rate.
  * `test_platform_revenue_follows_the_zone_rate_card` seeds a rate card
    with a deliberately unusual 11% and asserts the report follows it. That
    is the test that fails if anyone reintroduces a hardcoded percentage —
    a literal 15%, 18% or 20% all break it.

The driver-facing side of the same problem -- `driver_earnings_summary`
hardcoding `commission_percent = 20` and reporting `'commission': 0.0` -- has
since been fixed on the driver-earnings branch and is covered by
`tests/test_driver_earnings.py`. Nothing in this file touches settlement or
pricing behaviour.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.pricing.models import RateCard, ServiceZone
from servers.pricing.services import commission_percent_for_trip
from servers.ride.models import Trip, TripStatus

User = get_user_model()

FARE = Decimal('100.00')

# A rate that matches none of the values anyone has hardcoded (15, 18, 20)
# and none of the seeded zone cards, so the assertion can only pass if the
# report genuinely read this card.
SENTINEL_COMMISSION = Decimal('11.00')


def _square_around(lat, lon, pad=Decimal('0.1')):
    """GeoJSON polygon enclosing (lat, lon). Coordinates are (lon, lat)."""
    lat, lon, pad = Decimal(str(lat)), Decimal(str(lon)), Decimal(str(pad))
    west, east = float(lon - pad), float(lon + pad)
    south, north = float(lat - pad), float(lat + pad)
    return {
        'type': 'Polygon',
        'coordinates': [[
            [west, south], [east, south], [east, north], [west, north], [west, south],
        ]],
    }


class ExecutiveRevenueAdminViewTest(TestCase):
    PICKUP_LAT = Decimal('12.0')
    PICKUP_LON = Decimal('77.0')

    def setUp(self):
        self.admin = User.objects.create_superuser(
            phone_number='+911234567890', email='a@a.com', password='pass',
        )
        self.admin.role = 'admin'
        self.admin.save()

        self.rider = User.objects.create_user(
            phone_number='+919876543210', email='r@r.com', password='pass', role='rider',
        )
        driver_user = User.objects.create_user(
            phone_number='+919999999999', email='d@d.com', password='pass', role='driver',
        )
        self.driver = Driver.objects.create(user_id=driver_user)

        self.vtype = VehicleType.objects.create(type='Sedan')
        self.vehicle = Vehicle.objects.create(
            driver_id=self.driver, vehicle_type_id=self.vtype, vehicle_number='TN01AB1234',
        )
        self.completed_status, _ = TripStatus.objects.get_or_create(status_code='completed')

        self.trip = Trip.objects.create(
            user_id=self.rider,
            driver_id=self.driver,
            vehicle_id=self.vehicle,
            requested_vehicle_type=self.vtype,
            status_id=self.completed_status,
            completed_at=timezone.now(),
            pickup_lat=self.PICKUP_LAT,
            pickup_long=self.PICKUP_LON,
            destination_lat=Decimal('12.1'),
            destination_long=Decimal('77.1'),
            final_fare=FARE,
        )

    def _get_report(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('executive_revenue'))
        self.assertEqual(response.status_code, 200)
        return response.context

    def _expected_commission(self, rate):
        return (FARE * rate / Decimal('100')).quantize(Decimal('0.01'))

    def test_report_is_admin_only(self):
        """Anonymous callers must not reach the revenue report.

        Guarded by @admin_required as of the P0 authorization fix; the
        exhaustive route sweep lives in tests/test_admin_dashboard_authz.py.
        """
        self.client.logout()
        response = self.client.get(reverse('executive_revenue'))
        self.assertIn(response.status_code, {301, 302, 401, 403})

    def test_report_exposes_gbv_and_platform_revenue(self):
        ctx = self._get_report()
        self.assertIn('gbv', ctx)
        self.assertIn('platform_revenue', ctx)
        self.assertEqual(ctx['gbv'], FARE)

    def test_platform_revenue_matches_canonical_commission_source(self):
        """Reporting must agree with `commission_percent_for_trip`.

        No literal rate here on purpose: whatever the canonical service
        resolves for this trip is what the report must show. With no zone
        and no PlatformSettings row this falls through to
        settings.PLATFORM_COMMISSION_PERCENT (18), but the assertion holds
        if an operator changes that.
        """
        rate = commission_percent_for_trip(self.trip)
        ctx = self._get_report()
        self.assertEqual(ctx['platform_revenue'], self._expected_commission(rate))

    def test_platform_revenue_follows_the_zone_rate_card(self):
        """A zone rate card must drive reported revenue.

        This is the regression guard: it fails if the reporting layer ever
        reintroduces a hardcoded percentage, because SENTINEL_COMMISSION
        matches no value anyone has hardcoded and no seeded card.
        """
        zone = ServiceZone.objects.create(
            code='IN-TG-TEST-REVENUE',
            name='Executive revenue test zone',
            state_code='TG',
            city='Testville',
            zone_type='city',
            polygon_geojson=_square_around(self.PICKUP_LAT, self.PICKUP_LON),
            priority=10,
        )
        RateCard.objects.create(
            zone=zone,
            vehicle_type=self.vtype,
            base_fare=Decimal('20.00'),
            per_km_fare=Decimal('10.00'),
            per_min_fare=Decimal('1.00'),
            min_fare=Decimal('30.00'),
            commission_percent=SENTINEL_COMMISSION,
            gst_percent=Decimal('5.00'),
            # Comfortably before `requested_at`, which auto_now_add stamped
            # during setUp; an `effective_from` of "now" can land after it.
            effective_from=timezone.now() - timezone.timedelta(days=1),
            is_active=True,
        )
        self.trip.zone = zone
        self.trip.save(update_fields=['zone'])

        # Confirm the canonical service sees the card, so a failure below
        # points at the report rather than at this fixture.
        self.assertEqual(commission_percent_for_trip(self.trip), SENTINEL_COMMISSION)

        ctx = self._get_report()
        self.assertEqual(
            ctx['platform_revenue'], self._expected_commission(SENTINEL_COMMISSION),
            'executive_revenue must read commission from the zone rate card, '
            'not from a hardcoded percentage.',
        )
