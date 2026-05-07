import pytest

VEHICLE_TYPE = "sedan"

@pytest.mark.django_db
def test_estimate_ride_fare(auth_client_rider):
    client, user = auth_client_rider
    url = "/api/v1/ride/estimate-fare/"
    valid_payload = {
        "pickup_lat": 12.9716,
        "pickup_long": 77.5946,
        "destination_lat": 12.9352,
        "destination_long": 77.6245,
        "distance_km": 5.0,
        "duration_min": 15.0,
        "vehicle_type": VEHICLE_TYPE
    }
    
    response = client.post(url, data=valid_payload, format='json')
    assert response.status_code == 200
    data = response.json()
    assert data.get("status") == "success"
    
    fare_data = data.get("data")
    assert fare_data is not None
    for key in ("estimated_fare","duration_min","distance_km"):
        assert key in fare_data
        
    malformed_payloads = [
        {},
        {"pickup_lat": 12.9716, "pickup_long": 77.5946},
        {"pickup_lat": "not_a_float", "pickup_long": 77.5946, "destination_lat": 12.9352, "destination_long": 77.6245, "distance_km": 5.0, "duration_min": 15, "vehicle_type": VEHICLE_TYPE},
        {"pickup_lat": 12.9716, "pickup_long": 77.5946, "destination_lat": 12.9352, "destination_long": None, "distance_km": 5.0, "duration_min": 15, "vehicle_type": VEHICLE_TYPE},
    ]
    
    for payload in malformed_payloads:
        err_response = client.post(url, data=payload, format='json')
        assert err_response.status_code == 400
        err_data = err_response.json()
        assert err_data.get("status") == "error"

@pytest.mark.django_db
def test_request_a_ride(auth_client_rider):
    client, user = auth_client_rider
    url = "/api/v1/ride/ride-request/"
    
    ride_payload = {
        "pickup_lat": 12.9716,
        "pickup_long": 77.5946,
        "destination_lat": 12.9352,
        "destination_long": 77.6245,
        "distance_km": 5.0,
        "duration_min": 15.0,
        "vehicle_type": "sedan",
        "payment_method": "card"
    }
    
    # Use sync view but the actual architecture might test timeout properly. 
    # For now we just test it doesn't fail 500. Expected is 201 (success) or 503/504 (timeout without drivers)
    response = client.post(url, data=ride_payload, format='json')
    assert response.status_code in (201, 503, 504)
    
    if response.status_code == 201:
        data = response.json()
        assert data.get("status") == "success"
        assert data.get("data", {}).get("trip_id") is not None
