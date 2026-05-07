import pytest
from servers.driver.serializers import VehicleCreateSerializer
from servers.driver.models import VehicleType

@pytest.mark.django_db
def test_vehicle_create_serializer_valid():
    """Test standard validation for vehicle creation serializer."""
    # Ensure vehicle type exists in DB
    VehicleType.objects.create(type="Auto")
    
    data = {
        "vehicle_number": "KA01AB1234",
        "vehicle_type": "Auto",
        "brand": "Bajaj",
        "model": "RE",
        "color": "Yellow",
        "year": 2021,
        "capacity": 3
    }
    
    serializer = VehicleCreateSerializer(data=data)
    assert serializer.is_valid()
    assert serializer.validated_data["vehicle_type"] == "Auto"
    assert serializer.validated_data["capacity"] == 3

@pytest.mark.django_db
def test_vehicle_create_serializer_invalid_type():
    """Test validation fails if vehicle type doesn't exist."""
    data = {
        "vehicle_number": "KA01AB1234",
        "vehicle_type": "NonExistentType",
    }
    
    serializer = VehicleCreateSerializer(data=data)
    assert not serializer.is_valid()
    assert "vehicle_type" in serializer.errors
    assert "not found" in str(serializer.errors["vehicle_type"][0])

@pytest.mark.django_db
def test_vehicle_create_serializer_missing_fields():
    """Test validation fails if required fields are missing."""
    data = {
        "vehicle_type": "Auto",
    }
    
    serializer = VehicleCreateSerializer(data=data)
    assert not serializer.is_valid()
    assert "vehicle_number" in serializer.errors
