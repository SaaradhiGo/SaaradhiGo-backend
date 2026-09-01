# Complete Revenue Split Logic: Driver & Company from Rider Fees

## Overview
When a rider pays for a trip, the fare is split between the **driver** and **company (SaaradhiGo platform)** based on a configurable commission percentage. This document explains the exact code flow.

---

## 1. DATA MODELS INVOLVED

### 1.1 Trip Model - [servers/ride/models.py]
```python
class Trip(models.Model):
    user_id = models.ForeignKey(User, on_delete=models.CASCADE, related_name='trips')
    driver_id = models.ForeignKey(Driver, on_delete=models.DO_NOTHING, related_name='trips')
    
    # Fare amounts
    estimated_fare = models.DecimalField(max_digits=10, decimal_places=2)
    final_fare = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Payment information
    payment_method = models.CharField(max_length=50)  # 'cash', 'online', 'wallet'
    payment_status = models.CharField(max_length=50)
    
    # Zone for rate card lookup
    zone = models.ForeignKey('pricing.ServiceZone', on_delete=models.SET_NULL, null=True)
    
    # Requested vehicle type determines which rate card to use
    requested_vehicle_type = models.ForeignKey(VehicleType, on_delete=models.SET_NULL)
    
    requested_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)
```

### 1.2 RateCard Model - [servers/pricing/models.py]
```python
class RateCard(models.Model):
    """Versioned, effective-dated fare schedule per (zone, vehicle_type)"""
    zone = models.ForeignKey(ServiceZone, on_delete=models.PROTECT)
    vehicle_type = models.ForeignKey(VehicleType, on_delete=models.PROTECT)
    
    # Fare structure
    base_fare = models.DecimalField(max_digits=10, decimal_places=2)
    per_km_fare = models.DecimalField(max_digits=10, decimal_places=2)
    per_min_fare = models.DecimalField(max_digits=10, decimal_places=2)
    min_fare = models.DecimalField(max_digits=10, decimal_places=2)
    
    # ===== CRITICAL FOR REVENUE SPLIT =====
    # Driver commission percentage taken by SaaradhiGo
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('18.00')
    )
    # =====================================
    
    gst_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('5.00')
    )
    
    # Versioning and effective dates
    effective_from = models.DateTimeField(default=timezone.now)
    effective_to = models.DateTimeField(null=True, blank=True)
    version = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)
```

### 1.3 Wallet Model - [servers/rider/models.py]
```python
class Wallet(models.Model):
    """A balance held for one user in one role"""
    SCOPE_RIDER = 'rider'      # Rider credit balance
    SCOPE_DRIVER = 'driver'    # Driver settlement balance
    
    user_id = models.ForeignKey(User, on_delete=models.CASCADE)
    scope = models.CharField(max_length=16, choices=SCOPE_CHOICES)
    
    # Driver wallet balance (settlement account)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
```

### 1.4 WalletTransaction Model - [servers/rider/models.py]
```python
class WalletTransaction(models.Model):
    """Records of wallet balance changes"""
    user_id = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    txn_type = models.CharField(max_length=20)  # 'credit' or 'debit'
    status = models.CharField(max_length=20)    # 'pending', 'completed', 'failed'
    purpose = models.CharField(max_length=100)  # 'trip_earnings', 'trip_commission', etc
    reference_id = models.CharField(max_length=256)  # e.g., 'TRIP_12345'
    idempotency_key = models.CharField(max_length=256, unique=True)  # Prevents duplicates
```

---

## 2. COMMISSION CALCULATION FLOW

### 2.1 Get Commission Percentage - [servers/pricing/services.py]

```python
def commission_percent_for_trip(trip) -> Decimal:
    """
    Platform commission % for a trip.
    
    Priority:
      1. RateCard.commission_percent for trip's zone + vehicle type
      2. settings.PLATFORM_COMMISSION_PERCENT (fallback)
      3. Default: 18%
    """
    from django.conf import settings
    
    try:
        # Get the RateCard effective at trip request time
        card = rate_card_for_trip(trip=trip)
        if card is not None and card.commission_percent is not None:
            return Decimal(str(card.commission_percent))
    except Exception as exc:
        logger.warning('commission lookup failed for trip %s: %s', 
                      getattr(trip, 'id', '?'), exc)
    
    # Fallback to settings
    fallback = getattr(settings, 'PLATFORM_COMMISSION_PERCENT', Decimal('18'))
    try:
        return Decimal(str(fallback))
    except (ValueError, ArithmeticError):
        return Decimal('18')
```

