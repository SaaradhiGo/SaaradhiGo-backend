
from pathlib import Path
import os
import logging
logger = logging.getLogger(__name__)
from dotenv import load_dotenv
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(".env.local")

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
REDIS_URL=os.environ.get('REDIS_URL','redis://redis:6379')
# SECURITY WARNING: don't run with debug turned on in production!
DEBUG_ENV=os.environ.get('DEBUG_ENV','False')
DEBUG = DEBUG_ENV=='True'

# ALLOWED_HOSTS: wildcard is allowed in DEBUG only. A production deployment
# without the env var would otherwise accept any Host header, enabling cache
# poisoning and host-header injection. Fail fast at boot.
_allowed_hosts_raw = os.environ.get('ALLOWED_HOSTS', '*' if DEBUG else '')
ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_raw.split(',') if h.strip()]
if not DEBUG and not ALLOWED_HOSTS:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS must be set (comma-separated) when DEBUG=False."
    )

# CSRF trusted origins for any browser session (admin web).
_csrf_raw = os.environ.get('CSRF_TRUSTED_ORIGINS', '')
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_raw.split(',') if o.strip()]


# Application definition

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'channels',
    'storages',
    'servers.auth_user',
    'servers.rider',
    'servers.driver',
    'servers.ride',
    'servers.payments',
    'servers.admin_audit',
    'servers.sos',
    'servers.pricing',
    'servers.support',
    'django_cleanup.apps.CleanupConfig',

]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    "whitenoise.middleware.WhiteNoiseMiddleware",
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'base.middleware.ExceptionHandlingMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    
    
]

ROOT_URLCONF = 'base.urls'
# JWT-only API authentication. SessionAuthentication is intentionally absent —
# our clients (mobile, admin web) authenticate with bearer tokens; keeping
# session auth in the default list would let a CSRF on a logged-in browser
# session (Django admin) authorise DRF write calls.
#
# SessionAuthentication is deliberately NOT in this list. Our clients
# (mobile, ops console) are bearer-token clients. Leaving session auth in
# the defaults meant a logged-in Django-admin browser session could be
# driven by CSRF into authorising DRF writes — approving KYC, refunding a
# trip, releasing a payout. Tests authenticate with `force_authenticate`
# or a real JWT instead.
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.LimitOffsetPagination',
    'PAGE_SIZE': 20,
    # Rates are defined here; the actual throttle classes are wired per-view
    # (see base/throttles.py + decorators on auth endpoints) so we don't
    # accidentally rate-limit unrelated endpoints.
    # DRF's parse_rate only understands periods starting with s/m/h/d.
    # '1/30sec' silently parsed as period[0]='3' and raised KeyError on every
    # call. The closest valid expression of "anti-spam burst" is 2/min
    # (sliding window: never more than 2 in any 60-second period). Not
    # exactly 1-per-30s but close enough for Phase-0; if we want strict
    # 30s burst we'll write a custom throttle class.
    'DEFAULT_THROTTLE_RATES': {
        'otp_request': '5/hour',
        'otp_request_burst': '2/min',
        'otp_verify': '10/hour',
    },
}
from datetime import timedelta
# JWT lifetimes match the system-design doc:
#   - Access:  15 min (short window; minimises blast radius of a leaked token)
#   - Refresh: 14 days (rotating; old refresh blacklisted after rotation)
# Rotation + blacklist together mean a stolen refresh token is single-use:
# whichever client uses it first invalidates it for the other.
#
# Tokens are signed with a key distinct from SECRET_KEY. SECRET_KEY also
# derives password-reset tokens, session signing and CSRF secrets; a leak
# of one should not mint API tokens for every user. Falls back to
# SECRET_KEY only so an existing deploy keeps booting — set
# JWT_SIGNING_KEY in prod (rotating it logs everyone out once).
JWT_SIGNING_KEY = os.environ.get('JWT_SIGNING_KEY') or SECRET_KEY
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=14),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': JWT_SIGNING_KEY,
    'ISSUER': os.environ.get('JWT_ISSUER', 'saaradhigo'),
}
# celery
CELERY_BROKER_URL = REDIS_URL + '/0'

