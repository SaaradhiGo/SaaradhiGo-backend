# Downtime: Explanation & Reduction Strategy for SaaradhiGo

## PART 1: WHAT IS DOWNTIME?

### 1.1 Simple Definition

**Downtime** = The period when a service or system is NOT working or NOT available to users.

During downtime:
- ❌ Riders **cannot** book rides
- ❌ Drivers **cannot** accept trips
- ❌ Payments **cannot** be processed
- ❌ The API returns errors (500, 503, etc.)
- ❌ Database is unreachable
- ❌ Services are restarting or deploying

### 1.2 Types of Downtime

```
DOWNTIME
├─ PLANNED (Scheduled)
│  ├─ Server maintenance windows
│  ├─ Database migrations
│  ├─ Deployments (code updates)
│  └─ Scheduled backups
│
└─ UNPLANNED (Unexpected)
   ├─ Server crashes
   ├─ Database failure
   ├─ Network issues
   ├─ Resource exhaustion (CPU, RAM, disk full)
   ├─ Memory leaks
   └─ External service failure (Redis, Kafka, etc.)
```

### 1.3 Real-World Impact Example

```
Scenario: Django server restart for code deployment

14:00 → Deployment starts
14:05 → Server shuts down (existing requests timeout)
14:10 → Server restarts, loads code
14:15 → Server online, running

⏱️ Downtime: 15 minutes

DURING THIS TIME:
├─ Rider trying to book a trip
│  └─ ❌ Error: "Cannot reach server"
│  └─ 😞 User tries again... gets same error
│  └─ 😤 User switches to Uber
│
├─ Driver in middle of a trip completing it
│  └─ ❌ Payment doesn't process
│  └─ ❌ Earnings not credited
│  └─ ❌ Wallet balance shows old amount
│  └─ 😞 Driver upset (money is missing)
│
└─ Admin dashboard
   └─ ❌ Revenue reports show 0 transactions
   └─ ❌ Can't track live metrics

BUSINESS IMPACT:
├─ Lost bookings: ~100-200 trips × ₹200 avg = ₹20,000-40,000 lost revenue
├─ Driver churn: Drivers may stop accepting rides
├─ Negative reviews: "App doesn't work during peak hours"
├─ Refunds: Some failed payments need manual refunds
└─ Support tickets: Support team flooded with "Why is the app broken?"
```

### 1.4 Downtime vs Latency (Different Things!)

```
LATENCY (Response time)
├─ Good: < 200ms (user doesn't notice)
├─ OK: 200-500ms (user waits but accepts)
├─ Bad: 500ms-2s (user annoyed)
└─ Terrible: > 2s (user will abandon, try competitor)

DOWNTIME (Complete unavailability)
├─ Good: < 1 min per month
├─ OK: < 5 min per week
├─ Bad: > 10 min per deployment
└─ Terrible: > 1 hour per day
```

---

## PART 2: WHAT CAUSES DOWNTIME IN SAARADHI-GO?

### 2.1 Current Downtime Sources (This Project)

#### 1️⃣ Configuration Changes (ENV Variables)

**Current Problem:**
```python
# [base/settings.py] Line 336
PLATFORM_COMMISSION_PERCENT = os.environ.get("PLATFORM_COMMISSION_PERCENT", "18")
# Loaded once at Django startup
# To change: need to restart server
```

**Scenario:**
```
14:00 → Ops wants to change commission: 18% → 20%
        (Maybe surge pricing for New Year, or rate adjustment)

14:05 → Edit .env.local
14:10 → Git commit & push
14:15 → Jenkins deploys to production
14:20 → Stop old server processes
        ⏱️ DOWNTIME STARTS
        └─ Requests to /api/ride/book/ → 503 Service Unavailable
        
14:25 → New server processes start, load Django
14:30 → Server fully online
        ✓ DOWNTIME ENDS
        └─ Requests work again

⏱️ Total downtime: 10 minutes
💰 Impact: 50-100 lost bookings
```

**Why?** Environment variables are read only at Django startup. Changing them requires server restart.

---

#### 2️⃣ Database Migrations

**Current Problem:**
```sql
-- Adding a new column requires altering the table
ALTER TABLE ride_trip ADD COLUMN new_column VARCHAR(255);

-- During ALTER:
-- ✓ PostgreSQL locks the table
-- ✓ No reads, no writes allowed
-- ✓ Queries timeout
-- ⏱️ Downtime until migration completes
```

