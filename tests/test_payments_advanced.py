import pytest
from unittest.mock import patch, MagicMock
from servers.ride.models import Trip, TripStatus
from servers.payments.models import Payment
from django.conf import settings

@pytest.mark.django_db
class TestCreateOrderAdvanced:
    
    @pytest.fixture
    def setup_trip(self, auth_client_rider):
        client, user = auth_client_rider
        from servers.rider.models import Rider
        from servers.driver.models import Driver
        from django.contrib.auth import get_user_model
        User = get_user_model()
        
        rider = Rider.objects.get(user_id=user)
        
        # Create a driver
        d_user = User.objects.create_user(phone_number="+91888111222", role="driver")
        driver = Driver.objects.create(user_id=d_user)
        
        # Create a trip
        status_completed, _ = TripStatus.objects.get_or_create(status_code='completed')
        trip = Trip.objects.create(
            user_id=user, # Rider user
            driver_id=driver,
            pickup_lat=28.6139,
            pickup_long=77.209,
            destination_lat=28.7041,
            destination_long=77.1025,
            estimated_fare=150.0,
            status_id=status_completed
        )
        return client, user, trip

    @patch('servers.payments.views.create_razorpay_order')
    def test_create_order_success(self, mock_create_rzp, setup_trip):
        client, user, trip = setup_trip
        
        # Mock Razorpay response
        mock_create_rzp.return_value = {
            'id': 'order_test_123',
            'amount': 15000,
            'currency': 'INR',
            'key_id': 'rzp_test_key' # Even if it's there, we check our fix
        }
        
        url = "/api/v1/payments/create-order/"
        payload = {"trip_id": trip.id}
        
        response = client.post(url, data=payload, format='json')
        
        if response.status_code != 201:
            print(f"FAILED DATA: {response.data}")
        
        assert response.status_code == 201
        data = response.json().get("data")
        assert data['razorpay_order_id'] == 'order_test_123'
        assert data['razorpay_key_id'] == getattr(settings, 'RAZORPAY_KEY_ID', '')
        assert data['amount'] == '150.00'
        assert data['amount_paise'] == 15000
        
        # Verify Payment record created
        payment = Payment.objects.get(trip_id=trip)
        assert payment.status == 'processing'
        assert payment.razorpay_order_id == 'order_test_123'

    def test_create_order_unauthorized(self, setup_trip, auth_client_driver):
        client_rider, user_rider, trip = setup_trip
        client_driver, user_driver = auth_client_driver # Different user
        
        url = "/api/v1/payments/create-order/"
        payload = {"trip_id": trip.id}
        
        # Driver tries to pay for Rider's trip
        response = client_driver.post(url, data=payload, format='json')
        
        assert response.status_code == 403
        assert response.json()['error']['code'] == 'FORBIDDEN'

    def test_create_order_invalid_status(self, setup_trip):
        client, user, trip = setup_trip
        
        # Change trip status to 'requested'
        status_req, _ = TripStatus.objects.get_or_create(status_code='requested')
        trip.status_id = status_req
        trip.save()
        
        url = "/api/v1/payments/create-order/"
        payload = {"trip_id": trip.id}
        
        response = client.post(url, data=payload, format='json')
        
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'INVALID_STATE'

    def test_create_order_already_paid(self, setup_trip):
        client, user, trip = setup_trip
        
        # Create an already completed payment
        Payment.objects.create(
            trip_id=trip,
            user_id=user,
            amount=150.0,
            method='online',
            status='completed',
            razorpay_order_id='order_old'
        )
        
        url = "/api/v1/payments/create-order/"
        payload = {"trip_id": trip.id}
        
        response = client.post(url, data=payload, format='json')
        
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'ALREADY_PAID'

    @patch('servers.payments.views.create_razorpay_order')
    def test_create_order_gateway_error(self, mock_create_rzp, setup_trip):
        client, user, trip = setup_trip
        
        # Mock Razorpay failure
        mock_create_rzp.return_value = None
        
        url = "/api/v1/payments/create-order/"
        payload = {"trip_id": trip.id}
        
        response = client.post(url, data=payload, format='json')
        
        assert response.status_code == 502
        assert response.json()['error']['code'] == 'PAYMENT_GATEWAY_ERROR'