# Celery Beat — periodic tasks.
# The compose `celery` service runs with `-B` so beat is embedded in the
# worker. That's fine while we run a single celery instance; when we
# scale to multiple workers, beat must move to a standalone container
# (or adopt django-celery-beat for a DB-backed schedule lock).
from celery.schedules import crontab as _crontab
CELERY_BEAT_SCHEDULE = {
    'driver-block-expired-licenses-daily': {
        'task': 'driver.block_expired_driver_licenses',
        # 02:00 in TIME_ZONE (Asia/Kolkata) — quiet hours.
        'schedule': _crontab(hour=2, minute=0),
    },
    'payments-reconcile-stuck-every-5-min': {
        # Sweep payments/wallets stuck in pending/processing against the
        # gateway so a missed webhook can never strand money on its own.
        'task': 'payments.reconcile_stuck_payments',
        'schedule': _crontab(minute='*/5'),
    },
    'withdrawals-reconcile-stuck-every-10-min': {
        # Sweep withdrawals stuck in processing/approved. Lower cadence
        # than payments (10 vs 5 min) — driver payouts aren't on the hot
        # rider-facing path so a slightly longer window is fine.
        'task': 'payments.reconcile_stuck_withdrawals',
        'schedule': _crontab(minute='*/10'),
    },
    'driver-presence-sweep-every-minute': {
        # Evict drivers whose heartbeat lapsed (app killed, worker crashed)
        # from the geo index. Heartbeat TTL is 45s, so a ghost is matchable
        # for at most ~105s.
        'task': 'driver.sweep_stale_driver_presence',
        'schedule': 60.0,
    },
}
CELERY_TIMEZONE = 'Asia/Kolkata'
# cache
CACHES={
    'default':{
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL+'/1',
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5
        }
    },
    
}
# CORS allowlist: wildcard in DEBUG only. In production, set
# CORS_ALLOWED_ORIGINS to a comma-separated list of trusted origins
# (admin web, marketing site). Empty + non-DEBUG = no cross-origin access.
_cors_raw = os.environ.get('CORS_ALLOWED_ORIGINS', '')
CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_raw.split(',') if o.strip()]
CORS_ALLOW_ALL_ORIGINS = DEBUG and not CORS_ALLOWED_ORIGINS

# Allow local development on arbitrary ports (like Flutter web).
# DEBUG-only: in production this list must stay empty, otherwise any page
# served from a developer's localhost can read authenticated API responses.
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^http://localhost:\d+$",
    r"^http://127\.0\.0\.1:\d+$",
]

# Production security headers. No-ops in DEBUG.
if not DEBUG:
    # Tell Django the upstream proxy/ALB terminates TLS.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

    # SSL redirect is gated by an env var because Django-level redirect requires
    # nginx (or the ALB) to forward X-Forwarded-Proto. If the proxy doesn't,
    # Django sees every request as HTTP and issues a 301 to itself — infinite
    # redirect loop. Default OFF so a fresh deploy can never lock itself out;
    # operator opts in with DJANGO_SECURE_SSL_REDIRECT=True after confirming
    # nginx sets `proxy_set_header X-Forwarded-Proto $scheme;`.
    SECURE_SSL_REDIRECT = os.environ.get('DJANGO_SECURE_SSL_REDIRECT', 'False') == 'True'

    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    # HSTS is also opt-in via env: once a browser has seen HSTS, it will
    # refuse plain-HTTP to this host for SECURE_HSTS_SECONDS — a wrong
    # default here is hard to undo.
    SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', '0'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
    SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0

    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
    X_FRAME_OPTIONS = 'DENY'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR/'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# --- Test phone numbers (QA bypass for OTP delivery) -------------------------
# Maps phone number → fixed OTP. Listed phones:
#   - never trigger an AWS SNS send
#   - never count against the OTP-issue / OTP-verify throttles
#   - log in with the configured OTP every time
#
# Set via env, comma-separated `phone:otp` pairs. Empty by default so
# production never has a bypass unless the operator opts in. Example:
#   TEST_PHONE_NUMBERS="+919999000001:100001,+919999000002:100002"
#
# Anyone with the env var contents can authenticate as those phones —
# treat them like shared test credentials. Don't reuse a real user's
# phone here.
TEST_PHONE_NUMBERS = {}
_test_phones_raw = os.environ.get('TEST_PHONE_NUMBERS', '')
if _test_phones_raw:
    for _entry in _test_phones_raw.split(','):
        if ':' in _entry:
            _phone, _otp = _entry.split(':', 1)
            _phone, _otp = _phone.strip(), _otp.strip()
            if _phone and _otp:
                TEST_PHONE_NUMBERS[_phone] = _otp

logger.info(f"Loaded {len(TEST_PHONE_NUMBERS)} test phone numbers for OTP bypass.")
#URLS
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
BACKEND_URL  = os.environ.get("BACKEND_URL", "http://localhost:8000")

