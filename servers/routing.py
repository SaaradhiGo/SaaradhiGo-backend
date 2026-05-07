from django.urls import path
from .consumers import DriverLocationConsumer, RideRequestConsumer, TripStatusConsumer

websocket_urlpatterns = [
    path('ws/driver/location/', DriverLocationConsumer.as_asgi()),
    path('ws/ride/request/', RideRequestConsumer.as_asgi()),
    path('ws/ride/trip/<int:trip_id>/', TripStatusConsumer.as_asgi()),
]
