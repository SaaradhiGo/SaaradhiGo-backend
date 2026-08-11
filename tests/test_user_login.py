import pytest
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from django.core.cache import cache

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
def setup_user_and_otp(db):
    """
    Sets up a test user and a valid OTP in the cache, simulating the /request_otp/ flow.
    """
    phone_number = "+917396918971"
    valid_otp = "123456"
    role = "rider"
    
    # Pre-create the user in the database
    # In VahanGo logic, the user might be created ON login if DoesNotExist, 
    # but let's pre-create one to prove we aren't creating a new one (or we let it create). 
    # Let's let the login view handle creation as it does in `views.py` `try... except user_model.DoesNotExist: user_model.objects.create_user()`
    
    # Store OTP in cache as the login API expects:
    # cache_key = f'otp_role_{phone_number}'
    # cache.set(cache_key, {'otp': otp, 'role': role, 'attempts': 0}, OTP_EXPIRY)
    cache_key = f'otp_role_{phone_number}'
    cache.set(cache_key, {'otp': valid_otp, 'role': role, 'attempts': 0}, 600)
    
    yield {"phone_number": phone_number, "valid_otp": valid_otp, "role": role}

    # Teardown logic if necessary
    cache.delete(cache_key)

@pytest.mark.django_db
def test_login_with_valid_otp(api_client, setup_user_and_otp):
    url = "/api/v1/auth/login/"
    payload = {
        "phone_number": setup_user_and_otp["phone_number"],
        "otp": setup_user_and_otp["valid_otp"]
    }

    response = api_client.post(url, payload, format='json')

    assert response.status_code == 200, f"Expected 200 OK, got {response.status_code}. Response: {response.data}"
    
    data = response.json()
    assert data["status"] == "success"
    assert "token" in data["data"]
    assert "user" in data["data"]

@pytest.mark.django_db
def test_login_with_invalid_otp(api_client, setup_user_and_otp):
    url = "/api/v1/auth/login/"
    payload = {
        "phone_number": setup_user_and_otp["phone_number"],
        "otp": "000000"  # Invalid
    }

    response = api_client.post(url, payload, format='json')

    assert response.status_code == 400, f"Expected 400 Bad Request, got {response.status_code}. Response: {response.data}"
    
    data = response.json()
    assert data["status"] == "error"
    assert "message" in data["error"]