**Scenario:**
```
14:00 → Need to add 'surcharge_reason' column to Trip model
14:10 → Run: python manage.py migrate
        └─ Migration starts

        PostgreSQL locks ride_trip table
        ⏱️ DOWNTIME STARTS

14:11 → Rider tries to book: "Connection timeout"
        Driver tries to accept trip: "Connection timeout"

14:15 → Migration completes (5 min for 10M rows)
        ✓ Table unlocked
        ✓ DOWNTIME ENDS

⏱️ Total downtime: 5 minutes
💰 Impact: 25-50 lost bookings
```

**Why?** PostgreSQL locks tables during schema changes.

---

#### 3️⃣ Deployment (Code Changes)

**Current Problem:**
```
When deploying new code:
1. Old Python processes killed
2. New code loaded
3. Django initializes
4. Connects to database
5. Loads all models, signals, middleware

During steps 1-5: Server is unavailable
```

**Scenario:**
```
14:00 → Deploy fixed bug in driver earnings calculation
14:10 → Stop gunicorn processes
        ⏱️ DOWNTIME STARTS (0 servers available)
        └─ Load balancer has no healthy backends
        
14:15 → New gunicorn processes start
        └─ Loads Django
        └─ Initializes database connections
        └─ Starts middleware
        
14:18 → Health checks pass
        ✓ DOWNTIME ENDS

⏱️ Total downtime: 8 minutes
💰 Impact: 40-80 lost bookings
```

**Why?** Django needs time to bootstrap. All requests fail while restarting.

---

#### 4️⃣ Database Connection Pool Exhaustion

**Current Problem:**
```python
# [base/settings.py] (Hypothetical)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'CONN_MAX_AGE': 600,  # Connection pooling
    }
}

# If database has 20 connections max, and 21 requests come:
# → 1 request waits for a connection to become available
# → If all 20 are slow queries, timeout occurs
```

**Scenario:**
```
14:00 → Database has 20 connections
14:01 → 30 concurrent trip bookings arrive
        ├─ Connection 1-20: Assigned (executing queries)
        └─ Connection 21-30: WAITING (in queue)

14:02 → Queries 1-10 complete quickly (< 1 sec)
        └─ Connections freed → Waiting queries start
        
14:03 → But queries 11-20 are slow (5+ sec)
        └─ Query timeouts (conn_timeout = 5 sec)
        └─ ❌ Bookings fail

⏱️ Functional downtime: 2+ minutes
💰 Impact: 10-20 failed bookings
```

**Why?** Connection exhaustion due to slow queries or insufficient pool size.

---

#### 5️⃣ Redis Failure

**Current Problem:**
```python
# Redis is used for:
├─ WebSocket connection management (driver dispatch)
├─ Session storage
├─ Caching (rate cards, surge multipliers)
└─ Real-time location streaming

# If Redis crashes:
# ❌ Can't dispatch trips to drivers
# ❌ Session lookups fail
# ❌ Cache misses cause DB overload
```

**Scenario:**
```
14:00 → Redis server stops (out of memory, crashed)
14:01 → Driver doesn't receive new trip notifications
        └─ Riders try to book but drivers don't see requests
        └─ Trips stay in "requested" state (no acceptance)
        
14:02 → Admin tries to login
        └─ Session lookup fails (Redis down)
        └─ ❌ Admin can't access dashboard
        
14:05 → Rate card cache misses
        └─ Every trip lookup queries PostgreSQL
        └─ Database overloaded (30x more queries than normal)
        └─ ❌ Booking API slows to 5+ seconds per request
        
14:10 → Ops restarts Redis
        ✓ Service recovers

⏱️ Downtime: 10 minutes
💰 Impact: ALL riders & drivers affected
```

**Why?** Redis is critical infrastructure. No fallback if it crashes.

---

#### 6️⃣ Memory Leak or Resource Exhaustion

**Current Problem:**
```python
# Example: Memory leak in a middleware
class BadMiddleware(MiddlewareMixin):
    def __init__(self, get_response):
        self.cache = {}  # ← DANGER: Unbounded cache
        self.get_response = get_response
    
    def __call__(self, request):
        # Every request adds to cache, NEVER removes
        self.cache[request.id] = request.data
        return self.get_response(request)

# After 10,000 requests:
# ├─ cache dict has 10,000 items
# ├─ Each item: ~1 KB
# └─ Total: ~10 MB wasted

# After 1 million requests:
# ├─ cache dict has 1 million items
# ├─ Python process using 1+ GB RAM
# ├─ Server runs out of memory
# └─ Process crashes (OOM killed)
```