### 2.2 Resolve Which RateCard to Use - [servers/pricing/services.py]

```python
def rate_card_for_trip(at=None, trip=None):
    """
    Resolve the RateCard that governs a trip.
    
    Uses the zone stamped on the trip at booking time,
    then looks up the rate card for that zone + vehicle type
    that was effective at trip request time.
    """
    if trip is None:
        return None
    
    # Get zone (preferably from trip.zone, fallback to location lookup)
    zone = getattr(trip, 'zone', None)
    if zone is None:
        zone = find_zone_for_point(trip.pickup_lat, trip.pickup_long)
    
    if zone is None:
        return None
    
    # Get vehicle type
    vt = trip.requested_vehicle_type
    if vt is None:
        vehicle = getattr(trip, 'vehicle_id', None)
        vt = getattr(vehicle, 'vehicle_type_id', None)
    
    if vt is None:
        return None
    
    # Find active rate card for this zone + vehicle_type
    # Use trip.requested_at as the effective time
    return get_active_rate_card(zone, vt, at=at or trip.requested_at)


def get_active_rate_card(zone: ServiceZone, vehicle_type, at=None):
    """
    Get the active RateCard for (zone, vehicle_type) at time `at`.
    
    If zone is a sub-zone (e.g., AIRPORT), walks UP the parent chain
    to find a rate card. This allows city-level cards to serve as 
    defaults for sub-zones.
    """
    at = at or timezone.now()
    
    cursor: Optional[ServiceZone] = zone
    seen: set[int] = set()
    
    while cursor is not None and cursor.id not in seen:
        seen.add(cursor.id)
        
        # Find the most recent active rate card
        card = (
            RateCard.objects
            .filter(
                zone=cursor,
                vehicle_type=vehicle_type,
                is_active=True,
                effective_from__lte=at,  # Card became effective by now
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=at))  # Still effective
            .order_by('-effective_from', '-version')
            .first()
        )
        
        if card is not None:
            return card
        
        # Try parent zone (walk up hierarchy)
        cursor = cursor.parent
    
    return None
```

### 2.3 Default Commission Percentages

From migrations and data:

| Vehicle Type | Zone      | Commission % |
|--------------|-----------|--------------|
| Auto         | Any       | 18.00%       |
| Premium      | Any       | 18.00%       |
| Bike         | Any       | 18.00%       |
| Sedan        | Any       | 20.00%       |
| SUV          | Any       | 20.00%       |

---

## 3. REVENUE SPLIT CALCULATION

### 3.1 Mathematical Formula

```
Total Fare = final_fare (what rider pays)

Commission Amount = Total Fare × Commission % ÷ 100
Driver Earning = Total Fare - Commission Amount
Company Revenue = Commission Amount
```

### 3.2 Example Calculation

**Scenario: Auto ride in Hyderabad (18% commission)**

```
Rider pays: ₹200.00

Step 1: Get commission %
commission_rate = 18.00%

Step 2: Calculate commission
commission = 200.00 × 18.00 ÷ 100 = 36.00
commission = ₹36.00 (rounded to 2 decimals using ROUND_HALF_UP)

Step 3: Calculate driver earning
driver_earning = 200.00 - 36.00 = 164.00
driver_earning = ₹164.00

Step 4: Split breakdown
├─ Driver gets: ₹164.00 (82%)
└─ Company gets: ₹36.00 (18%)
```

---

## 4. DRIVER WALLET SETTLEMENT FLOW

### 4.1 Main Settlement Function - [servers/driver/utils.py]

This is where the actual revenue split happens and driver wallet is credited:

