# SaaradhiGo Backend (VahanGo)

Django + Django Channels backend that powers the SaaradhiGo / VahanGo ride-hailing platform. Serves the rider Flutter app ([SaaradhiGo-mobile](../SaaradhiGo-mobile)) and the web/native clients ([SaaradhiGo-web](../SaaradhiGo-web)).

- **Branch:** `dev` (deployed automatically to EC2 — see `.github/workflows/deploy.yml`)
- **Base URL (dev):** `https://dev.api.saaradhigo.in`
- **API prefix:** `/api/v1/`
- **Admin:** `/admin/`

## Stack

- Python 3.12, Django 5.0, Django REST Framework, SimpleJWT
- Django Channels 4 + Daphne for WebSockets, `channels_redis` channel layer
- Celery 5 (Redis broker) for background tasks
- PostgreSQL (production) / SQLite (default local fallback)
- Redis 7 — broker, cache, channel layer, and driver spatial index
- Cashfree PG (`cashfree-pg`) for payments and payouts; Razorpay settings retained
- AWS S3 via `django-storages` for media; AWS SNS for OTP delivery
- Firebase Admin for FCM push notifications
- Docker / Docker Compose for local dev and production

## Project structure

```
.
├── base/                       # Django project (settings, ASGI, Celery, middleware, storage)
│   ├── settings.py
│   ├── asgi.py                 # ProtocolTypeRouter: HTTP + WebSocket with JWT auth
│   ├── celery.py
│   ├── urls.py                 # /admin/, /api/v1/
│   ├── middleware.py           # ExceptionHandlingMiddleware
│   ├── storage_backends.py     # S3 storage classes
│   └── ...
├── servers/                    # Domain apps mounted under /api/v1/
│   ├── urls.py                 # auth/, rider/, driver/, ride/, payments/
│   ├── routing.py              # WebSocket routes (ws/driver/location, ws/ride/*)
│   ├── consumers.py            # DriverLocation / RideRequest / TripStatus consumers
│   ├── ws_middleware.py        # JWTAuthMiddleware for WebSockets
│   ├── redis_client.py
│   ├── auth_user/              # CustomUser, OTP, login, profile, admin
│   ├── rider/                  # favorites, nearby drivers, notifications, wallet
│   ├── driver/                 # profile, vehicles, earnings, withdrawals, admin
│   ├── ride/                   # fare estimation, trips, ratings, admin live map
│   ├── payments/               # orders, verify, webhooks, refunds, retries, gateways/
│   └── support/                # (registered later)
├── tests/                      # pytest suite (auth, ride, payments, driver, settlement, ...)
├── docker-compose.yml          # base (django + celery)
├── docker-compose.override.yml # local dev (postgres, redis, runserver)
├── docker-compose.prod.yml     # prod (daphne + redis, env from .env.prod)
├── dockerfile
├── requirements.txt
├── manage.py
├── API_Documentation_V2.md     # exhaustive REST API reference
└── WebSocket_Documentation.md  # WebSocket event flows
```

## REST API surface

Mounted under `/api/v1/` (see [base/urls.py](base/urls.py), [servers/urls.py](servers/urls.py)).

**Auth** (`/auth/`)
- `POST /otp/`, `POST /login/`, `POST /refresh/`
- `POST /update/`, `GET /profile/`
- `GET /admin/users/`

**Rider** (`/rider/`)
- Favorites: `POST /locations/`, `GET /locations/all/`, `DELETE /locations/{id}/delete/`
- `GET /nearby/`
- Notifications: `GET /notifications/`, `POST /notifications/{id}/read/`, `POST /notifications/read-all/`
- Wallet: `GET /wallet/balance/`, `POST /wallet/create-order/`, `POST /wallet/verify/`, `GET /wallet/transactions/`, `POST /wallet/payment/`

**Driver** (`/driver/`)
- Profile: `POST /driver/update/`, `GET /driver/profile/`
- Earnings: `GET /earnings/`, `GET /earnings/summary/`
- Withdrawals: `GET /withdrawals/balance/`, `POST /withdrawals/request/`, `GET /withdrawals/history/`, `GET /withdrawals/block-status/`
- Vehicles: `GET /vehicles/`, `POST /vehicles/add/`, `PATCH /vehicles/{id}/`, `DELETE /vehicles/{id}/delete/`
- Admin: `/admin/`, `/admin/{driver_id}/`, KYC + delete + vehicle details, `/admin/withdrawals/...`

**Ride** (`/ride/`)
- `POST /estimate-fare/`, `GET /ride-history/`, `GET /driver-history/`
- `GET /active/`, `GET /trip/{trip_id}/`, `GET /trip/{trip_id}/details/`, `POST /rate-trip/`
- Admin: `/admin/trips/`, `/admin/live-locations/`

**Payments** (`/payments/`)
- `POST /create-order/`, `POST /verify/`, `POST /webhook/`, `POST /payout-webhook/`
- `GET /history/`, `POST /refund/`, `POST /switch/`, `POST /retry/`, `GET /pending/`
- Admin: `/admin/payments/`, `/admin/transactions/`