**Scenario:**
```
Day 1-3 → Deployment with memory leak
Day 4  → After ~100,000 requests
        ├─ Memory: 500 MB → 1 GB
        └─ Performance degrades
        
Day 5  → After ~500,000 requests
        ├─ Memory: 1 GB → 2 GB
        ├─ Garbage collection pauses increase
        └─ API latency jumps to 2+ seconds
        
Day 6  → After ~1,000,000 requests
        ├─ Server runs out of memory
        ├─ Linux OOM killer terminates gunicorn
        └─ ⏱️ DOWNTIME STARTS
        
Day 6  → Ops restarts service
        ✓ DOWNTIME ENDS (but cycle repeats)

⏱️ Downtime: 15+ minutes
💰 Impact: Critical outage
🔁 Pattern: Recurring crashes every 24-48 hours
```

**Why?** Resource leaks cause cascading failures.

---

### 2.2 Summary: Top Downtime Causes in This Project

| Cause | Frequency | Duration | Impact | Preventable |
|-------|-----------|----------|--------|--|
| Config changes (ENV) | Per deployment | 10-15 min | ❌ All bookings fail | ✅ YES (use DB) |
| Database migrations | Weekly | 5-20 min | ❌ Table locked | ✅ YES (online migrations) |
| Code deployments | Daily-3x weekly | 5-10 min | ❌ Server restarts | ✅ YES (blue-green deploy) |
| Connection pool exhaustion | Rare (peak hours) | 2-5 min | ⚠️ Some bookings fail | ✅ YES (monitor pools) |
| Redis failure | Very rare | 5-15 min | ❌ Critical outage | ✅ YES (Redis Sentinel) |
| Memory leaks | Rare (bad code) | 24-48 hours then crash | 🔴 Cascading failure | ✅ YES (profiling) |
| Database CPU spike | Monthly | 5-10 min | ⚠️ Slow queries | ✅ YES (query optimization) |

---

## PART 3: BUSINESS IMPACT OF DOWNTIME

### 3.1 Financial Impact

```
SaaradhiGo Typical Metrics (Estimated):
├─ Peak hours: 200 bookings/minute
├─ Average fare: ₹200
├─ Platform commission: 18%
├─ Revenue per booking: ₹36

15-minute downtime during peak hours:
├─ Lost bookings: 200/min × 15 min = 3,000 trips
├─ Lost revenue: 3,000 × ₹36 = ₹108,000
│
└─ Additional costs:
   ├─ Support tickets: 1,000 "why is app broken?"
   ├─ Support team overtime: ₹5,000
   ├─ Refund processing: ₹10,000
   └─ Total: ₹123,000 loss per 15-min outage
```

### 3.2 Reputation Impact

```
After an outage, on social media:
├─ "SaaradhiGo is broken, I can't book rides"
├─ "Their app crashed during rush hour"
├─ "Switched to Uber, more reliable"
│
└─ Consequences:
   ├─ Negative reviews on app store
   ├─ Reduced app rating (5.0 → 4.5)
   ├─ User churn: 5-10% stop using after outage
   └─ Lost revenue (long-term): ₹500,000+ over 3 months
```

### 3.3 Driver Impact

```
After a payment processing outage:
├─ Driver: "I completed 10 trips, wallet shows ₹0"
├─ Driver: "SaaradhiGo stole my earnings!"
├─ Driver: "I'm quitting, not reliable"
│
└─ Consequences:
   ├─ Drivers don't log in next day
   ├─ Supply shortage (fewer drivers online)
   ├─ Riders can't find drivers for hours
   ├─ Bad rider experience
   └─ Riders switch to competitors
```

---

## PART 4: HOW TO REDUCE DOWNTIME (Strategies)

### 4.1 Reduce Downtime for Configuration Changes

#### Problem (Current):
```
Change commission% → Restart server → 10-15 min downtime
```

#### Solution 1: Database-Based Configuration ✅

```python
# Add PlatformSettings model
class PlatformSettings(models.Model):
    key = models.CharField(max_length=256, unique=True)
    value = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

# Load from DB with Redis cache
def get_platform_setting(key, default=None):
    # Check Redis first (5-min TTL)
    cached = cache.get(f'setting_{key}')
    if cached:
        return cached
    
    # Query database
    try:
        setting = PlatformSettings.objects.get(key=key)
        value = setting.value
        cache.set(f'setting_{key}', value, timeout=300)
        return value
    except:
        return default

# Usage: No server restart needed!
commission = get_platform_setting('PLATFORM_COMMISSION_PERCENT', '18')
```

