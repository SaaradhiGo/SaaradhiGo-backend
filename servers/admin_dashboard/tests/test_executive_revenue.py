"""Executive revenue reporting must read commission from the commission rules.

The commission is the layered, versioned `CommissionRule` model (module 8, AUDIT
27.36): a trip's own snapshot, else the most specific rule effective when it was
requested. `pricing.services.commission_percent_for_trip` is canonical and the
report calls it per trip; it hardcodes nothing. (The rate card's old
`commission_percent` column, the PlatformSettings key, the environment setting and
the literal 18 are no longer read.)

  * `test_platform_revenue_matches_canonical_commission_source` asserts the report
    agrees with `commission_percent_for_trip`, whatever that resolves to: reporting
    must not invent its own rate.
  * `test_platform_revenue_follows_the_commission_rule` creates a rule with a
    deliberately unusual 11% and asserts the report follows it, so a hardcoded
    15%, 18% or 20% would break it.
  * A trip in a market with no rule is left out of the revenue figure, not guessed.

Nothing in this file touches settlement or pricing behaviour.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.pricing.commission_models import ACTIVE, CommissionRule
from servers.pricing.models import ServiceZone
from servers.pricing.services import commission_percent_for_trip
from servers.ride.models import Trip, TripStatus

User = get_user_model()

FARE = Decimal('100.00')

# A rate that matches none of the values anyone has hardcoded (15, 18, 20), so the
# assertion can only pass if the report genuinely read this rule.
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


# The report is a frozen-console screen: 404 unless the deployment allowlists
# it and the caller is a Super Admin (the superuser below qualifies). Opened
# here so the commission invariant is still checked wherever it is served.
@override_settings(FROZEN_CONSOLE_ALLOWED_ROUTES=['executive_revenue'])
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

        self.zone = ServiceZone.objects.create(
            code='IN-TG-TEST-REVENUE', name='Executive revenue test zone', country='IN',
            state_code='TG', city='Testville', zone_type='city',
            polygon_geojson=_square_around(self.PICKUP_LAT, self.PICKUP_LON), priority=10,
        )
        CommissionRule.objects.create(
            country='IN', percent=Decimal('15.00'), status=ACTIVE,
            effective_from=timezone.now() - timezone.timedelta(days=30), reason='test country rule')

        self.trip = Trip.objects.create(
            zone=self.zone,
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
        # 404: the frozen console's gate hides the route from anyone who is
        # not a Super Admin (servers/admin_dashboard/lockdown.py).
        self.assertIn(response.status_code, {301, 302, 401, 403, 404})

    def test_report_exposes_gbv_and_platform_revenue(self):
        ctx = self._get_report()
        self.assertIn('gbv', ctx)
        self.assertIn('platform_revenue', ctx)
        self.assertEqual(ctx['gbv'], FARE)

    def test_platform_revenue_matches_canonical_commission_source(self):
        """Reporting must agree with `commission_percent_for_trip`.

        No literal rate here on purpose: whatever the canonical service resolves for
        this trip is what the report must show.
        """
        rate = commission_percent_for_trip(self.trip)
        ctx = self._get_report()
        self.assertEqual(ctx['platform_revenue'], self._expected_commission(rate))

    def test_platform_revenue_follows_the_commission_rule(self):
        """A commission rule must drive reported revenue.

        The regression guard: it fails if the reporting layer ever reintroduces a
        hardcoded percentage, because SENTINEL_COMMISSION matches no value anyone has
        hardcoded.
        """
        CommissionRule.objects.create(
            country='IN', vehicle_type=self.vtype, percent=SENTINEL_COMMISSION, status=ACTIVE,
            effective_from=timezone.now() - timezone.timedelta(days=1), reason='sentinel')
        # The trip has no snapshot, so it resolves the rule effective at its request time.
        self.assertEqual(commission_percent_for_trip(self.trip), SENTINEL_COMMISSION)

        ctx = self._get_report()
        self.assertEqual(
            ctx['platform_revenue'], self._expected_commission(SENTINEL_COMMISSION),
            'executive_revenue must read commission from the commission rules, '
            'not from a hardcoded percentage.',
        )
