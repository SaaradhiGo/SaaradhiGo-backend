from django.urls import path, include

from servers.auth_user import urls as auth_user_urls
from servers.rider import urls as rider_urls
from servers.driver import urls as driver_urls
from servers.ride import urls as ride_urls
from servers.payments import urls as payments_urls
from servers.sos import urls as sos_urls

urlpatterns = [
    path('auth/', include(auth_user_urls.urlpatterns)),
    path('rider/', include(rider_urls.urlpatterns)),
    path('driver/', include(driver_urls.urlpatterns)),
    path('ride/', include(ride_urls)),
    path('payments/', include(payments_urls)),
    path('sos/', include(sos_urls.urlpatterns)),
]