**Result:**
```
Before: Change config → Deploy → Restart → 10-15 min downtime
After:  Change config → Instant, zero downtime
```

---

#### Solution 2: Feature Flags ✅

```python
# Add FeatureFlag model for A/B testing & gradual rollouts
class FeatureFlag(models.Model):
    name = models.CharField(max_length=256, unique=True)
    is_enabled = models.BooleanField(default=False)
    percentage_rolled_out = models.IntegerField(default=0)  # 0-100%

# Check if feature is enabled
def is_feature_enabled(flag_name, user=None):
    flag = cache.get(f'feature_{flag_name}')
    if flag is None:
        flag = FeatureFlag.objects.get(name=flag_name)
        cache.set(f'feature_{flag_name}', flag, timeout=300)
    
    # For rollout: check if user is in percentage
    if flag.percentage_rolled_out < 100:
        user_hash = hash(user.id) % 100
        return user_hash < flag.percentage_rolled_out
    
    return flag.is_enabled

# Usage: Gradual rollout without restart
if is_feature_enabled('new_algorithm_v2', user=request.user):
    earnings = calculate_earnings_v2(trip)  # New algorithm
else:
    earnings = calculate_earnings_v1(trip)  # Old algorithm
```

**Result:**
```
Before: New feature → Deploy → Restart → 10-15 min downtime → All users
After:  New feature → Deploy → Enable flag → Zero downtime → Gradual rollout

Example: Enable for 10% users → 50% users → 100% users (over 3 days)
```

---

### 4.2 Reduce Downtime for Database Migrations

#### Problem (Current):
```
ALTER TABLE ride_trip ADD COLUMN ... → Table locked → 5-20 min downtime
```

#### Solution 1: Online Migrations (PostgreSQL 11+) ✅

```python
# Old way (BLOCKING):
class Migration(migrations.Migration):
    operations = [
        migrations.AddField(
            model_name='trip',
            name='new_field',
            field=models.CharField(max_length=100),
        ),
    ]
# This locks the table!

# New way (NON-BLOCKING):
class Migration(migrations.Migration):
    operations = [
        migrations.AddField(
            model_name='trip',
            name='new_field',
            field=models.CharField(max_length=100, null=True),
        ),
        # Then optionally make NOT NULL later
    ]

# PostgreSQL 11+ runs this without taking exclusive locks
```

**Result:**
```
Before: ALTER TABLE → Lock → 5-20 min downtime
After:  ALTER TABLE → No lock → Zero downtime
```

---

#### Solution 2: Blue-Green Deployments ✅

```
Database: ride_trip (1 table)
Application: Old code (v1.0) + New code (v2.0)

Step 1: Setup
├─ Production: v1.0 code running (100% traffic)
├─ Staging: v2.0 code deployed (0% traffic)
└─ Database: Updated with migration already applied

Step 2: Gradual Rollout
├─ Send 10% traffic to v2.0
├─ Monitor for errors (metric aggregation)
├─ If OK: Send 50% traffic to v2.0
├─ If OK: Send 100% traffic to v2.0
└─ Rollback available: Switch 100% back to v1.0 instantly

Step 3: Remove Old Code
├─ v1.0 servers can be shutdown
└─ v2.0 is now the only version

⏱️ Downtime: 0 minutes
✓ No server restarts needed
✓ Instant rollback if issues detected
```

**Implementation:**
```nginx
# Load balancer config
upstream v1 {
    server 10.0.1.10:8000;
    server 10.0.1.11:8000;
}

upstream v2 {
    server 10.0.2.10:8000;
    server 10.0.2.11:8000;
}

# Start: 90% v1, 10% v2
server {
    listen 80;
    location /api {
        if ($request_uri ~ "^/api/") {
            # 90% to v1
            if ($random > 0.1) {
                proxy_pass http://v1;
            }
            # 10% to v2
            else {
                proxy_pass http://v2;
            }
        }
    }
}

# After monitoring: 50% v1, 50% v2
# After more monitoring: 0% v1, 100% v2
```

**Result:**
```
Before: Deploy code → Restart all servers → 5-10 min downtime
After:  Deploy code → Gradual rollout → Zero downtime
```

---

### 4.3 Reduce Downtime for Database Connection Issues

