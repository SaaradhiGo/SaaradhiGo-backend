import pytest
from django.core.cache import cache
from unittest.mock import patch

@patch('servers.auth_user.views.send_otp_via_sns.delay')
def test_request_otp(mock_send, api_client):
    mock_send.return_value = "dummy-task-id"
    url = "/api/v1/auth/otp/"
    payload = {"phone_number": "+917396918971"}

    response = api_client.post(url, payload, format='json')
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "success"
    # OTP is delivered only via SMS — explicitly assert it is NOT echoed in
    # the response body (regression guard for the Phase-0 security fix).
    assert "otp" not in data["data"]
    assert "expires_in" in data["data"]
    assert "task_id" in data["data"]

@pytest.fixture
def setup_otp(db):
    phone_number = "+917396918971"
    valid_otp = "123456"
    cache_key = f'otp_role_{phone_number}'
    cache.set(cache_key, {'otp': valid_otp, 'role': 'rider', 'attempts': 0}, 600)
    yield {"phone_number": phone_number, "valid_otp": valid_otp}
    cache.delete(cache_key)

@pytest.mark.django_db
def test_login_with_valid_otp(api_client, setup_otp):
    url = "/api/v1/auth/login/"
    payload = {
        "phone_number": setup_otp["phone_number"],
        "otp": setup_otp["valid_otp"]
    }

    response = api_client.post(url, payload, format='json')
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "token" in data["data"]

@pytest.mark.django_db
def test_login_with_invalid_otp(api_client, setup_otp):
    url = "/api/v1/auth/login/"
    payload = {
        "phone_number": setup_otp["phone_number"],
        "otp": "000000"
    }

    response = api_client.post(url, payload, format='json')
    assert response.status_code == 400
    data = response.json()
    assert data["status"] == "error"