# --- Wallet / Credits posture ------------------------------------------------
# Phase-0 runs the rider wallet as a *closed-loop credit store* -- it can hold
# refunds, promo cashbacks, and customer-service goodwill credits, but cannot
# be topped up from external money. This keeps us out of the RBI Prepaid
# Payment Instrument (PPI) regulatory perimeter; we move to a co-branded PPI
# (or our own license) in a later phase. See ADR-0003 in SaaradhiGo-docs.
#
# WALLET_TOPUPS_ENABLED  -- master switch for the rider top-up endpoints.
#   When False (default), POST /rider/wallet/create-order/, /wallet/verify/,
#   and the Cashfree wallet-top-up webhook path all return 503. Driver
#   earnings + withdrawal flows are unaffected (those are an internal
#   intermediated payout, not a PPI).
# RIDER_CREDIT_BALANCE_CAP -- maximum rupee balance a rider wallet may hold.
#   Refund / promo / support credits that would push the balance above this
#   are rejected; the issuer must split or refund-to-original-method instead.
# RIDER_CREDIT_EXPIRY_DAYS -- credits expire after this many days from issue.
#   A daily Celery job (TBD) sweeps and writes them off.
from decimal import Decimal as _Decimal

WALLET_TOPUPS_ENABLED = os.environ.get('WALLET_TOPUPS_ENABLED', 'False') == 'True'
RIDER_CREDIT_BALANCE_CAP = _Decimal(os.environ.get('RIDER_CREDIT_BALANCE_CAP', '2000.00'))
RIDER_CREDIT_EXPIRY_DAYS = int(os.environ.get('RIDER_CREDIT_EXPIRY_DAYS', '365'))

#AWS
AWS_SECRET_ACCESS_KEY=os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_ACCESS_KEY_ID=os.environ.get("AWS_ACCESS_KEY_ID")
AWS_REGION=os.environ.get("AWS_REGION")
AWS_SNS_SENDER_ID=os.environ.get("AWS_SNS_SENDER_ID")

# AWS S3
AWS_S3_BUCKET_NAME=os.environ.get("AWS_S3_BUCKET_NAME", "")
AWS_S3_REGION=os.environ.get("AWS_S3_REGION", AWS_REGION)
AWS_STORAGE_BUCKET_NAME=AWS_S3_BUCKET_NAME
AWS_S3_REGION_NAME=AWS_S3_REGION
AWS_DEFAULT_ACL=None
AWS_QUERYSTRING_EXPIRE=int(os.environ.get("AWS_QUERYSTRING_EXPIRE", "900"))
AWS_S3_FILE_OVERWRITE=False
AWS_S3_SIGNATURE_VERSION="s3v4"
AWS_S3_ADDRESSING_STYLE="virtual"

# Razorpay
RAZORPAY_KEY_ID=os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET=os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET=os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

# Cashfree
CASHFREE_APP_ID=os.environ.get("CASHFREE_APP_ID", "")
CASHFREE_SECRET_KEY=os.environ.get("CASHFREE_SECRET_KEY", "")
CASHFREE_WEBHOOK_SECRET=os.environ.get("CASHFREE_WEBHOOK_SECRET", "")
CASHFREE_API_VERSION=os.environ.get("CASHFREE_API_VERSION", "2023-08-01")
CASHFREE_ENVIRONMENT=os.environ.get("CASHFREE_ENVIRONMENT", "sandbox")  # sandbox or production
CASHFREE_PG_BASE_URL = os.environ.get("CASHFREE_PG_BASE_URL", "https://sandbox.cashfree.com")

CASHFREE_PAYOUTS_CLIENT_ID = os.environ.get("CASHFREE_PAYOUTS_CLIENT_ID", "")
CASHFREE_PAYOUTS_CLIENT_SECRET = os.environ.get("CASHFREE_PAYOUTS_CLIENT_SECRET", "")
CASHFREE_PAYOUTS_BASE_URL = os.environ.get("CASHFREE_PAYOUTS_BASE_URL", "https://payout-api.cashfree.com/payout/v1")


# Payment Gateway Selection
PAYMENT_GATEWAY=os.environ.get("PAYMENT_GATEWAY", "cashfree")
PAYOUT_GATEWAY=os.environ.get("PAYOUT_GATEWAY", "cashfree")

# --- Platform / dispatch settings -------------------------------------------
# PLATFORM_COMMISSION_PERCENT is now only the LAST-RESORT fallback. The
# authoritative rate is RateCard.commission_percent for the trip's zone
# (see servers/pricing/services.commission_for_trip). Kept as a Decimal —
# a float here silently produced binary-rounded commission on every trip.
PLATFORM_COMMISSION_PERCENT=_Decimal(os.environ.get("PLATFORM_COMMISSION_PERCENT", "18"))

# How long a rider's request stays open before auto-cancel. 600s meant a
# rider stared at "finding a driver" for ten minutes; the rolling fanout
# below has fully played out well before 90s.
TRIP_ACCEPT_TIMEOUT_SECONDS=int(os.environ.get("TRIP_ACCEPT_TIMEOUT_SECONDS", "90"))