#### Problem (Current):
```
Max connections: 20
Concurrent requests: 30
→ 10 requests timeout
→ Bookings fail
```

#### Solution 1: Connection Pooling ✅

```python
# Use PgBouncer or pgpool for connection pooling

# Old: Django ↔ PostgreSQL (direct, limited connections)
# New: Django ↔ PgBouncer ↔ PostgreSQL (pooled, unlimited)

# [pgbouncer.ini]
[databases]
postgres = host=localhost port=5432 dbname=postgres

[pgbouncer]
pool_mode = transaction  # Return connection after each transaction
max_client_conn = 500    # Accept up to 500 client connections
default_pool_size = 20   # Maintain 20 connections to PostgreSQL
reserve_pool_size = 5    # Keep 5 spare connections
reserve_pool_timeout = 3 # Timeout waiting for spare conn
```

**Result:**
```
Before: 20 max connections → Some requests timeout
After:  500 client connections pooled → No timeouts
        (500 clients sharing 20 DB connections)
```

---

#### Solution 2: Read Replicas ✅

```python
# Most queries are reads (80%), only writes need primary

# Architecture:
#   Read-heavy queries (fare estimates, trip history) → Replica
#   Write queries (create trip, payment) → Primary

# [Django settings]
DATABASES = {
    'default': {  # PRIMARY
        'ENGINE': 'django.db.backends.postgresql',
        'HOST': 'primary.db.internal',
        'NAME': 'saaradhi',
    },
    'readonly': {  # REPLICA
        'ENGINE': 'django.db.backends.postgresql',
        'HOST': 'replica.db.internal',
        'NAME': 'saaradhi',
    }
}

# Usage
from django.db import connections

def estimate_fare(pickup, destination, vehicle_type):
    # Read from replica (faster, no blocking writes)
    rate_card = (
        RateCard.objects
        .using('readonly')  # ← Use replica
        .get(zone=zone, vehicle_type=vehicle_type)
    )
    return calculate_fare(rate_card, pickup, destination)

def book_trip(user, pickup, destination, vehicle_type):
    # Write to primary (only source of truth)
    trip = Trip.objects.create(  # Default uses 'default'
        user_id=user,
        pickup_lat=pickup['lat'],
        # ...
    )
    return trip
```

**Result:**
```
Before: All queries compete for primary connections (40% slow)
After:  Read queries use replica → Primary freed up (0% slow)
        Read latency: 500ms → 50ms
```

---

### 4.4 Reduce Downtime for Redis Failures

#### Problem (Current):
```
Redis crashes → No driver dispatch → No trip acceptance → Severe outage
```

#### Solution 1: Redis Sentinel (High Availability) ✅

```
Redis Sentinel is a Redis deployment with automatic failover

Architecture:
┌─────────────────────────────────────┐
│ Redis Master (primary)              │
│ - Accepts reads & writes            │
│ - Syncs to replicas                 │
└─────────────────────────────────────┘
           ↓      ↓
┌──────────────────────────────────────────┐
│ Redis Replica 1        Redis Replica 2   │
│ - Read-only copy       - Read-only copy  │
└──────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│ Sentinel Monitors (3 nodes)              │
│ - Watches master health every 1 sec      │
│ - If master down → Promote replica       │
│ - Notifies clients of new master         │
└──────────────────────────────────────────┘

When master crashes:
├─ 1s: Sentinel detects failure
├─ 2s: Sentinel elects new master from replicas
├─ 3s: Applications reconnect to new master
└─ Total downtime: ~3 seconds (vs 10+ minutes without Sentinel)
```

**Setup:**
```python
# [docker-compose.yml]
version: '3'
services:
  redis-master:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - ./redis-master.conf:/usr/local/etc/redis/redis.conf
    command: redis-server /usr/local/etc/redis/redis.conf

  redis-replica-1:
    image: redis:7-alpine
    ports:
      - "6380:6379"
    command: redis-server --slaveof redis-master 6379

  redis-replica-2:
    image: redis:7-alpine
    ports:
      - "6381:6379"
    command: redis-server --slaveof redis-master 6379

  sentinel-1:
    image: redis:7-alpine
    ports:
      - "26379:26379"
    volumes:
      - ./sentinel.conf:/usr/local/etc/sentinel.conf
    command: redis-sentinel /usr/local/etc/sentinel.conf

  sentinel-2:
    image: redis:7-alpine
    ports:
      - "26380:26379"
    volumes:
      - ./sentinel.conf:/usr/local/etc/sentinel.conf
    command: redis-sentinel /usr/local/etc/sentinel.conf

  sentinel-3:
    image: redis:7-alpine
    ports:
      - "26381:26379"
    volumes:
      - ./sentinel.conf:/usr/local/etc/sentinel.conf
    command: redis-sentinel /usr/local/etc/sentinel.conf
```