```python
def credit_driver_wallet(trip):
    """Settle a completed trip against the driver's settlement balance."""
    from servers.payments.models import TransactionHistory
    from servers.rider.models import Wallet, WalletTransaction, get_wallet
    from servers.pricing.services import commission_percent_for_trip
    from decimal import Decimal, ROUND_HALF_UP
    from django.db import transaction, IntegrityError

    if not trip.driver_id:
        return

    # STEP 1: Get the fare amount
    # Prefer final_fare (actual), fallback to estimated_fare
    amount = trip.final_fare or trip.estimated_fare or Decimal('0.00')
    
    # STEP 2: Get commission rate for this trip's zone + vehicle type
    # This looks up the RateCard effective at trip request time
    commission_rate = commission_percent_for_trip(trip)
    
    # STEP 3: Calculate commission amount
    # Quantize to 2 decimals using banker's rounding
    commission = (amount * commission_rate / Decimal('100')).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP,
    )
    
    # STEP 4: Calculate net driver earning (fare - commission)
    net_amount = amount - commission

    # STEP 5: Use transaction context to ensure atomicity
    with transaction.atomic():
        # Get driver's settlement wallet with SELECT FOR UPDATE lock
        # This prevents race conditions with concurrent settlements
        wallet = get_wallet(trip.driver_id.user_id, Wallet.SCOPE_DRIVER, lock=True)
        current_balance = Decimal(str(wallet.balance))
        
        # STEP 6: Different handling for CASH vs ONLINE payments
        if trip.payment_method == 'cash':
            # ===== CASH TRIPS =====
            # Driver collected full ₹200 in cash from rider
            # But owes the platform ₹36 commission
            # Net settlement to driver wallet: -₹36 (debit)
            
            txn_amount = commission          # Amount to debit
            txn_type = 'debit'               # It's a debit from wallet
            purpose = 'trip_commission'      # Purpose code
            new_balance = current_balance - commission  # Reduce wallet
            
            # Example:
            # current_balance = 500.00
            # commission = 36.00
            # new_balance = 500.00 - 36.00 = 464.00
            # Driver owes platform 36.00
        
        else:
            # ===== ONLINE/WALLET PAYMENTS =====
            # Platform collected ₹200 from rider's payment method
            # Platform credits driver ₹164 to settlement wallet
            # Net settlement to driver wallet: +₹164 (credit)
            
            txn_amount = net_amount          # Amount to credit
            txn_type = 'credit'              # It's a credit to wallet
            purpose = 'trip_earnings'        # Purpose code
            new_balance = current_balance + net_amount  # Increase wallet
            
            # Example:
            # current_balance = 500.00
            # net_amount = 164.00
            # new_balance = 500.00 + 164.00 = 664.00
            # Driver earns 164.00
        
        # STEP 7: Create wallet transaction record (with idempotency)
        # This ensures if webhook is retried, we don't double-process
        try:
            with transaction.atomic():
                WalletTransaction.objects.create(
                    user_id=trip.driver_id.user_id,
                    amount=txn_amount,
                    txn_type=txn_type,
                    status='completed',
                    purpose=purpose,
                    reference_id=f'TRIP_{trip.id}',
                    idempotency_key=f'TRIP_{trip.id}_EARNING'  # Unique per trip
                )
        except IntegrityError:
            # Duplicate webhook/retry already processed this trip
            # Silently return to prevent double-crediting
            return

        # STEP 8: Update driver's wallet balance in database
        wallet.balance = new_balance
        wallet.save(update_fields=['balance'])

        # STEP 9: Create transaction history record
        # This is for audit trail and reporting
        TransactionHistory.objects.create(
            trip_id=trip,
            user_id=trip.user_id,
            driver_id=trip.driver_id,
            amount=txn_amount,
            method=trip.payment_method or 'online',
            status='completed',
            txn_type=txn_type,
            user_name=trip.user_id.full_name or trip.user_id.phone_number,
        )
```

### 4.2 Detailed Example: Online Payment (₹200 fare, 18% commission)

