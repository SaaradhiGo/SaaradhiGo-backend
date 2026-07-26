import os
import sys
import django

# Setup Django environment
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'base.settings')
django.setup()

from servers.driver.models import Driver
from servers.rider.models import Wallet, WalletTransaction
from servers.driver.models import WithdrawalRequest
from servers.driver.services import trigger_payout_creation
from django.contrib.auth import get_user_model
from decimal import Decimal

User = get_user_model()

def run_rehearsal():
    print("Starting Cashfree Payout Rehearsal...")
    
    # 1. Create a test driver
    email = "test_driver_payout@example.com"
    user, _ = User.objects.get_or_create(
        email=email,
        defaults={
            'username': 'test_driver_payout',
            'phone_number': '9999999991',
            'role': 'driver',
            'full_name': 'Test Driver'
        }
    )
    
    driver, _ = Driver.objects.get_or_create(
        user_id=user,
        defaults={
            'upi_id': 'ankamsaiteja27-01@oksbi',
            'status': 'active'
        }
    )
    driver.upi_id = 'ankamsaiteja27-01@oksbi'
    driver.save()
    
    # 2. Add funds to Wallet
    wallet, _ = Wallet.objects.get_or_create(user_id=user)
    wallet.balance = Decimal('1000.00')
    wallet.save()
    
    # 3. Create WithdrawalRequest
    withdrawal = WithdrawalRequest.objects.create(
        driver=driver,
        amount=Decimal('500.00'),
        payout_method='upi',
        status='approved'
    )
    
    print(f"Created WithdrawalRequest ID: {withdrawal.id} for Amount: {withdrawal.amount}")
    
    # 4. Trigger Payout
    print("Triggering Payout Creation...")
    success = trigger_payout_creation(withdrawal)
    
    withdrawal.refresh_from_db()
    print(f"Success: {success}")
    print(f"Withdrawal Status: {withdrawal.status}")
    print(f"Payout Reference: {withdrawal.payout_reference_id}")
    print(f"Failure Reason: {withdrawal.failure_reason}")
    print(f"Payout Status: {withdrawal.payout_status}")

if __name__ == '__main__':
    run_rehearsal()