```conf
# [sentinel.conf]
port 26379
sentinel monitor mymaster redis-master 6379 2
sentinel down-after-milliseconds mymaster 5000
sentinel parallel-syncs mymaster 1
sentinel failover-timeout mymaster 10000
```

**Python client:**
```python
from redis.sentinel import Sentinel

# Connect to Sentinel (not Redis directly)
sentinel = Sentinel([
    ('sentinel-1', 26379),
    ('sentinel-2', 26379),
    ('sentinel-3', 26379),
])

# Get master connection (Sentinel handles failover)
redis_master = sentinel.master_for('mymaster', socket_timeout=0.1)
redis_replica = sentinel.slave_for('mymaster', socket_timeout=0.1)

# Automatic failover on master crash
redis_master.set('key', 'value')  # Automatically uses new master if old one crashes
```

**Result:**
```
Before: Redis master crashes → Outage until manual restart (10+ min)
After:  Redis master crashes → Automatic failover to replica (3 sec)
        Users barely notice (~1-2 error messages max)
```

---

#### Solution 2: Circuit Breaker Pattern (Graceful Degradation) ✅

```python
from circuitbreaker import circuit

class RedisCircuitBreaker:
    def __init__(self):
        self.failures = 0
        self.last_failure_time = None
        self.is_open = False
    
    def call(self, func, *args, **kwargs):
        if self.is_open:
            # Check if enough time passed to retry
            if time.time() - self.last_failure_time > 60:  # Retry after 60 sec
                self.is_open = False
                self.failures = 0
            else:
                # Circuit still open, fail fast
                raise CircuitBreakerOpen(f"Redis circuit open for {time.time() - self.last_failure_time:.1f}s")
        
        try:
            return func(*args, **kwargs)
        except Exception as e:
            self.failures += 1
            if self.failures >= 3:  # Open circuit after 3 failures
                self.is_open = True
                self.last_failure_time = time.time()
            raise

redis_cb = RedisCircuitBreaker()

def get_from_cache_with_fallback(key, fallback_func):
    """Try to get from Redis, fallback to function if Redis is down."""
    try:
        # Try Redis (with circuit breaker)
        value = redis_cb.call(redis_client.get, key)
        if value:
            return value
    except (CircuitBreakerOpen, ConnectionError, TimeoutError) as e:
        logger.warning(f"Redis failed, using fallback: {e}")
    
    # Redis is down, use fallback (e.g., query database)
    return fallback_func()

# Usage
def estimate_fare_with_fallback(pickup, destination, vehicle_type):
    # Try cache first
    cache_key = f"fare:{vehicle_type}:{pickup}:{destination}"
    
    # If Redis fails, query database directly
    return get_from_cache_with_fallback(
        cache_key,
        fallback_func=lambda: RateCard.objects.get(
            zone=zone,
            vehicle_type=vehicle_type
        ).calculate_fare(pickup, destination)
    )
```

**Result:**
```
Before: Redis crashes → Driver dispatch fails → All trips affected
After:  Redis crashes → Circuit breaker → Fall back to database
        - Some latency increase (500ms vs 50ms)
        - But service continues working
        - Users don't see errors
```

---

### 4.5 Reduce Downtime from Code Defects (Memory Leaks)

#### Solution 1: Continuous Profiling ✅

```python
# Add memory profiling to detect leaks early

from memory_profiler import profile
from pympler import tracker

# Detect memory leaks during development
tr = tracker.SummaryTracker()

@profile
def problematic_function():
    # This function leaks memory
    cache = {}
    for i in range(1000000):
        cache[i] = [0] * 1000  # Unbounded growth
    return cache

# Run and analyze
tr.print_diff()  # Shows memory allocation changes
```

**Result:**
```
Before: Memory leak → Discovered after 5+ days → Users affected
After:  Memory leak → Detected in CI/CD → Fixed before production
```

---

#### Solution 2: Auto-restart on Memory Threshold ✅