```
TRIP DATA:
  - final_fare: 200.00
  - payment_method: 'online'
  - driver_id.user_id: USER_123
  - zone: Hyderabad
  - vehicle_type: Auto

STEP-BY-STEP EXECUTION:

1. Get fare amount
   amount = 200.00

2. Look up commission rate
   - Find RateCard for (Hyderabad, Auto) at trip.requested_at
   - RateCard.commission_percent = 18.00
   commission_rate = 18.00 (Decimal)

3. Calculate commission
   commission = (200.00 * 18.00 / 100).quantize(0.01)
   commission = 36.00

4. Calculate net driver earning
   net_amount = 200.00 - 36.00 = 164.00

5. Get driver wallet
   wallet = Wallet(
       user_id=USER_123,
       scope='driver',  # This is DRIVER settlement, not rider credit
       balance=500.00   # Previous balance
   )
   current_balance = 500.00

6. Determine transaction type
   payment_method == 'online'  (not cash)
   
   txn_amount = 164.00 (net_amount)
   txn_type = 'credit'
   purpose = 'trip_earnings'
   new_balance = 500.00 + 164.00 = 664.00

7. Create WalletTransaction
   WalletTransaction(
       user_id=USER_123,
       amount=164.00,
       txn_type='credit',
       status='completed',
       purpose='trip_earnings',
       reference_id='TRIP_12345',
       idempotency_key='TRIP_12345_EARNING'  # UNIQUE - prevents duplicates
   )

8. Update wallet balance
   wallet.balance = 664.00
   wallet.save()

9. Create TransactionHistory
   TransactionHistory(
       trip_id=TRIP_12345,
       user_id=RIDER_USER,
       driver_id=DRIVER,
       amount=164.00,
       method='online',
       status='completed',
       txn_type='credit',
   )

FINAL RESULT:
  ✓ Driver wallet: 500.00 → 664.00 (credited ₹164)
  ✓ Company account: Receives ₹36 commission
  ✓ Audit trail: Complete transaction history
```

### 4.3 Detailed Example: Cash Payment (₹200 fare, 18% commission)

```
TRIP DATA:
  - final_fare: 200.00
  - payment_method: 'cash'
  - driver_id.user_id: USER_456
  - zone: Bangalore
  - vehicle_type: Sedan (20% commission)

STEP-BY-STEP EXECUTION:

1. Get fare amount
   amount = 200.00

2. Look up commission rate
   - Find RateCard for (Bangalore, Sedan) at trip.requested_at
   - RateCard.commission_percent = 20.00
   commission_rate = 20.00 (Decimal)

3. Calculate commission
   commission = (200.00 * 20.00 / 100).quantize(0.01)
   commission = 40.00

4. Calculate net driver earning
   net_amount = 200.00 - 40.00 = 160.00
   (NOT USED in cash flow - shown for reference)

5. Get driver wallet
   wallet = Wallet(
       user_id=USER_456,
       scope='driver',
       balance=1000.00
   )
   current_balance = 1000.00

6. Determine transaction type
   payment_method == 'cash'  (driver already has cash)
   
   txn_amount = 40.00 (commission)
   txn_type = 'debit'
   purpose = 'trip_commission'
   new_balance = 1000.00 - 40.00 = 960.00

   LOGIC: Driver collected ₹200 cash from rider.
          Driver owes SaaradhiGo ₹40 commission.
          So we DEBIT ₹40 from driver's settlement account.

7. Create WalletTransaction
   WalletTransaction(
       user_id=USER_456,
       amount=40.00,
       txn_type='debit',
       status='completed',
       purpose='trip_commission',
       reference_id='TRIP_67890',
       idempotency_key='TRIP_67890_EARNING'
   )

8. Update wallet balance
   wallet.balance = 960.00
   wallet.save()

9. Create TransactionHistory
   TransactionHistory(
       trip_id=TRIP_67890,
       user_id=RIDER_USER,
       driver_id=DRIVER,
       amount=40.00,
       method='cash',
       status='completed',
       txn_type='debit',
   )

FINAL RESULT:
  ✓ Driver wallet: 1000.00 → 960.00 (debited ₹40)
  ✓ Company account: Receives ₹40 commission
  ✓ Driver physically has: ₹200 (collected in cash)
  ✓ Net: Driver keeps ₹160, pays platform ₹40
```

---

## 5. ADMIN DASHBOARD REVENUE DISPLAY

### 5.1 Revenue Report View - [servers/admin_dashboard/views.py]

The admin dashboard displays the revenue split breakdown:

