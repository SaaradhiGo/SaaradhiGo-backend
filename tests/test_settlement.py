import pytest
from decimal import Decimal
from servers.ride.models import Trip, TripStatus
from servers.driver.models import Driver, DriverEarning
from servers.rider.models import Wallet
from servers.payments.models import TransactionHistory
from servers.driver.utils import create_driver_earning
from django.contrib.auth import get_user_model

User = get_user_model()

@pytest.mark.django_db
def test_driver_settlement_on_earning_creation():
    # 1. Setup Data
    rider_user = User.objects.create_user(phone_number="+911111111111", full_name="Rider")
    driver_user = User.objects.create_user(phone_number="+912222222222", full_name="Driver")
    driver = Driver.objects.create(user_id=driver_user, approved=True)
    
    status_completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    
    trip = Trip.objects.create(
        user_id=rider_user,
        driver_id=driver,
        pickup_lat=12.9716,
        pickup_long=77.5946,
        destination_lat=12.9352,
        destination_long=77.6245,
        estimated_fare=Decimal('100.00'),
        final_fare=Decimal('100.00'),
        status_id=status_completed,
        payment_method='online'
    )
    
    # 2. Call Settlement Logic
    create_driver_earning(trip)
    
    # 3. Verify Earnings
    earning = DriverEarning.objects.get(trip_id=trip)
    assert earning.net_amount == Decimal('80.00')  # 100 - 20% commission
    
    # 4. Verify Wallet Credit
    wallet = Wallet.objects.get(user_id=driver_user)
    assert wallet.balance == Decimal('80.00')
    
    # 5. Verify Transaction History
    txn = TransactionHistory.objects.get(trip_id=trip, txn_type='credit')
    assert txn.amount == Decimal('80.00')
    assert txn.driver_id == driver
    assert txn.status == 'completed'

@pytest.mark.django_db
def test_cash_payment_settlement():
    rider_user = User.objects.create_user(phone_number="+913333333333", full_name="Rider Cash")
    driver_user = User.objects.create_user(phone_number="+914444444444", full_name="Driver Cash")
    driver = Driver.objects.create(user_id=driver_user, approved=True)
    
    status_completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    
    trip = Trip.objects.create(
        user_id=rider_user,
        driver_id=driver,
        pickup_lat=12.9716,
        pickup_long=77.5946,
        destination_lat=12.9352,
        destination_long=77.6245,
        final_fare=Decimal('200.00'),
        status_id=status_completed,
        payment_method='cash'
    )
    
    create_driver_earning(trip)
    
    wallet = Wallet.objects.get(user_id=driver_user)
    assert wallet.balance == Decimal('160.00')
    
    txn = TransactionHistory.objects.get(trip_id=trip, txn_type='credit')
    assert txn.method == 'cash'
    assert txn.amount == Decimal('160.00')