```python
# Graceful restart before OOM crash

import psutil
import os
import signal

def check_memory_and_restart():
    """Monitor memory, graceful restart if exceeds threshold."""
    process = psutil.Process(os.getpid())
    memory_percent = process.memory_percent()
    
    # If using > 80% of allowed memory
    if memory_percent > 80:
        logger.warning(f"Memory usage high: {memory_percent}%")
        
        # Graceful shutdown (finish existing requests)
        os.kill(os.getpid(), signal.SIGTERM)
        
        # Kubernetes will detect pod death and restart it
        # (with zero downtime due to load balancer & multiple replicas)

# Run every 10 seconds
from celery.beat import schedule
from celery import shared_task

@shared_task
def monitor_memory():
    check_memory_and_restart()

# celery beat schedule
CELERY_BEAT_SCHEDULE = {
    'monitor-memory': {
        'task': 'base.tasks.monitor_memory',
        'schedule': schedule(run_every=10),
    },
}
```

**Result:**
```
Before: Memory leak → Process OOM killed → 5+ min downtime
After:  Memory leak → Graceful restart → < 10 sec downtime (if using K8s)
        (Load balancer detects pod death, routes to healthy replicas)
```

---

### 4.6 Reduce Downtime from Network Issues

#### Solution: Retry Logic with Exponential Backoff ✅

```python
import time
from functools import wraps

def retry_on_failure(max_attempts=3, backoff=2):
    """Retry with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            last_exception = None
            
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    last_exception = e
                    
                    if attempt >= max_attempts:
                        raise
                    
                    # Wait: 1s, 2s, 4s, 8s...
                    wait_time = backoff ** (attempt - 1)
                    logger.warning(
                        f"Attempt {attempt} failed, retrying in {wait_time}s: {e}"
                    )
                    time.sleep(wait_time)
            
            raise last_exception
        return wrapper
    return decorator

@retry_on_failure(max_attempts=3, backoff=2)
def call_payment_gateway(trip, amount):
    """Retry payment up to 3 times."""
    response = requests.post(
        'https://api.cashfree.com/process',
        json={'trip_id': trip.id, 'amount': amount},
        timeout=10
    )
    return response.json()

# Usage
try:
    result = call_payment_gateway(trip, 200)
    payment.status = 'completed'
except Exception as e:
    payment.status = 'failed'
    logger.error(f"Payment failed after retries: {e}")
```

**Result:**
```
Before: Network blip → Payment fails immediately → User sees error
After:  Network blip → Auto-retry 3 times → Usually succeeds
        (Only fails if persistent network issue)
```

---

## PART 5: DOWNTIME REDUCTION ROADMAP FOR SAARADHI-GO

### Phase 1 (Immediate - Week 1-2)

| Task | Impact | Effort | Priority |
|------|--------|--------|----------|
| Implement PlatformSettings (DB config) | Eliminates config restart downtime | Low | 🔴 Critical |
| Add Redis Sentinel | Prevents Redis failure outages | Medium | 🔴 Critical |
| Add connection pooling (PgBouncer) | Prevents connection exhaustion | Low | 🟡 High |
| Add basic monitoring/alerting | Early warning of issues | Low | 🟡 High |

---

### Phase 2 (Short-term - Week 3-4)

| Task | Impact | Effort | Priority |
|------|--------|--------|----------|
| Implement feature flags | Enables A/B testing without restart | Medium | 🟡 High |
| Setup read replicas | Reduces query load on primary | High | 🟡 High |
| Add circuit breaker pattern | Graceful degradation on failures | Medium | 🟡 High |
| Optimize slow queries | Reduces lock contention | High | 🟡 High |

---

### Phase 3 (Medium-term - Month 2)

| Task | Impact | Effort | Priority |
|------|--------|--------|----------|
| Setup blue-green deployments | Zero-downtime code deployments | High | 🟢 Medium |
| Implement memory profiling in CI | Detects leaks before production | Medium | 🟢 Medium |
| Add comprehensive logging/tracing | Faster debugging | Medium | 🟢 Medium |

---

### Phase 4 (Long-term - Month 3+)

| Task | Impact | Effort | Priority |
|------|--------|--------|----------|
| Kubernetes deployment | Auto-recovery from failures | Very High | 🟢 Medium |
| Distributed tracing (Jaeger) | Understand request flow | High | 🟢 Medium |
| Canary deployments | Safer code rollouts | High | 🟢 Medium |

---

## PART 6: MEASURING DOWNTIME

### 6.1 Key Metrics