```python
def executive_revenue(request):
    """Display revenue analytics: rider payments, driver earnings, platform fees"""
    
    # Get all completed trips
    completed_trips = Trip.objects.filter(
        status_id__status_code='completed'
    ).select_related('user_id', 'driver_id', 'zone')
    
    # Initialize rows for different transaction types
    rider_rows = []      # What riders paid
    earning_rows = []    # What drivers earned
    payout_rows = []     # Driver payouts (same as earnings for display)
    fee_rows = []        # Platform fees (company revenue)
    refund_rows = []     # Refunded trips
    cancellation_rows = []  # Cancellation fees
    
    for trip in completed_trips:
        # Get names
        rider_name = trip.user_id.full_name or trip.user_id.phone_number
        driver_name = trip.driver_id.user_id.full_name if trip.driver_id else 'N/A'
        fare = trip.final_fare or trip.estimated_fare or Decimal('0.00')
        payment_method = trip.payment_method or 'online'
        created_at = trip.requested_at
        
        # ===== RIDER PAYMENT ROW =====
        # Shows how much the rider paid
        rider_rows.append({
            "transaction_id": f"TXN-{trip.id}",
            "type": "rider_payment",
            "trip_id": trip.id,
            "rider": rider_name,
            "driver": driver_name,
            "amount": fare,
            "status": "success",
            "payment_method": payment_method,
            "created_at": created_at,
        })
        
        # ===== GET COMMISSION RATE =====
        try:
            from servers.pricing.services import commission_percent_for_trip
            commission_percent = Decimal(
                str(commission_percent_for_trip(trip))
            )
        except Exception:
            commission_percent = Decimal("18.00")
        
        # ===== CALCULATE SPLIT =====
        # Formula:
        #   Platform Fee = Fare × Commission% ÷ 100
        #   Driver Earning = Fare - Platform Fee
        
        platform_fee = money(
            fare * commission_percent / Decimal("100")
        )
        driver_earning = money(
            fare - platform_fee
        )
        
        # ===== DRIVER EARNINGS ROW =====
        # Shows what the driver earned
        earning_rows.append({
            "transaction_id": f"EARNING-{trip.id}",
            "type": "driver_earnings",
            "trip_id": trip.id,
            "rider": rider_name,
            "driver": driver_name,
            "amount": driver_earning,      # Fare - Commission
            "status": "success",
            "payment_method": payment_method,
            "created_at": created_at,
        })
        
        # ===== DRIVER PAYOUT ROW =====
        # Payout = Earnings (in this mock implementation)
        payout_rows.append({
            "transaction_id": f"PAYOUT-{trip.id}",
            "type": "driver_payout",
            "trip_id": trip.id,
            "rider": rider_name,
            "driver": driver_name,
            "amount": driver_earning,      # Same as earnings
            "status": "success",
            "payment_method": payment_method,
            "created_at": created_at,
        })
        
        # ===== PLATFORM FEE ROW =====
        # Shows company revenue from this trip
        fee_rows.append({
            "transaction_id": f"FEE-{trip.id}",
            "type": "platform_fee",
            "trip_id": trip.id,
            "rider": rider_name,
            "driver": driver_name,
            "amount": platform_fee,        # Commission amount
            "status": "success",
            "payment_method": payment_method,
            "created_at": created_at,
        })
    
    # Calculate totals
    total_rider_payment = sum(t["amount"] for t in rider_rows)
    total_driver_earning = sum(t["amount"] for t in earning_rows)
    total_platform_fee = sum(t["amount"] for t in fee_rows)
    
    # Verify: should always equal
    # total_rider_payment == total_driver_earning + total_platform_fee
    
    context = {
        'rider_payment': {
            'rows': rider_rows,
            'total': total_rider_payment,
        },
        'driver_earnings': {
            'rows': earning_rows,
            'total': total_driver_earning,
        },
        'driver_payout': {
            'rows': payout_rows,
            'total': sum(t["amount"] for t in payout_rows),
        },
        'platform_fee': {
            'rows': fee_rows,
            'total': total_platform_fee,
        },
    }
    
    return render(request, 'revenue_dashboard.html', context)
```

### 5.2 Dashboard Display Example

