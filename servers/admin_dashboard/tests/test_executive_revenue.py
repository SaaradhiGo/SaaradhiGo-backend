from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from django.contrib.auth import get_user_model

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus


User = get_user_model()


class ExecutiveRevenueAdminViewTest(TestCase):
    def setUp(self):
        # create an admin user
        self.admin = User.objects.create_superuser(phone_number='+911234567890', email='a@a.com', password='pass')
        self.admin.role = 'admin'
        self.admin.save()

        # create a driver and related vehicle/type
        self.rider = User.objects.create_user(phone_number='+919876543210', email='r@r.com', password='pass', role='rider')

        driver_user = User.objects.create_user(phone_number='+919999999999', email='d@d.com', password='pass', role='driver')
        self.driver = Driver.objects.create(user_id=driver_user)

        self.vtype = VehicleType.objects.create(type='Sedan')
        self.vehicle = Vehicle.objects.create(driver_id=self.driver, vehicle_type_id=self.vtype, vehicle_number='TN01AB1234')

        # ensure a 'completed' TripStatus exists
        self.completed_status, _ = TripStatus.objects.get_or_create(status_code='completed')

        # a completed trip in the current timezone
        self.trip = Trip.objects.create(
            user_id=self.rider,
            driver_id=self.driver,
            vehicle_id=self.vehicle,
            requested_vehicle_type=self.vtype,
            status_id=self.completed_status,
            completed_at=timezone.now(),
            pickup_lat=12.0,
            pickup_long=77.0,
            destination_lat=12.1,
            destination_long=77.1,
            final_fare=Decimal('100.00'),
        )

    def test_executive_revenue_shows_gbv_and_platform_revenue(self):
        self.client.force_login(self.admin)
        url = reverse('executive_revenue')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        # template context should contain gbv and platform_revenue computed from final_fare
        ctx = resp.context
        self.assertIn('gbv', ctx)
        self.assertIn('platform_revenue', ctx)

        self.assertEqual(ctx['gbv'], Decimal('100.00'))
        expected_platform = (Decimal('100.00') * Decimal('0.15'))
        self.assertEqual(ctx['platform_revenue'], expected_platform)
