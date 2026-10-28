from django.urls import path

from .views import (
    admin_search_drivers, admin_search_riders, admin_search_trips,
    admin_search_vehicles,
)

# Under `admin/` so the structural guard finds all four by path. The names
# are what `AdminSearchThrottle` keys its per-route bucket on.
urlpatterns = [
    path('admin/trips/', admin_search_trips, name='admin_search_trips'),
    path('admin/drivers/', admin_search_drivers, name='admin_search_drivers'),
    path('admin/riders/', admin_search_riders, name='admin_search_riders'),
    path('admin/vehicles/', admin_search_vehicles, name='admin_search_vehicles'),
]