```
REVENUE REPORT FOR Q4 2024

Trips Completed: 1,250

═══════════════════════════════════════════════════════════════

RIDER PAYMENTS (Total collected from riders)
  Trip #1001  | Rider ABC | Driver XYZ | ₹200.00
  Trip #1002  | Rider DEF | Driver UVW | ₹150.00
  Trip #1003  | Rider GHI | Driver RST | ₹300.00
  ────────────────────────────────────────────
  TOTAL RIDER PAYMENTS: ₹650.00

═══════════════════════════════════════════════════════════════

DRIVER EARNINGS (What drivers earned)
  Trip #1001  | Driver XYZ | ₹164.00 (200 - 36 commission)
  Trip #1002  | Driver UVW | ₹123.00 (150 - 27 commission)
  Trip #1003  | Driver RST | ₹240.00 (300 - 60 commission)
  ────────────────────────────────────────────
  TOTAL DRIVER EARNINGS: ₹527.00

═══════════════════════════════════════════════════════════════

PLATFORM FEES (Company revenue)
  Trip #1001  | ₹36.00   (200 × 18%)
  Trip #1002  | ₹27.00   (150 × 18%)
  Trip #1003  | ₹60.00   (300 × 20%)
  ────────────────────────────────────────────
  TOTAL PLATFORM FEES: ₹123.00

═══════════════════════════════════════════════════════════════

VERIFICATION:
  Rider Payments    ₹650.00
  = Driver Earnings ₹527.00  +  Platform Fees ₹123.00
  ✓ BALANCED
```

---

## 6. COMMISSION RATES BY VEHICLE TYPE

### 6.1 Default Rates (from migrations)

From [servers/pricing/migrations/0002_seed_phase0_data.py]:

```python
# Auto, Premium, Bike
commission_percent = Decimal('18.00')  # 18%

# Sedan, SUV
commission_percent = Decimal('20.00')  # 20%
```

### 6.2 Custom Rate Cards

Operators can override commission rates per zone:

```python
# Example: Create a rate card for Mumbai Premium rides (15% commission)
rate_card = RateCard.objects.create(
    zone=ServiceZone.objects.get(code='IN-MH-MUM'),
    vehicle_type=VehicleType.objects.get(type='Premium'),
    base_fare=Decimal('50.00'),
    per_km_fare=Decimal('20.00'),
    per_min_fare=Decimal('1.00'),
    min_fare=Decimal('50.00'),
    commission_percent=Decimal('15.00'),  # 15% instead of 18%
    effective_from=timezone.now(),
    is_active=True,
    version=1,
)

# This rate card will be used for all Premium trips in Mumbai
# with trip.requested_at >= effective_from
```

### 6.3 Rate Card Hierarchy (Zone Inheritance)

Zones are hierarchical: Country → State → City → Sub-Zone

```
Country: India (IN)
  └─ State: Telangana (IN-TG)
       ├─ City: Hyderabad (IN-TG-HYD)
       │    ├─ Sub-Zone: Airport (IN-TG-HYD-AIRPORT)
       │    └─ Sub-Zone: Hitech City (IN-TG-HYD-HITECH)
       └─ City: Warangal (IN-TG-WGL)
```

Rate card lookup walks up the chain:

```
Trip pickup at: Airport Sub-Zone (IN-TG-HYD-AIRPORT)

Lookup sequence:
1. Check for rate card in Airport zone (IN-TG-HYD-AIRPORT)
   ✓ Found: Commission = 22% (special airport rates)
   
Return this rate card.

If Airport had no rate card:
2. Check parent: Hyderabad City (IN-TG-HYD)
   ✓ Found: Commission = 18% (city rates)
   
Return this rate card.

If Hyderabad had no rate card:
3. Check parent: Telangana State (IN-TG)
4. Check parent: India Country (IN)
5. Use default: 18%
```

---

## 7. WALLET BALANCE TRACKING

### 7.1 Driver Wallet vs Rider Wallet

Drivers who also ride have TWO separate wallets:

```python
# Driver's SETTLEMENT wallet (for trip earnings)
driver_wallet = Wallet.objects.get(
    user_id=driver_user,
    scope=Wallet.SCOPE_DRIVER  # 'driver'
)
# This holds earnings from completed trips

# Same person's RIDER wallet (for credit balance)
rider_wallet = Wallet.objects.get(
    user_id=driver_user,
    scope=Wallet.SCOPE_RIDER   # 'rider'
)
# This holds credit balance for when they book rides

# They CANNOT spend driver_wallet on rider payments
# Settlement money is separate from credit
```

### 7.2 Wallet Balance Update Transaction

```python
# Before settlement
driver_wallet.balance = 500.00

# After crediting ₹164 earnings from trip
driver_wallet.balance = 664.00

# The balance update is atomic within a database transaction:
with transaction.atomic():
    wallet = Wallet.objects.select_for_update().get(...)
    wallet.balance = new_balance
    wallet.save(update_fields=['balance'])
```

### 7.3 Idempotency Protection