Full request/response samples live in [API_Documentation_V2.md](API_Documentation_V2.md).

## WebSocket surface

Mounted at the server root (see [servers/routing.py](servers/routing.py)). All connections must include `?token=<JWT>` — auth is handled by [`JWTAuthMiddleware`](servers/ws_middleware.py).

- `ws/driver/location/?token=<JWT>&lat=<lat>&lng=<lng>` — driver online + live location + incoming ride requests
- `ws/ride/request/?token=<JWT>` — rider initiates ride, retries, receives matching events and live driver location
- `ws/ride/trip/<trip_id>/?token=<JWT>` — bidirectional trip state machine (`accept`/`reached`/`start`+OTP/`complete`/`cancel`)

Full event payloads and sequences live in [WebSocket_Documentation.md](WebSocket_Documentation.md).

## Configuration

Settings load environment variables via `python-dotenv` (see [base/settings.py](base/settings.py)). The compose files expect `.env.local` (dev) or `.env.prod`. Notable variables:

| Variable                       | Purpose                                                     |
| ------------------------------ | ----------------------------------------------------------- |
| `DJANGO_SECRET_KEY`            | Django secret                                               |
| `DEBUG_ENV`                    | `"True"` / `"False"`                                        |
| `ALLOWED_HOSTS`                | Comma-separated hosts                                       |
| `CSRF_TRUSTED_ORIGINS`         | Comma-separated trusted origins (prod)                      |
| `REDIS_URL`                    | Redis URL (defaults to `redis://redis:6379`)                |
| `DB_HOST`/`DB_NAME`/`DB_USER`/`DB_PASSWORD`/`DB_PORT` | Postgres connection (falls back to SQLite if unset) |
| `DB_SSLMODE`, `DB_SSLROOTCERT` | Postgres SSL                                                |
| `FRONTEND_URL`, `BACKEND_URL`  | App URLs used in emails / links                             |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `AWS_SNS_SENDER_ID` | AWS creds (SNS for OTP) |
| `AWS_S3_BUCKET_NAME`, `AWS_S3_REGION`, `AWS_QUERYSTRING_EXPIRE` | S3 media storage              |
| `CASHFREE_APP_ID`, `CASHFREE_SECRET_KEY`, `CASHFREE_WEBHOOK_SECRET`, `CASHFREE_ENVIRONMENT`, `CASHFREE_PG_BASE_URL`, `CASHFREE_API_VERSION` | Cashfree gateway |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | Razorpay (legacy)                       |
| `PAYMENT_GATEWAY`, `PAYOUT_GATEWAY` | Active gateways (default `cashfree`)                   |
| `PLATFORM_COMMISSION_PERCENT`  | Trip commission                                             |
| `TRIP_ACCEPT_TIMEOUT_SECONDS`  | Driver accept window (default `600`)                        |
| `GOOGLE_MAPS_API_KEY`          | Maps API key                                                |

Custom user model: `auth_user.customUser`. Timezone: `Asia/Kolkata`. JWT lifetimes: access 1 day, refresh 7 days.

## Running locally (Docker)

```bash
cp .env.example .env.local           # then fill in the variables above
docker compose up --build            # uses docker-compose.yml + docker-compose.override.yml
```

This brings up `django` (runserver on `0.0.0.0:8000`), `celery`, `redis`, and `db` (postgres). Migrations and `collectstatic` run automatically on container start.

Useful one-off commands:

```bash
docker compose exec django python manage.py createsuperuser
docker compose exec django python manage.py makemigrations
docker compose exec django python manage.py migrate
docker compose exec django python manage.py shell
```

## Running locally (host Python)

```bash
python -m venv .venv && . .venv/Scripts/activate    # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Ensure Redis is running locally; create .env with the variables above
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
# In another shell:
celery -A base worker -l INFO
```

For WebSocket support in production, the prod compose runs Daphne directly:

```bash
daphne -b 0.0.0.0 -p 8000 base.asgi:application
```

## Testing

```bash
# inside the django container or an activated venv
python manage.py test
# or, with pytest if configured:
pytest tests/
```

Existing test modules: `test_auth`, `test_rider`, `test_driver`, `test_driver_admin`, `test_driver_logic`, `test_ride`, `test_payments`, `test_payments_advanced`, `test_settlement`, `test_upload_integration`, `test_user_login`.

## Deployment

`.github/workflows/deploy.yml` deploys every push to `dev`:

1. SSH into the EC2 host using `EC2_SSH_KEY` / `EC2_USER` / `EC2_HOST` repo secrets
2. `git pull origin dev` in `/home/ubuntu/SaaradhiGo-backend`
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`

The prod stack runs Django via Daphne (ASGI) and a Celery worker, both reading `.env.prod`, fronted by Redis.

## Branching

- `dev` — integration + auto-deploys to dev environment
- `main` — production / release
- `srini_fixes` — developer-specific working branch

## Related repositories

- [SaaradhiGo-mobile](../SaaradhiGo-mobile) — Flutter rider app (VahanGo)
- [SaaradhiGo-web](../SaaradhiGo-web) — Web client + branch protection rules
