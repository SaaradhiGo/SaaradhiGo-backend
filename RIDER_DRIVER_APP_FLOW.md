# Rider App vs Driver App Flow & Configuration Strategy

## PART 1: WHAT RIDERS DO IN THE RIDER APP

### 1.1 Trip Booking Journey

#### Step 1: Estimate Fare (Before Booking)
**Endpoint**: `POST /api/ride/estimate-fare/`  
**File**: [servers/ride/views.py](servers/ride/views.py#L20-L120)

```python
# Rider inputs
{
    "pickup_lat": 17.3850,
    "pickup_long": 78.4867,          # Pickup location
    "destination_lat": 17.4000,
    "destination_long": 78.5200,     # Drop location
    "distance_km": 8.5,
    "duration_min": 18,
    "vehicle_type": "auto"            # or 'sedan', 'premium', 'bike'
}

# System response with EXACT FARE (no commission shown to rider)
{
    "base_fare": 50.00,
    "distance_fare": 127.50,          # 8.5 km × ₹15/km
    "time_fare": 18.00,               # 18 min × ₹1/min
    "surge_multiplier": 1.0,
    "total_fare": 195.50,             # WHAT RIDER SEES & PAYS
    "zone": "Hyderabad",
    "rate_card_id": 123
}
```

**What happens behind the scenes:**
```
1. Find service zone from pickup coordinates
   zone = find_zone_for_point(17.3850, 78.4867)
   → Returns: ServiceZone(code='IN-TG-HYD-AIRPORT', priority=100)

2. Look up RateCard for (zone, vehicle_type)
   card = get_active_rate_card(
       zone=IN-TG-HYD-AIRPORT,
       vehicle_type=Auto,
       at=now()
   )
   → Returns: RateCard(
       base_fare=50.00,
       per_km_fare=15.00,
       per_min_fare=1.00,
       commission_percent=18.00,  ← NOT SHOWN TO RIDER
       ...
   )

3. Calculate fare components
   base = 50.00
   distance = 8.5 × 15.00 = 127.50
   time = 18 × 1.00 = 18.00
   subtotal = 50 + 127.50 + 18 = 195.50
   
   surge_multiplier = compute_surge_from_redis(lat, lon)
   
   final_fare = 195.50 × 1.0 = 195.50

4. NO COMMISSION DEDUCTION
   Rider sees: ₹195.50
   This is the FULL amount rider will be charged
   (Commission is internal between driver & platform)
```

#### Step 2: Book Trip
**Endpoint**: `POST /api/ride/book/`  
**File**: [servers/ride/views.py](servers/ride/views.py#L130-L220)

```python
# Rider submits booking
{
    "pickup_lat": 17.3850,
    "pickup_long": 78.4867,
    "destination_lat": 17.4000,
    "destination_long": 78.5200,
    "vehicle_type": "auto",
    "payment_method": "online"        # or 'cash', 'wallet'
}

# Trip created with status: REQUESTED
Trip(
    user_id=RIDER_123,
    status_id=requested,
    pickup_lat=17.3850,
    pickup_long=78.4867,
    estimated_fare=195.50,           # Quoted fare
    zone=IN-TG-HYD,
    payment_method='online'
)
```

**What's happening:**
- Trip is broadcasted to nearby drivers via WebSocket/Redis
- Drivers see: pickup location, rider rating, vehicle type needed
- Drivers do NOT see the fare (they earn based on final_fare)

#### Step 3: Driver Accepts (Rider sees "Driver Incoming")
Trip status changes: `REQUESTED` → `ACCEPTED`

**Rider sees:**
- Driver name, photo, vehicle details
- Estimated time to arrival
- Live location of driver (real-time GPS updates)

#### Step 4: Driver Arrives at Pickup
Trip status: `REACHED`

#### Step 5: Trip In Progress
Trip status: `IN_PROGRESS`

**Rider sees:**
- Live trip tracking
- Estimated time remaining
- Distance traveled

#### Step 6: Trip Completed
Trip status: `COMPLETED`

**Rider sees:**
```
TRIP RECEIPT:
  Pickup: 123 Main Street
  Drop: 456 Airport Road
  Distance: 8.5 km
  Duration: 18 min
  
  FARE BREAKDOWN:
  Base Fare:           ₹50.00
  Distance (8.5 km):   ₹127.50
  Time (18 min):       ₹18.00
  Surge:               ₹0.00 (1.0×)
  ─────────────────────────────
  TOTAL:               ₹195.50
  
  Payment Method: Online (Already Paid)
  Driver: Rajesh K
  Rating: ⭐ 4.8
```

⚠️ **NOTE**: Rider NEVER sees commission amount. Commission is calculated and split internally.

### 1.2 Wallet & Payment Management

#### View Wallet Balance
**Endpoint**: `GET /api/rider/wallet/balance/`  
**File**: [servers/rider/views.py](servers/rider/views.py#L450-L480)

```python
# Rider has TWO separate wallets

Wallet A: SCOPE_RIDER (Credit Balance)
  Balance: ₹2,500.00
  Purpose: Credits from refunds, promotions, top-ups
  Use: Can spend on rides

Wallet B: SCOPE_DRIVER (Settlement - if rider is also a driver)
  Balance: ₹8,400.00
  Purpose: Earnings from trips they drove
  Use: Can ONLY withdraw (not spend on rides)
```

#### Top-Up Wallet
**Endpoint**: `POST /api/rider/wallet/payment/`  
**File**: [servers/rider/views.py](servers/rider/views.py#L495-L550)

```python
# Rider initiates wallet top-up
{
    "amount": 1000.00,
    "payment_gateway": "cashfree",    # Payment gateway
    "return_url": "https://app.saaradhi.go/wallet/success"
}

# Flow:
# 1. Create Payment record with status='pending'
# 2. Generate Cashfree order
# 3. Redirect to Cashfree checkout
# 4. Rider enters card/UPI details
# 5. Cashfree processes payment
# 6. Webhook confirms payment
# 7. WalletTransaction created with status='completed'
# 8. Rider credit balance increases by ₹1000
```

#### View Transaction History
**Endpoint**: `GET /api/rider/wallet/transactions/`  
**File**: [servers/rider/views.py](servers/rider/views.py#L560-L600)

```python
# Rider sees all wallet movements

[
  {
    "id": "TXN-001",
    "type": "credit",           # Money in
    "amount": 1000.00,
    "purpose": "wallet_topup",
    "gateway": "cashfree",
    "status": "completed",
    "created_at": "2024-01-15 14:30:00"
  },
  {
    "id": "TXN-002",
    "type": "debit",            # Money out
    "amount": 195.50,
    "purpose": "trip_payment",
    "reference_id": "TRIP_12345",
    "status": "completed",
    "created_at": "2024-01-15 15:00:00"
  },
  {
    "id": "TXN-003",
    "type": "credit",           # Refund
    "amount": 50.00,
    "purpose": "trip_refund",
    "reference_id": "TRIP_67890",
    "status": "completed",
    "created_at": "2024-01-15 16:20:00"
  }
]
```

### 1.3 Locations & Favorites

**Endpoints**:
- `POST /api/rider/locations/` - Save favorite location (Home, Work, etc)
- `GET /api/rider/locations/all/` - List all saved locations
- `DELETE /api/rider/locations/<id>/` - Delete location

**Example:**
```python
# Save favorite location
{
    "address_text": "123 Tech Park, Hyderabad",
    "latitude": 17.4500,
    "longitude": 78.5300,
    "label": "Office"  # Home, Work, Airport, etc
}

# Rider can quickly book from these saved locations
```

### 1.4 Notifications & Preferences

**Endpoints**:
- `GET /api/rider/notifications/` - List notifications
- `POST /api/rider/notifications/<id>/read/` - Mark as read
- `GET /api/rider/notifications/preferences/` - Get notification settings
- `PUT /api/rider/notifications/preferences/update/` - Enable/disable trip alerts, promos, etc

---

## PART 2: WHAT DRIVERS DO IN THE DRIVER APP

### 2.1 Trip Acceptance Journey

#### Step 1: Receive Trip Request (WebSocket Real-time)
**Technology**: Django Channels + Redis + WebSocket  
**File**: [servers/consumers.py](servers/consumers.py)

```python
# Trip is broadcast to nearby drivers in waves:

WAVE 1 (0-20 sec):  All drivers within 1.5 km
WAVE 2 (20-40 sec): All drivers within 3.0 km
WAVE 3 (40-60 sec): All drivers within 5.0 km
TIMEOUT (60+ sec):  Trip auto-cancels, retry logic

# Config from env:
DISPATCH_RADIUS_WAVES_M = '1500,3000,5000'  # meters
DISPATCH_WAVE_SECONDS = '20'                  # seconds per wave
TRIP_ACCEPT_TIMEOUT_SECONDS = '90'            # auto-cancel
```

**What driver sees:**
```
INCOMING TRIP (Real-time notification)

Pickup Location: 123 Main Street, Hyderabad
Rider: Priya P ⭐ 4.9
Drop: 456 Airport Road
Distance: 8.5 km
Estimated Time: 18 min
Surge: 1.0×

[ACCEPT] [REJECT]

(Auto-rejects in 30 sec if driver doesn't respond)
```

#### Step 2: Accept Trip
**Endpoint**: `POST /api/driver/trips/<id>/accept/`  
**File**: [servers/driver/views.py](servers/driver/views.py#L50-L120)

Trip status changes: `ACCEPTED`

```python
# Driver accepts
{
    "trip_id": 12345,
    "location": {
        "lat": 17.3800,
        "long": 78.4800
    }
}

# System response
{
    "trip_id": 12345,
    "rider_phone": "+91-9876543210",
    "pickup_address": "123 Main Street",
    "drop_address": "456 Airport Road",
    "distance_km": 8.5,
    "estimated_duration_min": 18,
    "estimated_fare": 195.50,  # ← Driver sees this (but it's for reference)
    "payment_method": "online",
    "rider_rating": 4.9
}
```

**What driver sees:**
- Rider's exact location
- Rider's phone number
- Trip details
- Pre-assigned payment method

#### Step 3: Pick up Rider
Trip status: `REACHED`

**Driver sees:**
- "Waiting for rider"
- Confirmation button to mark "started"
- Current GPS location

#### Step 4: Trip In Progress
Trip status: `IN_PROGRESS`

**Driver sees:**
- Current GPS location
- Destination address
- Real-time navigation
- Estimated time remaining

#### Step 5: Complete Trip
**Endpoint**: `POST /api/driver/trips/<id>/complete/`  
**File**: [servers/driver/views.py](servers/driver/views.py#L140-L200)

Trip status: `COMPLETED`

```python
# Driver completes trip with actual metrics
{
    "trip_id": 12345,
    "end_location": {
        "lat": 17.4000,
        "long": 78.5200
    },
    "actual_distance_km": 8.4,       # Actual measured
    "actual_duration_min": 17,       # Actual measured
    "final_fare": 195.50             # Final calculated fare
}

# System processes:
# 1. Trip status = COMPLETED
# 2. Payment processed (if online)
# 3. Commission calculated from RateCard
# 4. Driver wallet credited with earnings
# 5. TransactionHistory created
```

**Driver sees:**
```
TRIP COMPLETED ✓

Trip ID: #12345
Pickup: 123 Main Street
Drop: 456 Airport Road

DISTANCE: 8.4 km
DURATION: 17 min

FARE RECEIVED: ₹195.50
(This is AFTER commission already calculated)

Rider Rating: ⭐ 4.9 [Rate Rider]

Next Trip: Ready to accept more trips
```

### 2.2 Earnings & Wallet Management

#### View Earnings (Transaction History)
**Endpoint**: `GET /api/driver/earnings/`  
**File**: [servers/driver/views.py](servers/driver/views.py#L242-L280)

```python
# Driver sees all trip earnings (credit transactions only)

[
  {
    "id": "TXN-001",
    "trip_id": 12345,
    "amount": 164.00,              # AFTER commission (200 - 36)
    "payment_method": "online",
    "status": "completed",
    "rider": "Priya P",
    "created_at": "2024-01-15 15:30:00",
    "type": "credit"               # Earning
  },
  {
    "id": "TXN-002",
    "trip_id": 12346,
    "amount": 155.50,              # AFTER commission (195.50 - 40)
    "payment_method": "cash",      # Driver collected cash
    "status": "completed",
    "rider": "Rajesh K",
    "created_at": "2024-01-15 16:00:00",
    "type": "credit"
  }
]
```

#### Earnings Summary
**Endpoint**: `GET /api/driver/earnings/summary/`  
**File**: [servers/driver/views.py](servers/driver/views.py#L283-L320)

```python
# Dashboard summary

{
    "total_earned": 8400.00,        # All-time total earnings
    "total_commission": 1680.00,    # 20% of earnings (HARDCODED BUG!)
    "total_trips": 50,              # Completed trips
    "today_earned": 450.00,         # Today's earnings
    "today_trips": 3,               # Today's completed trips
    "wallet_balance": 8400.00,      # Current settlement balance
    "average_per_trip": 168.00      # 8400 / 50
}

# ⚠️ BUG: total_commission uses hardcoded 20%
# Should call commission_percent_for_trip() for actual rates
```

### 2.3 Withdrawal & Payout

#### Check Withdrawal Eligibility
**Endpoint**: `GET /api/driver/withdrawals/balance/`  
**File**: [servers/driver/views.py](servers/driver/views.py#L330-L370)

```python
{
    "wallet_balance": 8400.00,           # Total available
    "minimum_withdrawal": 500.00,        # Minimum per withdrawal
    "maximum_withdrawal": 8400.00,       # Can't exceed balance
    "platform_fee_percent": 2.00,        # Withdrawal fee
    "estimated_net": 8232.00,            # After 2% fee
    
    "withdrawal_blocked": true,
    "block_reason": "7-day cooldown",
    "last_withdrawal_date": "2024-01-15 10:00:00",
    "next_withdrawal_available": "2024-01-22 10:00:00"  # 7 days later
}
```

**Withdrawal Rules:**
```
1. Minimum ₹500 per withdrawal
2. 7-day cooldown after each withdrawal
3. 2% platform fee (≈ payment gateway fees)
4. Supports: UPI, Bank Transfer, Wallet
```

#### Request Withdrawal
**Endpoint**: `POST /api/driver/withdrawals/request/`  
**File**: [servers/driver/views.py](servers/driver/views.py#L375-L450)

```python
# Driver requests payout
{
    "amount": 5000.00,
    "method": "upi",                    # or 'bank_transfer'
    "upi_id": "rajesh.k@paytm"          # For UPI method
}

# or for bank transfer
{
    "amount": 5000.00,
    "method": "bank_transfer",
    "account_number": "1234567890",
    "ifsc_code": "HDFC0001234",
    "account_name": "Rajesh Kumar"
}

# System processes:
# 1. Validate ₹5000 >= ₹500 (minimum)
# 2. Validate 7-day cooldown passed
# 3. Calculate fee: 5000 × 2% = 100
# 4. Net payout: 5000 - 100 = 4900
# 5. Initiate Cashfree payout
# 6. Create WithdrawalRequest record
# 7. Update driver.last_withdrawal_at
# 8. Debit wallet: 8400 → 3400 (5000 removed)

# Response:
{
    "withdrawal_id": "WD-12345",
    "status": "processing",
    "requested_amount": 5000.00,
    "platform_fee": 100.00,
    "net_amount": 4900.00,
    "method": "upi",
    "upi_id": "rajesh.k@paytm",
    "estimated_completion": "2024-01-16 18:00:00"
}
```

#### Withdrawal History
**Endpoint**: `GET /api/driver/withdrawals/history/`  
**File**: [servers/driver/views.py](servers/driver/views.py#L455-L490)

```python
[
  {
    "id": "WD-12345",
    "requested_amount": 5000.00,
    "platform_fee": 100.00,
    "net_amount": 4900.00,
    "method": "upi",
    "status": "completed",            # pending, processing, completed, failed
    "requested_at": "2024-01-10 15:30:00",
    "completed_at": "2024-01-11 18:45:00"
  },
  {
    "id": "WD-12344",
    "requested_amount": 2000.00,
    "platform_fee": 40.00,
    "net_amount": 1960.00,
    "method": "bank_transfer",
    "status": "completed",
    "requested_at": "2024-01-03 12:00:00",
    "completed_at": "2024-01-04 09:30:00"
  }
]
```

### 2.4 Vehicle & Profile Management

#### List Vehicles
**Endpoint**: `GET /api/driver/vehicles/`  
**File**: [servers/driver/views.py](servers/driver/views.py#L500-L530)

```python
[
  {
    "id": 1,
    "registration_number": "TS09AB1234",
    "model": "Swift Dzire",
    "manufacture_year": 2022,
    "color": "White",
    "vehicle_type": "auto",
    "seating_capacity": 4,
    "status": "active"                # or 'inactive'
  }
]
```

#### Add Vehicle
**Endpoint**: `POST /api/driver/vehicles/add/`

```python
{
    "registration_number": "TS09CD5678",
    "model": "Hyundai i10",
    "manufacture_year": 2023,
    "color": "Silver",
    "vehicle_type": "auto",
    "seating_capacity": 4,
    "document_files": {
        "rc": <file>,                 # Registration Certificate
        "insurance": <file>,
        "pollution": <file>
    }
}
```

#### Update Profile
**Endpoint**: `PUT /api/driver/profile/update/`

```python
{
    "full_name": "Rajesh Kumar",
    "email": "rajesh@example.com",
    "phone_number": "+91-9876543210",
    "upi_id": "rajesh.k@paytm",       # For withdrawals
    "preferred_withdrawal_method": "upi"
}
```

---

## PART 3: HOW COMMISSION IS CURRENTLY HANDLED (ENV-BASED)

### 3.1 Current Architecture (Production)

```
.env.local (or OS environment)
    ↓
PLATFORM_COMMISSION_PERCENT="18"
    ↓
base/settings.py (Django settings)
    ↓
PLATFORM_COMMISSION_PERCENT = Decimal('18')
    ↓
At runtime: commission_percent_for_trip()
    ├─ Try: Get from RateCard (zone + vehicle_type)
    └─ Fallback: Use PLATFORM_COMMISSION_PERCENT from settings
```

**Code Flow:**

```python
# [base/settings.py] Line 336
PLATFORM_COMMISSION_PERCENT = _Decimal(
    os.environ.get("PLATFORM_COMMISSION_PERCENT", "18")
)

# [servers/pricing/services.py] Line 203
def commission_percent_for_trip(trip) -> Decimal:
    """Get commission % for a trip."""
    try:
        # PRIMARY: Get from RateCard (zone + vehicle_type specific)
        card = rate_card_for_trip(trip=trip)
        if card is not None and card.commission_percent is not None:
            return Decimal(str(card.commission_percent))
    except Exception as exc:
        logger.warning('commission lookup failed for trip %s: %s', trip.id, exc)
    
    # FALLBACK: Use environment variable
    fallback = getattr(settings, 'PLATFORM_COMMISSION_PERCENT', Decimal('18'))
    return Decimal(str(fallback))
```

### 3.2 Problems with Current Environment-Based Approach

| Problem | Impact |
|---------|--------|
| **To change commission%**: Need to edit `.env.local` | Requires code deployment |
| **Deploy needed**: Restart Django server | ⏱️ **DOWNTIME** (5-10 minutes) |
| **During downtime**: All trip requests fail | 😞 Riders can't book |
| **Zone-specific rates**: Only in RateCard anyway | ENV var only for fallback |
| **A/B testing**: Can't change rates without full restart | Slow iteration |
| **Emergency rate change**: Need DevOps to deploy | Slow response |
| **Multiple environments**: Can't hot-update prod | Risk of misconfigs |

### 3.3 Example: Rate Change with ENV-based System

```
Timeline:
├─ 14:00 → Ops decides to change commission: 18% → 20%
├─ 14:05 → Engineer updates .env.local: "18" → "20"
├─ 14:10 → Git push & deploy initiated
├─ 14:15 → Server restart begins
│           ⚠️ DOWNTIME STARTS
│           ✗ New bookings FAIL
│           ✗ Trip completions queued (no credits)
├─ 14:20 → Server comes back online
│           ✓ DOWNTIME ENDS
├─ 14:25 → Normal operations resume
│
└─ Total downtime: ~10 minutes
   Lost bookings: ~50-100 trips in rush hour
   Bad reputation: Riders see "booking unavailable"
```

---

## PART 4: BETTER APPROACH - DATABASE-BASED COMMISSION

### 4.1 Improved Architecture (No Downtime)

```
DATABASE: Platform Settings Table
    ↓
platform_settings (new table)
├─ key: "PLATFORM_COMMISSION_PERCENT"
├─ value: "20"
├─ updated_at: 2024-01-15 14:15:00
└─ updated_by: "ops_admin"
    ↓
At runtime: commission_percent_for_trip()
    ├─ Try: Get from RateCard (zone + vehicle_type)
    └─ Fallback: Query platform_settings table
    ↓
✓ NO SERVER RESTART NEEDED
✓ INSTANT CHANGE
✓ CACHED IN REDIS (for performance)
```

### 4.2 Implementation: PlatformSettings Model

**New model to add:**

```python
# [servers/pricing/models.py] ADD THIS:

from django.db import models
from decimal import Decimal

class PlatformSettings(models.Model):
    """System-wide configuration stored in database."""
    
    SETTING_TYPES = [
        ('decimal', 'Decimal'),
        ('integer', 'Integer'),
        ('string', 'String'),
        ('boolean', 'Boolean'),
        ('json', 'JSON'),
    ]
    
    key = models.CharField(
        max_length=256,
        unique=True,
        db_index=True,
        help_text='Setting key, e.g. "PLATFORM_COMMISSION_PERCENT"'
    )
    value = models.TextField(
        help_text='Setting value as string (parsed based on type)'
    )
    setting_type = models.CharField(
        max_length=20,
        choices=SETTING_TYPES,
        default='string'
    )
    
    description = models.TextField(
        blank=True,
        help_text='Human-readable description'
    )
    
    # Audit trail
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='platform_settings_updates'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name_plural = 'Platform Settings'
        ordering = ['-updated_at']
    
    def __str__(self):
        return f'{self.key} = {self.value}'
    
    def get_value(self):
        """Parse and return typed value."""
        if self.setting_type == 'decimal':
            return Decimal(self.value)
        elif self.setting_type == 'integer':
            return int(self.value)
        elif self.setting_type == 'boolean':
            return self.value.lower() in ('true', '1', 'yes')
        elif self.setting_type == 'json':
            import json
            return json.loads(self.value)
        else:
            return self.value
```

### 4.3 Updated Commission Lookup (with DB Fallback)

```python
# [servers/pricing/services.py] MODIFY:

import redis
from django.core.cache import cache
from servers.pricing.models import PlatformSettings
from decimal import Decimal

def commission_percent_for_trip(trip) -> Decimal:
    """
    Platform commission % for a trip.
    
    Priority order:
      1. RateCard.commission_percent (zone + vehicle_type)
      2. PlatformSettings.PLATFORM_COMMISSION_PERCENT (DB cached)
      3. Default: 18%
    """
    try:
        # PRIMARY: Get from RateCard
        card = rate_card_for_trip(trip=trip)
        if card is not None and card.commission_percent is not None:
            return Decimal(str(card.commission_percent))
    except Exception as exc:
        logger.warning('commission lookup failed for trip %s: %s', trip.id, exc)
    
    # FALLBACK 1: Try database (with Redis cache)
    try:
        # Check Redis cache first (5-minute TTL)
        cache_key = 'PLATFORM_COMMISSION_PERCENT'
        cached_value = cache.get(cache_key)
        
        if cached_value is not None:
            return Decimal(str(cached_value))
        
        # If not cached, query database
        setting = PlatformSettings.objects.get(key='PLATFORM_COMMISSION_PERCENT')
        value = setting.get_value()
        
        # Cache for 5 minutes
        cache.set(cache_key, value, timeout=300)
        
        return Decimal(str(value))
    except PlatformSettings.DoesNotExist:
        logger.warning('PlatformSettings.PLATFORM_COMMISSION_PERCENT not found')
    except Exception as exc:
        logger.error('Error reading platform settings: %s', exc)
    
    # FALLBACK 2: Environment variable (as last resort)
    fallback = getattr(settings, 'PLATFORM_COMMISSION_PERCENT', Decimal('18'))
    return Decimal(str(fallback)) if fallback else Decimal('18')
```

### 4.4 Admin Interface to Change Commission

**Add to Django Admin:**

```python
# [servers/pricing/admin.py] ADD:

from django.contrib import admin
from servers.pricing.models import PlatformSettings

@admin.register(PlatformSettings)
class PlatformSettingsAdmin(admin.ModelAdmin):
    list_display = ['key', 'value', 'setting_type', 'updated_at', 'updated_by']
    list_filter = ['setting_type', 'updated_at']
    search_fields = ['key', 'description']
    readonly_fields = ['created_at', 'updated_at', 'updated_by']
    
    fieldsets = (
        ('Setting', {
            'fields': ('key', 'value', 'setting_type')
        }),
        ('Documentation', {
            'fields': ('description',)
        }),
        ('Audit Trail', {
            'fields': ('updated_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def save_model(self, request, obj, form, change):
        if not change:  # Creating new
            obj.updated_by = request.user
        else:  # Updating existing
            obj.updated_by = request.user
        super().save_model(request, obj, form, change)
```

**Ops can now:**
1. Login to Django admin
2. Navigate to "Platform Settings"
3. Click "PLATFORM_COMMISSION_PERCENT"
4. Change value: "18" → "20"
5. Click "Save"
6. ✓ Change is LIVE immediately (after cache expires or invalidates)

### 4.5 Example: Rate Change with DB-Based System

```
Timeline:
├─ 14:00 → Ops decides to change commission: 18% → 20%
├─ 14:01 → Login to Django admin
├─ 14:02 → Edit PlatformSettings.PLATFORM_COMMISSION_PERCENT: "20"
├─ 14:03 → Click Save
│           ✓ Database updated
│           ✓ Cache invalidated (or waits 5 min)
├─ 14:04 → New trip completion uses 20% commission
│           ✓ NO DOWNTIME
│           ✓ All bookings continue working
│           ✓ New earnings calculated at 20%
│
└─ Total downtime: 0 minutes
   Lost bookings: 0
   Bad reputation: None
```

---

## PART 5: COMPARISON TABLE

| Aspect | ENV-Based (Current) | DB-Based (Proposed) |
|--------|---|---|
| **How it works** | Read from `.env.local` at startup | Query database at runtime (cached) |
| **Downtime to change** | YES - Restart required | NO - Instant via admin |
| **Change mechanism** | Edit file → Git push → Deploy | Admin UI → Click save |
| **Time to apply change** | 10-15 minutes | < 1 second (cache miss: <100ms) |
| **Flexibility** | Low (env per environment) | High (change per environment) |
| **Audit trail** | Lost after restart | Stored (who changed, when) |
| **Emergency response** | Slow | Fast |
| **A/B testing** | Not feasible | Feasible |
| **Per-zone rates** | Only via RateCard | Only via RateCard |
| **Fallback behavior** | Hardcoded "18" | Database → Env → Hardcoded |
| **Performance** | Loaded once | Cached in Redis |
| **Scalability** | Fine for single env | Scales to multi-region |

---

## PART 6: MIGRATION PLAN (To implement DB-based)

### Phase 1: Create Database Model

```sql
CREATE TABLE pricing_platformsettings (
    id INTEGER PRIMARY KEY,
    key VARCHAR(256) UNIQUE NOT NULL,
    value TEXT NOT NULL,
    setting_type VARCHAR(20),
    description TEXT,
    updated_by_id INTEGER REFERENCES auth_user(id),
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

INSERT INTO pricing_platformsettings 
(key, value, setting_type, description, created_at, updated_at)
VALUES (
    'PLATFORM_COMMISSION_PERCENT',
    '18',
    'decimal',
    'Default platform commission percentage for trips',
    NOW(),
    NOW()
);
```

### Phase 2: Update Code

1. Add `PlatformSettings` model to [servers/pricing/models.py](servers/pricing/models.py)
2. Update `commission_percent_for_trip()` in [servers/pricing/services.py](servers/pricing/services.py)
3. Add admin interface in [servers/pricing/admin.py](servers/pricing/admin.py)
4. Add cache invalidation on save

### Phase 3: Deploy

1. Deploy code changes
2. Run migration: `python manage.py migrate`
3. Verify settings table created
4. Test: Change commission in admin → verify new trips use new rate
5. Monitor: Check transaction logs for correct commissions

### Phase 4: Remove ENV Dependency

After verification:
1. Optional: Keep `.env.local` entry as backup
2. Document: Update ops runbooks
3. Train: Teach ops how to use admin UI

---

## PART 7: CACHING STRATEGY (Performance)

To avoid DB query on every trip settlement:

```python
from django.core.cache import cache

# In commission_percent_for_trip()
cache_key = 'PLATFORM_COMMISSION_PERCENT'
cached = cache.get(cache_key)

if cached is not None:
    return cached  # Instant, no DB query

# If cache miss:
setting = PlatformSettings.objects.get(...)
cache.set(cache_key, value, timeout=300)  # Cache 5 minutes
return value
```

**Performance comparison:**

| Operation | Latency | Impact |
|-----------|---------|--------|
| Redis cache hit | ~5ms | INSTANT (no DB) |
| PostgreSQL query | ~50-100ms | OK (but slower) |
| New RateCard lookup | ~20-50ms | Part of normal flow |

**Cache invalidation signal:**

```python
# In PlatformSettings.save()
from django.core.cache import cache

def save(self, *args, **kwargs):
    super().save(*args, **kwargs)
    
    # Invalidate cache on any setting change
    cache.delete('PLATFORM_COMMISSION_PERCENT')
    
    # Log change for audit
    logger.info(
        f'PlatformSettings [{self.key}] changed to {self.value} by {self.updated_by}'
    )
```

---

## SUMMARY

### What Riders See & Do
1. **Estimate fare** → See ₹195.50 (full amount, commission hidden)
2. **Book trip** → Pay ₹195.50
3. **Ride trip** → See location, driver info
4. **Complete trip** → See receipt (no commission breakdown)
5. **Manage wallet** → Top-up, view transactions, pay for rides

### What Drivers See & Do
1. **Receive trip** → Accept within 30 sec (WebSocket real-time)
2. **Pickup rider** → Navigate via GPS
3. **Complete trip** → Confirm end location, distance, duration
4. **See earnings** → View ₹164 (already after commission)
5. **Withdraw** → Payout with 2% fee, 7-day cooldown

### Current Configuration (ENV-based)
- Commission loaded from `.env.local` at Django startup
- Requires server restart to change
- Causes **10-15 minute downtime**
- No audit trail

### Proposed Configuration (DB-based)
- Commission stored in `PlatformSettings` table
- Change via Django admin UI
- **Instant change, zero downtime**
- Redis cache for performance (5-min TTL)
- Full audit trail (who changed, when)
- Better for multi-region, multi-zone operations

### Key Benefits of DB-based Approach
✅ **Zero downtime** for rate changes  
✅ **Instant apply** (vs 15 min wait)  
✅ **Audit trail** (full history)  
✅ **User-friendly** (admin UI)  
✅ **Scalable** (multi-region ready)  
✅ **Safe** (fallback to env)  
✅ **Performant** (Redis cached)