```python
# Without idempotency, webhook retries cause double-crediting:

# Webhook arrives (Trip #1001 completed)
WalletTransaction.objects.create(
    user_id=DRIVER,
    amount=164.00,
    txn_type='credit',
    reference_id='TRIP_1001',
    idempotency_key='TRIP_1001_EARNING'  # UNIQUE constraint
)
# Wallet balance: 500 → 664

# Webhook RETRY (duplicate delivery)
WalletTransaction.objects.create(
    user_id=DRIVER,
    amount=164.00,
    txn_type='credit',
    reference_id='TRIP_1001',
    idempotency_key='TRIP_1001_EARNING'  # ← DUPLICATE KEY!
)
# IntegrityError raised, caught, ignored
# Wallet balance: stays 664 (not double-credited)
```

---

## 8. EDGE CASES & SPECIAL HANDLING

### 8.1 Decimal Precision

All monetary calculations use `Decimal` for precision:

```python
from decimal import Decimal, ROUND_HALF_UP

# ✓ Correct
commission = (Decimal('200.00') * Decimal('18') / Decimal('100')).quantize(
    Decimal('0.01'), 
    rounding=ROUND_HALF_UP
)  # 36.00

# ✗ Wrong (floating point errors)
commission = 200.00 * 18 / 100  # 35.99999... != 36.00
```

### 8.2 Rounding Strategy

