import pytest
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import AccessToken
from servers.rider.models import Rider
from servers.driver.models import Driver

User = get_user_model()

@pytest.fixture(autouse=True)
def use_dummy_cache(settings):
    settings.CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def auth_client_rider(db):
    """Returns an API client authenticated as a Rider, along with the user."""
    user = User.objects.create_user(phone_number="+919999999999", role="rider")
    Rider.objects.create(user_id=user)
    token = str(AccessToken.for_user(user))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client, user

@pytest.fixture
def auth_client_driver(db):
    """Returns an API client authenticated as a Driver, along with the user."""
    user = User.objects.create_user(phone_number="+918888888888", role="driver")
    Driver.objects.create(user_id=user)
    token = str(AccessToken.for_user(user))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client, user

@pytest.fixture
def auth_client_admin(db):
    """Returns an API client authenticated as an Admin, along with the user."""
    user = User.objects.create_user(
        phone_number="+917777777777",
        role="admin",
        is_staff=True,
        is_superuser=True
    )
    token = str(AccessToken.for_user(user))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client, user
