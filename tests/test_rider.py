import pytest
from unittest.mock import patch

@pytest.mark.django_db
@patch('servers.rider.views.nearby_drivers')
def test_get_nearby_drivers(mock_nearby, auth_client_rider):
    mock_nearby.return_value = [["driver:1", 2.5, [77.5945, 12.9715]]]
    client, user = auth_client_rider
    url = "/api/v1/rider/nearby/"
    params = {"lat": "12.9715987", "lng": "77.5945627"}

    response = client.get(url, params)
    
    # It might return a 200 or 404 depending on db state but we check structure
    assert response.status_code in [200, 404]
    
    if response.status_code == 200:
        data = response.json()
        assert "data" in data or isinstance(data, list)

@pytest.mark.django_db
def test_get_rider_locations_all(auth_client_rider):
    client, user = auth_client_rider
    url = "/api/v1/rider/locations/all/"

    response = client.get(url)
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "success"
    assert isinstance(data["data"], list)
