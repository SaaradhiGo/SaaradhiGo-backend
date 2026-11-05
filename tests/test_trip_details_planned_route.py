"""The trip details payload carries the stored planned route (Maps M1)."""
from servers.ride.serializers import TripDetailSerializer


def test_planned_route_is_a_detail_field():
    assert 'planned_route_polyline' in TripDetailSerializer.Meta.fields


def test_value_passes_through():
    field = TripDetailSerializer().fields['planned_route_polyline']
    assert field.to_representation('a~l~Fjk~uOwHJy@P') == 'a~l~Fjk~uOwHJy@P'
