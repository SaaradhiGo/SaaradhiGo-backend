import pytest
from datetime import datetime, timezone

@pytest.mark.django_db
def test_update_driver_location(auth_client_driver):
    client, user = auth_client_driver
    url = "/api/v1/driver/update_location/"
    timestamp_iso = datetime.now(timezone.utc).isoformat()
    
    # test valid location
    valid_payload = {
        "lat": 12.9715987,
        "lng": 77.594566,
        "timestamp": timestamp_iso,
    }
    response = client.post(url, data=valid_payload, format='json')
    assert response.status_code == 200
    assert isinstance(response.json(), dict)

    # test missing lat
    payload_missing_lat = {
        "lng": 77.594566,
        "timestamp": timestamp_iso,
    }
    response_missing_lat = client.post(url, data=payload_missing_lat, format='json')
    assert response_missing_lat.status_code == 400
    assert "lat" in str(response_missing_lat.json()).lower()

    # test missing lng
    payload_missing_lng = {
        "lat": 12.9715987,
        "timestamp": timestamp_iso,
    }
    response_missing_lng = client.post(url, data=payload_missing_lng, format='json')
    assert response_missing_lng.status_code == 400
    assert "lng" in str(response_missing_lng.json()).lower()

@pytest.mark.django_db
def test_get_driver_earnings(auth_client_driver):
    client, user = auth_client_driver
    url = "/api/v1/driver/earnings/"
    params = {"range": "daily"}
    
    response = client.get(url, params)
    assert response.status_code == 200
    
    data = response.json()
    assert data.get("status") == "success"
    
    earnings_data = data.get("data")
    assert isinstance(earnings_data, dict)
    expected_keys = {"count", "next", "previous", "results"}
    assert expected_keys.issubset(earnings_data.keys())