Uses ROUND_HALF_UP (banker's rounding):

```
₹36.005 → ₹36.01  (rounds up)
₹36.004 → ₹36.00  (rounds down)

This ensures no unaccounted penny losses across thousands of trips.
```

### 8.3 Null Fare Handling

```python
amount = trip.final_fare or trip.estimated_fare or Decimal('0.00')

# Priority:
# 1. Use final_fare if trip completed with actual fare
# 2. Use estimated_fare if final_fare not recorded (legacy)
# 3. Default to ₹0.00 if both missing (cancellation, error)
```

### 8.4 Missing Rate Card

```python
def commission_percent_for_trip(trip) -> Decimal:
    try:
        card = rate_card_for_trip(trip=trip)
        if card and card.commission_percent:
            return Decimal(str(card.commission_percent))
    except Exception as exc:
        logger.warning('commission lookup failed for trip %s: %s', trip.id, exc)
    
    # Fallback chain:
    # 1. settings.PLATFORM_COMMISSION_PERCENT
    # 2. Default 18%
    fallback = getattr(settings, 'PLATFORM_COMMISSION_PERCENT', Decimal('18'))
    return Decimal(str(fallback)) if fallback else Decimal('18')
```

---

## 9. COMPLETE FLOW DIAGRAM

```
┌──────────────────────────────────────────────────────────┐
│  RIDER BOOKS A TRIP (₹200 estimated fare)               │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  DRIVER ACCEPTS & COMPLETES TRIP                        │
│  Trip.final_fare = ₹200.00                              │
│  Trip.payment_method = 'online'                         │
│  Trip.requested_at = timestamp                          │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  WEBHOOK: TRIP COMPLETED                                │
│  → credit_driver_wallet(trip) called                    │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  STEP 1: GET FARE AMOUNT                                │
│  amount = trip.final_fare = 200.00                      │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  STEP 2: LOOKUP COMMISSION RATE                         │
│  • Find trip's zone (Hyderabad)                         │
│  • Find trip's vehicle type (Auto)                      │
│  • Get RateCard for (Hyderabad, Auto)                   │
│  • RateCard.commission_percent = 18.00%                 │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  STEP 3: CALCULATE COMMISSION                           │
│  commission = 200.00 × 18 ÷ 100 = 36.00                │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  STEP 4: CALCULATE NET DRIVER EARNING                   │
│  net_amount = 200.00 - 36.00 = 164.00                  │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  STEP 5: GET DRIVER WALLET (with lock)                  │
│  wallet = Wallet(                                       │
│      user_id=driver_user,                               │
│      scope='driver',                                    │
│      balance=500.00                                     │
│  )                                                      │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  STEP 6: DETERMINE TRANSACTION TYPE                     │
│  payment_method = 'online' (not cash)                   │
│  →  txn_amount = 164.00 (net_amount)                    │
│  →  txn_type = 'credit'                                 │
│  →  new_balance = 500.00 + 164.00 = 664.00             │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  STEP 7: CREATE WALLET TRANSACTION (idempotent)        │
│  WalletTransaction(                                     │
│      user_id=driver_user,                               │
│      amount=164.00,                                     │
│      txn_type='credit',                                 │
│      status='completed',                                │
│      purpose='trip_earnings',                           │
│      idempotency_key='TRIP_1001_EARNING'  ← UNIQUE     │
│  )                                                      │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  STEP 8: UPDATE WALLET BALANCE                          │
│  wallet.balance = 664.00                                │
│  wallet.save()                                          │
│  (atomic transaction commits)                           │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  STEP 9: CREATE AUDIT TRANSACTION HISTORY              │
│  TransactionHistory(                                    │
│      trip_id=trip,                                      │
│      amount=164.00,                                     │
│      txn_type='credit',                                 │
│      status='completed'                                 │
│  )                                                      │
└──────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────┐
│  SETTLEMENT COMPLETE                                    │
│                                                         │
│  Driver Wallet: 500.00 → 664.00                         │
│  Driver Earned: ₹164.00                                │
│  Company Got:   ₹36.00                                 │
└──────────────────────────────────────────────────────────┘
```

---

## 10. SUMMARY TABLE

| Component | Value | Formula | Code Location |
|-----------|-------|---------|---|
| **Total Fare** | ₹200.00 | Rider pays | Trip.final_fare |
| **Commission %** | 18% | Zone + Vehicle Type | RateCard.commission_percent |
| **Commission Amount** | ₹36.00 | Fare × 18% | servers/driver/utils.py:68 |
| **Driver Earning** | ₹164.00 | Fare - Commission | servers/driver/utils.py:70 |
| **Company Revenue** | ₹36.00 | Commission Amount | servers/admin_dashboard/views.py:1510 |
| **Driver Wallet Credit** | ₹164.00 | net_amount | servers/driver/utils.py:90 |
| **Transaction Type** | credit | Online payment | servers/driver/utils.py:88 |
| **Idempotency Key** | TRIP_1001_EARNING | Unique per trip | servers/driver/utils.py:96 |

---

## 11. KEY FILES REFERENCE

| Purpose | File | Key Function/Class |
|---------|------|---|
| **Revenue Split Calculation** | servers/driver/utils.py | `credit_driver_wallet()` |
| **Commission Lookup** | servers/pricing/services.py | `commission_percent_for_trip()` |
| **Rate Card Resolution** | servers/pricing/services.py | `rate_card_for_trip()` |
| **Rate Card Model** | servers/pricing/models.py | `RateCard` class |
| **Wallet Model** | servers/rider/models.py | `Wallet`, `WalletTransaction` |
| **Trip Model** | servers/ride/models.py | `Trip` class |
| **Admin Dashboard** | servers/admin_dashboard/views.py | `executive_revenue()` view |
| **Transaction History** | servers/payments/models.py | `TransactionHistory` class |

---

## 12. TESTING THE LOGIC

### Unit Test Example

```python
def test_revenue_split_online_payment():
    """Test driver gets (fare - commission), company gets commission"""
    from servers.ride.models import Trip
    from servers.driver.utils import credit_driver_wallet
    from servers.rider.models import Wallet, WalletTransaction
    
    # Setup
    driver = Driver.objects.create(...)
    trip = Trip.objects.create(
        driver_id=driver,
        final_fare=Decimal('200.00'),
        payment_method='online',
        zone=zone_hyderabad,
        requested_vehicle_type=vehicle_auto,
        requested_at=timezone.now(),
    )
    
    wallet = Wallet.objects.create(
        user_id=driver.user_id,
        scope=Wallet.SCOPE_DRIVER,
        balance=Decimal('500.00')
    )
    
    # Execute
    credit_driver_wallet(trip)
    
    # Assert
    wallet.refresh_from_db()
    
    # Driver should receive 164 (200 - 36 commission)
    assert wallet.balance == Decimal('664.00')
    
    # Should have a wallet transaction
    txn = WalletTransaction.objects.get(reference_id=f'TRIP_{trip.id}')
    assert txn.amount == Decimal('164.00')
    assert txn.txn_type == 'credit'
    assert txn.purpose == 'trip_earnings'
```

---

**Document Version**: 1.0  
**Last Updated**: 2024  
**Author**: SaaradhiGo Engineering Team  
**Status**: Current Production Implementation