```
AVAILABILITY = (Total Time - Downtime) / Total Time × 100%

99.99% uptime (four nines):
├─ Allowed downtime per year: 52.6 minutes
├─ Allowed downtime per month: 4.3 minutes
├─ Allowed downtime per week: 1 minute
└─ Very high availability (enterprise grade)

99.9% uptime (three nines):
├─ Allowed downtime per year: 8.76 hours
├─ Allowed downtime per month: 43 minutes
├─ Allowed downtime per week: 10 minutes
└─ Industry standard for SaaS

95% uptime:
├─ Allowed downtime per year: 18.25 days
├─ Allowed downtime per month: 1.5 days
├─ Allowed downtime per week: 3.5 hours
└─ Not acceptable for ride-sharing
```

### 6.2 Monitoring Implementation

```python
# [monitoring/uptime.py]

import time
from django.db import connection
from django.core.cache import cache
import requests

class UptimeMonitor:
    def __init__(self):
        self.downtime_start = None
        self.total_downtime = 0
    
    def health_check(self):
        """Check if all critical services are working."""
        checks = {
            'api': self.check_api(),
            'database': self.check_database(),
            'redis': self.check_redis(),
            'payment_gateway': self.check_payment_gateway(),
        }
        
        all_healthy = all(checks.values())
        
        if not all_healthy and self.downtime_start is None:
            self.downtime_start = time.time()
            logger.error(f"Downtime detected: {checks}")
        
        if all_healthy and self.downtime_start is not None:
            duration = time.time() - self.downtime_start
            self.total_downtime += duration
            logger.info(f"Service recovered after {duration:.1f}s downtime")
            self.downtime_start = None
        
        return all_healthy
    
    def check_api(self):
        try:
            response = requests.get('http://localhost:8000/health/', timeout=2)
            return response.status_code == 200
        except:
            return False
    
    def check_database(self):
        try:
            connection.ensure_connection()
            return True
        except:
            return False
    
    def check_redis(self):
        try:
            cache.set('health', 'ok', timeout=10)
            return cache.get('health') == 'ok'
        except:
            return False
    
    def check_payment_gateway(self):
        try:
            response = requests.get(
                'https://api.cashfree.com/health',
                timeout=5
            )
            return response.status_code == 200
        except:
            return False
    
    def get_uptime_percentage(self):
        total_time = 86400 * 30  # 30 days in seconds
        uptime = total_time - self.total_downtime
        return (uptime / total_time) * 100

# Run health check every 10 seconds
from celery.beat import schedule

CELERY_BEAT_SCHEDULE = {
    'health-check': {
        'task': 'monitoring.uptime.check_health',
        'schedule': schedule(run_every=10),
    },
}
```

---

## PART 7: SUMMARY & RECOMMENDATIONS

### What is Downtime?
✅ The period when service is **unavailable to users**
- Riders can't book
- Drivers can't accept trips
- Payments fail
- Data is inaccessible

### Current Downtime Sources
1. **Config changes (ENV variables)** → 10-15 min per change
2. **Database migrations** → 5-20 min per migration
3. **Code deployments** → 5-10 min per deploy
4. **Resource exhaustion** → 2-5 min per incident
5. **Redis failures** → 10+ min per outage
6. **Memory leaks** → Crash after 24-48 hours

### Business Impact
- **Financial**: ₹100,000+ lost per outage
- **Reputation**: App rating drops, users churn
- **Drivers**: Earnings shown as missing, reduced trust

### Top Recommendations (Priority)

| Priority | Recommendation | Benefit | Effort |
|----------|---|---|---|
| 🔴 CRITICAL | Use DB for config (not ENV) | Eliminate restart downtime | Low |
| 🔴 CRITICAL | Redis Sentinel (HA) | Automatic failover in 3 sec | Medium |
| 🔴 CRITICAL | Connection pooling (PgBouncer) | Prevent connection exhaustion | Low |
| 🟡 HIGH | Feature flags | A/B testing without restart | Medium |
| 🟡 HIGH | Circuit breaker pattern | Graceful degradation | Medium |
| 🟢 MEDIUM | Blue-green deployments | Zero-downtime code updates | High |
| 🟢 MEDIUM | Read replicas | Reduce primary load | High |

### Target SLA
```
Current: 95% uptime (~18 hours downtime/year)
Target:  99.9% uptime (~9 hours downtime/year)  [3 nines]
Stretch: 99.99% uptime (~50 minutes downtime/year)  [4 nines - enterprise]
```

---

**Document Version**: 1.0  
**Created**: 2024  
**Status**: Complete  
**Next Steps**: Start Phase 1 implementation immediately