# Rolling fanout: expanding radius waves rather than one 5km blast. Each
# wave gets DISPATCH_WAVE_SECONDS of exclusivity before the next widens.
DISPATCH_RADIUS_WAVES_M = tuple(
    int(r) for r in os.environ.get('DISPATCH_RADIUS_WAVES_M', '1500,3000,5000').split(',') if r.strip()
)
DISPATCH_WAVE_SECONDS = float(os.environ.get('DISPATCH_WAVE_SECONDS', '20'))

GOOGLE_MAPS_API_KEY=os.environ.get("GOOGLE_MAPS_API_KEY", "")

# When no RateCard governs a pickup, may we quote the hardcoded fallback
# fare? Yes in dev/CI so a fresh DB is usable; no in production, where a
# missing card means a city was seeded wrong and quoting a made-up price
# is worse than refusing. (Read from the env at import time — Django's
# test runner forces settings.DEBUG=False, so DEBUG is not a usable
# signal here.)
PRICING_ALLOW_DEFAULT_FARE = os.environ.get(
    'PRICING_ALLOW_DEFAULT_FARE', 'True' if DEBUG else 'False',
) == 'True'

# WSGI_APPLICATION = 'base.wsgi.application'
ASGI_APPLICATION = 'base.asgi.application'

# Django Channels - Redis Channel Layer
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
             "hosts": [REDIS_URL + "/4"], 
        },
    },
}


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# A production boot with DB_HOST unset used to fall back to a local, empty
# SQLite file and serve traffic against it — no trips, no drivers, no
# obvious error. Fail at boot instead.
if not DEBUG and not os.environ.get('DB_HOST'):
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "DB_HOST must be set when DEBUG=False (refusing to run on SQLite)."
    )

if os.environ.get('DB_HOST'):
    db_options = {
        'sslmode': os.environ.get('DB_SSLMODE', 'require'),
    }
    sslrootcert = os.environ.get('DB_SSLROOTCERT')
    if sslrootcert:
        db_options['sslrootcert'] = os.path.join(BASE_DIR, sslrootcert) if not os.path.isabs(sslrootcert) else sslrootcert

    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME'),
        'USER': os.environ.get('DB_USER'),
        'PASSWORD': os.environ.get('DB_PASSWORD'),
        'HOST': os.environ.get('DB_HOST'),
        'PORT': os.environ.get('DB_PORT', '5432'),
        'OPTIONS': db_options,
    }

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Root log level is env-driven; default WARNING in production so SQL queries,
# request bodies, OTPs, and tokens do not bleed into CloudWatch.
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG' if DEBUG else 'WARNING').upper()
# Format: 'json' for CloudWatch / structured ingestion, 'text' for human eyes.
LOG_FORMAT = os.environ.get('DJANGO_LOG_FORMAT', 'text' if DEBUG else 'json')
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'text': {
            'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
        },
        'json': {
            '()': 'base.logging_filters.JSONFormatter',
        },
    },
    'filters': {
        # Scrubs OTPs, bearer tokens, JWTs, AWS keys, last 8 digits of
        # E.164 phone numbers from every log message before they hit the
        # handler. Belt-and-braces — never rely on devs remembering to
        # avoid logging PII.
        'pii_redact': {
            '()': 'base.logging_filters.PIIRedactionFilter',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': LOG_FORMAT,
            'filters': ['pii_redact'],
        },
    },
    'root': {
        'handlers': ['console'],
        'level': LOG_LEVEL,
    },
    # AWS SDK chatter: every presign/HEAD emits dozens of DEBUG lines that
    # drown real signals (and cost latency under load). Keep warnings+.
    'loggers': {
        'botocore': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'boto3': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        's3transfer': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'urllib3': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}

# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Kolkata'

USE_I18N = True

USE_TZ = True

# Custom user model
AUTH_USER_MODEL='auth_user.customUser'
# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Sentry Configuration
SENTRY_DSN = os.environ.get('SENTRY_DSN')
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.redis import RedisIntegration
    from django.dispatch.dispatcher import Signal

    # WORKAROUND: sentry-sdk 1.32.0 crashes on Django 5.0 due to a bug in signal receiver patching
    # (TypeError: 'tuple' object does not support item assignment). We must use 1.32.0 for cashfree-pg.
    # We bypass this by restoring the original _live_receivers function after sentry_sdk init.
    original_live_receivers = getattr(Signal, '_live_receivers', None)

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(),
            RedisIntegration(),
        ],
        # Sampling every transaction is both expensive and unnecessary;
        # 10% is plenty to spot latency regressions at Phase-0 volume.
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0.1')),
        # send_default_pii=True attached rider phone numbers, IPs and
        # request bodies to every event. Under the DPDP Act that makes
        # Sentry a processor of personal data we never told users about,
        # and it defeats the PII redaction filter above.
        send_default_pii=False,
        environment=os.environ.get('ENVIRONMENT', 'production' if not DEBUG else 'development')
    )

    if original_live_receivers:
        Signal._live_receivers = original_live_receivers
