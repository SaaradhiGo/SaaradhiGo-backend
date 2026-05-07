
from pathlib import Path
import os
from dotenv import load_dotenv
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv()

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
REDIS_URL=os.environ.get('REDIS_URL','redis://redis:6379')
# SECURITY WARNING: don't run with debug turned on in production!
DEBUG_ENV=os.environ.get('DEBUG_ENV','False')
DEBUG = DEBUG_ENV=='True'

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '*').split(',')
if DEBUG_ENV=='True':
    CSRF_ALLOW_ALL_ORIGINS = True
else:
    csrf_trusted_origins = os.environ.get('CSRF_TRUSTED_ORIGINS', '')
    if csrf_trusted_origins:
        CSRF_TRUSTED_ORIGINS = csrf_trusted_origins.split(',')
    else:
        CSRF_TRUSTED_ORIGINS = []


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
    'corsheaders',
    'channels',
    'storages',
    'servers.auth_user',
    'servers.rider',
    'servers.driver',
    'servers.ride',
    'servers.payments',
    'django_cleanup.apps.CleanupConfig',
    # 'servers.support',
    
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
REST_FRAMEWORK={
    'DEFAULT_AUTHENTICATION_CLASSES':[
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication'
    ],
    'DEFAULT_PAGINATION_CLASS':'rest_framework.pagination.LimitOffsetPagination',
    'PAGE_SIZE':20
}
from datetime import timedelta
SIMPLE_JWT={
    'ACCESS_TOKEN_LIFETIME':timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME':timedelta(days=7),

}
# celery
CELERY_BROKER_URL=REDIS_URL+'/0'
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
CORS_ALLOW_ALL_ORIGINS = True
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
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

#URLS
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
BACKEND_URL  = os.environ.get("BACKEND_URL", "http://localhost:8000")

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

# Payment Gateway Selection
PAYMENT_GATEWAY=os.environ.get("PAYMENT_GATEWAY", "cashfree")
PAYOUT_GATEWAY=os.environ.get("PAYOUT_GATEWAY", "cashfree")

# Platform Settings
PLATFORM_COMMISSION_PERCENT=float(os.environ.get("PLATFORM_COMMISSION_PERCENT", "0"))
TRIP_ACCEPT_TIMEOUT_SECONDS=int(os.environ.get("TRIP_ACCEPT_TIMEOUT_SECONDS", "600"))
GOOGLE_MAPS_API_KEY=os.environ.get("GOOGLE_MAPS_API_KEY", "")

# WSGI_APPLICATION = 'base.wsgi.application'
ASGI_APPLICATION = 'base.asgi.application'

# Django Channels - Redis Channel Layer
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [REDIS_URL + '/4'],
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

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'DEBUG',
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
